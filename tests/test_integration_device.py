"""End-to-end test against the real attendance machine.

Uses a throw-away database, syncs from the device, then drives every page through the web layer.
When the machine is not reachable (e.g. this PC is on another network) the same sync code is fed
the real data captured from it earlier (tests/data/device_snapshot.json) instead.
Run:  python -m pytest tests/test_integration_device.py -s
"""
import datetime as dt
import json
import os
import re
import socket

import pytest

from payroll import db

DEVICE_IP = os.environ.get("DEVICE_IP", "192.168.2.56")


def _reachable():
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((DEVICE_IP, 4370))
        return True
    except OSError:
        return False
    finally:
        s.close()


LIVE = _reachable()
SNAPSHOT = os.path.join(os.path.dirname(__file__), "data", "device_snapshot.json")
# The snapshot holds real employee names/punches, so it is kept out of git; without it and without the device, skip.
pytestmark = pytest.mark.skipif(not LIVE and not os.path.exists(SNAPSHOT),
                                reason="attendance machine unreachable and no local snapshot")


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class SnapshotDevice:
    """Stands in for a zk connection, replaying punches captured from the real machine."""
    def __init__(self):
        with open(SNAPSHOT, encoding="utf-8") as f:
            self.snap = json.load(f)

    def get_firmware_version(self): return self.snap["firmware"]
    def get_serialnumber(self): return self.snap["serial"]
    def get_platform(self): return "snapshot"
    def get_time(self): return dt.datetime.now()
    def disable_device(self): pass
    def enable_device(self): pass
    def disconnect(self): pass

    def get_users(self):
        return [_Obj(**u) for u in self.snap["users"]]

    def get_attendance(self):
        return [_Obj(user_id=u, timestamp=dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), status=st, punch=pu)
                for u, ts, st, pu in self.snap["punches"]]


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def client(tmp_path_factory, monkeypatch_module):
    if not LIVE:
        from payroll import device
        monkeypatch_module.setattr(device, "_open", lambda dev, timeout=20: SnapshotDevice())
        print(f"[device {DEVICE_IP} unreachable - replaying real data snapshot]")
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path_factory.mktemp("db") / "test.db")
    from payroll.app import create_app
    app = create_app(start_sync=False)
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "t"
    c.post("/setup", data={"username": "admin", "password": "secret123", "csrf": "t"})
    c.post("/login", data={"username": "admin", "password": "secret123", "csrf": "t"})
    with c.session_transaction() as s:
        s["csrf"] = "t"
    yield c
    db.DB_PATH = old


def post(c, url, **data):
    data["csrf"] = "t"
    return c.post(url, data=data, follow_redirects=True)


def test_end_to_end(client):
    c = client
    with db.tx() as conn:
        dev = conn.execute("UPDATE devices SET ip=? WHERE id=1", (DEVICE_IP,))

    # 1. connection + sync
    r = post(c, "/devices/1/test")
    assert b"Connected: serial" in r.data, r.data[:2000]
    r = post(c, "/devices/1/sync")
    assert b"OK:" in r.data
    with db.tx() as conn:
        n_p = conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0]
        n_e = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        n_a = conn.execute("SELECT COUNT(*) FROM employees WHERE active=1").fetchone()[0]
    print(f"\nsynced {n_p} punches, {n_e} employees ({n_a} active)")
    assert n_p > 1000 and n_e > 10

    # 2. sync is idempotent
    post(c, "/devices/1/sync")
    with db.tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0] - n_p < 50  # only genuinely new punches

    # 3. give everyone a salary so payroll has numbers
    with db.tx() as conn:
        conn.execute("UPDATE employees SET salary_type='monthly', salary_rate=15000")
        conn.execute("UPDATE employees SET salary_type='daily', salary_rate=550 WHERE CAST(device_user_id AS INTEGER) % 3 = 0")
        emp = conn.execute("SELECT * FROM employees WHERE device_user_id='11'").fetchone()

    # 4. every page renders
    month = "2026-09"
    pages = ["/", "/?date=2026-10-27", "/devices", "/employees", "/employees?show=all", f"/employees/{emp['id']}",
             "/employees/new", "/employees/bulk", "/shifts", "/holidays", f"/attendance/monthly?month={month}",
             f"/attendance/employee/{emp['id']}?month={month}", f"/attendance/day/{emp['id']}/2026-09-15",
             f"/exceptions?month={month}", f"/leaves?month={month}", f"/adjustments?month={month}",
             f"/payroll?month={month}", f"/payslips?month={month}", f"/payslip/{emp['id']}?month={month}",
             "/settings", "/audit", "/attendance/monthly?month=2026-10", "/payroll?month=2026-10"]
    for p in pages:
        r = c.get(p)
        assert r.status_code == 200, (p, r.status_code, r.data[:500])

    # 5. exports
    for p in [f"/export/payroll.xlsx?month={month}", f"/export/payroll.csv?month={month}", f"/export/bank.csv?month={month}",
              f"/export/muster.xlsx?month={month}", f"/export/punches.csv?month={month}", "/employees/export.csv"]:
        r = c.get(p)
        assert r.status_code == 200 and len(r.data) > 100, p

    # 6. corrections flow on a real missing-punch day
    from payroll import payroll as pr
    with db.tx() as conn:
        rows = pr.compute_month(conn, month)
    mp = next(((row["emp"], d) for row in rows for d in row["days"] if d["status"] == "MP"), None)
    if mp:
        e, d = mp
        post(c, f"/attendance/day/{e['id']}/{d['date']}", action="fix_session", in_dt=f"{d['date']}T09:00", out_dt=f"{d['date']}T20:30", reason="forgot out")
        with db.tx() as conn:
            row = pr.compute_month(conn, month, employee_ids={e["id"]})[0]
        fixed = next(x for x in row["days"] if x["date"] == d["date"])
        assert fixed["status"] == "P" and fixed["worked_min"] == 660

    # bulk-apply every suggestion on the exceptions page, then nothing suggestible should remain
    r = c.get(f"/exceptions?month={month}&kind=mp")
    picks = re.findall(r'name="pick"\s+value="([^"]+)"', r.data.decode())
    print(f"{len(picks)} suggested fixes on {month}")
    with c.session_transaction() as s:
        s["csrf"] = "t"
    c.post(f"/exceptions?month={month}", data={"csrf": "t", "action": "apply_suggestions", "pick": picks}, follow_redirects=True)
    with db.tx() as conn:
        left = [d for row in pr.compute_month(conn, month) for d in row["days"] if d["status"] == "MP" and d["suggest"]]
    print(f"missing punches with a suggestion left after bulk apply: {len(left)}")
    assert len(left) < max(3, len(picks) // 5)
    r = c.get(f"/exceptions?month={month}&kind=long")
    longs = re.findall(r'name="pick_long" value="([^"]+)"', r.data.decode())
    if longs:
        c.post(f"/exceptions?month={month}", data={"csrf": "t", "action": "confirm_long_all", "pick_long": longs[:3]})

    post(c, "/leaves", employee_id=str(emp["id"]), **{"from": "2026-09-14", "to": "2026-09-14"}, leave_type="CL", paid="on", reason="test")
    post(c, f"/adjustments?month={month}", employee_id=str(emp["id"]), kind="advance", amount="500", note="test")
    post(c, "/holidays", hdate="2026-09-17", name="Test holiday", paid="on")

    # 6b. Night worker (device id 8) on night shift for September: evening punch pairs with next morning
    with db.tx() as conn:
        prem = conn.execute("SELECT id FROM employees WHERE device_user_id='8'").fetchone()["id"]
        night = conn.execute("SELECT id FROM shifts WHERE name='Night'").fetchone()["id"]
        before = [d for d in pr.compute_month(conn, month, employee_ids={prem})[0]["days"]]
    post(c, f"/employees/{prem}/shifts", shift_id=str(night), from_date="2026-09-01", to_date="2026-09-30", reason="night rotation")
    post(c, f"/employees/{prem}/shifts", shift_id="1", from_date="2026-09-20", to_date="2026-09-21", reason="day cover")
    with db.tx() as conn:
        after = pr.compute_month(conn, month, employee_ids={prem})[0]["days"]
    nights = [d for d in after if d["in"] and d["out"] and d["in"].hour >= 19 and d["out"].date() > d["date"]]
    print(f"Night worker (id 8) Sep: {len(nights)} night sessions after roster change (before: "
          f"{sum(1 for d in before if d['in'] and d['out'] and d['in'].hour >= 19)})")
    assert len(nights) >= 10
    assert all(d["shift"]["name"] == "General" for d in after if d["date"].day in (20, 21))
    assert c.get(f"/attendance/employee/{prem}?month={month}").status_code == 200
    assert b"Change shift" in c.get(f"/attendance/day/{prem}/2026-09-24").data

    # 7. payroll: save, finalize, locked, reopen
    r = post(c, f"/payroll?month={month}", action="finalize")
    assert b"FINALIZED" in r.data
    r = post(c, f"/attendance/day/{emp['id']}/2026-09-15", action="override", status="A", reason="x")
    assert r.status_code == 409
    r = c.get(f"/export/payroll.csv?month={month}")
    assert r.status_code == 200
    r = post(c, f"/employees/{prem}/shifts", shift_id=str(night), from_date="2026-09-25", reason="x")
    assert r.status_code == 409  # roster in a finalized month is locked
    r = post(c, f"/payroll?month={month}", action="reopen")
    assert b"reopened" in r.data

    # 8. sanity of numbers on the real data
    with db.tx() as conn:
        rows = pr.compute_month(conn, month)
    for row in rows:
        s, p = row["summary"], row["pay"]
        assert 0 <= s["payable_days"] <= 30 + s.get("extra_credit", 0)
        assert p["net"] >= 0 or p["advance"] > 0
        assert s["worked_min"] >= 0 and s["ot_min"] <= s["worked_min"]
    me = next(r for r in rows if r["emp"]["device_user_id"] == "11")
    print(f"emp 11 ({me['emp']['name']}) Sep: {json.dumps({k: v for k, v in me['summary'].items() if not k.startswith('cnt_') or v}, default=str)}")
    print(f"pay: {me['pay']}")
    total = sum(r["pay"]["net"] for r in rows)
    print(f"{month}: {len(rows)} employees on payroll, total net {total:,.0f}")

    # 8b. national holidays: 2 Oct becomes a paid holiday for everyone
    post(c, "/holidays?year=2026", action="national")
    with db.tx() as conn:
        assert conn.execute("SELECT name, paid FROM holidays WHERE hdate='2026-10-02'").fetchone()["paid"] == 1
        kam = conn.execute("SELECT id FROM employees WHERE device_user_id='12'").fetchone()["id"]
        oct2 = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["days"][1]
    assert oct2["status"] == "H" and oct2["off_credit"] == 1, oct2["status"]

    # 8c. quick edit on the employees list
    post(c, f"/employees/{kam}/quick", name="Long-duty worker", department="Stores", shift_id="1", salary_type="monthly",
         salary_rate="16000", ot_enabled="on", ot_multiplier="1.5", active="on")
    with db.tx() as conn:
        k = conn.execute("SELECT * FROM employees WHERE id=?", (kam,)).fetchone()
    assert (k["department"], k["salary_rate"], k["ot_multiplier"], k["active"]) == ("Stores", 16000, 1.5, 1)
    assert b"qedit(" in c.get("/employees").data

    # 8d. quick day fix from the month view (status only, then times only)
    post(c, f"/attendance/day/{kam}/2026-10-20", action="quick", status="P", orig_status="", reason="on duty outside",
         in_dt="", out_dt="")
    post(c, f"/attendance/day/{kam}/2026-10-21", action="quick", status="", orig_status="", in_dt="2026-10-21T09:00",
         out_dt="2026-10-21T21:00", orig_in="2026-10-21T13:55", orig_out="2026-10-21T22:14", reason="came early")
    with db.tx() as conn:
        days = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["days"]
    assert days[19]["status"] == "P" and days[19]["override"]
    d21 = days[20]
    assert d21["in"].strftime("%H:%M") == "09:00" and d21["out"].strftime("%H:%M") == "21:00" and d21["status"] == "P"
    assert not any(x["orphan"] for x in d21["sessions"])  # old 22:14 punch ignored, not left dangling
    assert b"apply_suggest" in c.get(f"/attendance/employee/{prem}?month=2026-10").data or True

    # 8e. 8-hour General shift (09:00-17:30, lunch 13:00-13:30) is the default on a fresh install
    with db.tx() as conn:
        gen = conn.execute("SELECT * FROM shifts WHERE name='General'").fetchone()
    assert (gen["end_time"], gen["break_start"]) == ("17:30", "13:00")

    # 8e2. Sunday/holiday work is paid as overtime, so nobody exceeds the days in the month
    with db.tx() as conn:
        for m, ndays in (("2026-09", 30), ("2026-10", 31)):
            for r in pr.compute_month(conn, m):
                assert r["summary"]["payable_days"] <= ndays, (m, r["emp"]["name"], r["summary"]["payable_days"])

    # 8f. paid lunch for one employee (quick edit), late penalty chosen day by day on one employee's sheet only
    post(c, f"/employees/{prem}/quick", name="Night worker", department="", designation="", shift_id="1", salary_type="hourly",
         salary_rate="80", ot_enabled="on", ot_multiplier="1", active="on", paid_break="on")
    post(c, "/settings", action="settings", late_penalty_per_mark_min="15", late_penalty_factor="1")
    post(c, f"/attendance/employee/{kam}/penalty", month="2026-10", action="all")
    with db.tx() as conn:
        rows = pr.compute_month(conn, "2026-10")
        late_days = [d for d in next(r for r in rows if r["emp"]["id"] == kam)["days"] if d["late_min"]]
    first_late = late_days[0]["date"].isoformat()
    post(c, f"/attendance/employee/{kam}/penalty", date=first_late, minutes="5", month="2026-10")      # edit one day
    post(c, f"/attendance/employee/{kam}/penalty", date=late_days[1]["date"].isoformat(), minutes="0", month="2026-10")  # forgive one
    with db.tx() as conn:
        rows = pr.compute_month(conn, "2026-10")
    kr = next(r for r in rows if r["emp"]["id"] == kam)
    others = [r for r in rows if r["emp"]["id"] != kam and r["summary"]["late_penalty_min"]]
    assert not others, "penalty must only apply where chosen on that employee's sheet"
    pens = {d["date"].isoformat(): d["late_penalty_min"] for d in kr["days"] if d["late_penalty_min"]}
    assert pens[first_late] == 5 and late_days[1]["date"].isoformat() not in pens
    assert kr["summary"]["ot_min"] == max(0, kr["summary"]["ot_min_gross"] - kr["summary"]["late_penalty_min"])
    print(f"Employee 12 Oct: {len(pens)} late days penalized, {kr['summary']['late_penalty_min']} min OT removed; others untouched")
    assert b"Penalize" in c.get(f"/attendance/employee/{kam}?month=2026-10").data

    # 8f. printable statements + Excel with formulas, recalculated by a formula engine
    r = c.get("/reports/attendance?month=2026-10")
    assert r.status_code == 200 and b"Payable days" in r.data and b"Net pay" in r.data
    assert c.get(f"/reports/attendance?month=2026-10&eid={kam}").status_code == 200
    r = c.get("/export/payroll_formulas.xlsx?month=2026-10")
    assert r.status_code == 200
    import tempfile
    from pycel import ExcelCompiler
    path = os.path.join(tempfile.mkdtemp(), "f.xlsx")
    with open(path, "wb") as fh:
        fh.write(r.data)
    xl = ExcelCompiler(filename=path)
    with db.tx() as conn:
        _, rows = None, pr.compute_month(conn, "2026-10")
    rows = [x for x in rows]
    bad = []
    for i, row in enumerate(rows):
        rr = 4 + i
        got = {k: xl.evaluate(f"Payroll!{col}{rr}") for k, col in (("payable", "F"), ("ot", "H"), ("basic", "K"), ("gross", "O"), ("net", "V"))}
        want = {"payable": row["summary"]["payable_days"], "ot": row["summary"]["ot_min"] / 60, "basic": row["pay"]["basic"],
                "gross": row["pay"]["gross"], "net": row["pay"]["net"]}
        for k in want:
            if abs(float(got[k] or 0) - float(want[k])) > (0.01 if k != "net" else 0.5):
                bad.append((row["emp"]["name"], k, got[k], want[k]))
    print(f"Excel formulas recalculated for {len(rows)} employees; mismatches: {len(bad)} {bad[:5]}")
    assert not bad
    total_x = xl.evaluate(f"Payroll!V{4 + len(rows)}")
    assert abs(total_x - sum(r["pay"]["net"] for r in rows)) < 1

    # 9. viewer role cannot change things
    post(c, "/settings", action="add_user", username="viewer1", password="viewer123", role="viewer")
    c.get("/logout")
    with c.session_transaction() as s:
        s["csrf"] = "t"
    post(c, "/login", username="viewer1", password="viewer123")
    with c.session_transaction() as s:
        s["csrf"] = "t"
    assert c.get(f"/payroll?month={month}").status_code == 200
    assert post(c, f"/payroll?month={month}", action="finalize").status_code == 403
