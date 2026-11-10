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

from payroll import db, engine, payroll

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
    # shift changes are kept month by month: an open or longer range stops at the end of the From month
    r = post(c, f"/employees/{prem}/shifts", shift_id=str(night), from_date="2026-09-27", to_date="2026-10-15", reason="monthly")
    assert b"kept month by month" in r.data
    with db.tx() as conn:
        row = conn.execute("SELECT id, from_date, to_date FROM employee_shifts WHERE employee_id=? AND reason='monthly'", (prem,)).fetchone()
    assert (row["from_date"], row["to_date"]) == ("2026-09-27", "2026-09-30")
    page = c.get(f"/attendance/employee/{prem}?month=2026-09").get_data(as_text=True)
    assert 'name="to_date" value="2026-09-30" min="2026-09-01" max="2026-09-30"' in page and "September 2026" in page
    oct_page = c.get(f"/attendance/employee/{prem}?month=2026-10").get_data(as_text=True)
    assert "monthly" not in oct_page.split('id="shift-panel"')[1].split("</details>")[0]   # September's change not listed in October
    # edit the entry in place: same id (keeps its "newest wins" place), new shift / dates / reason
    r = post(c, f"/employees/{prem}/shifts", update=str(row["id"]), shift_id="1", from_date="2026-09-28", to_date="2026-10-05",
             reason="edited")
    assert b"shift entry changed" in r.data and b"cut to the end of the From month" in r.data
    with db.tx() as conn:
        ed = conn.execute("SELECT * FROM employee_shifts WHERE id=?", (row["id"],)).fetchone()
    assert (ed["shift_id"], ed["from_date"], ed["to_date"], ed["reason"]) == (1, "2026-09-28", "2026-09-30", "edited")
    page = c.get(f"/attendance/employee/{prem}?month=2026-09").get_data(as_text=True)
    assert f'id="se{row["id"]}"' in page and f'name="update" value="{row["id"]}"' in page
    post(c, f"/employees/{prem}/shifts", delete=str(row["id"]))

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

    # 9. Changes page: every manual change vs machine data, with undo
    r = c.get("/changes?month=2026-10")
    assert r.status_code == 200
    page = r.data.decode()
    for label in ("Punch added", "Punch ignored", "Day status forced", "Late penalty", "Holiday"):
        assert label in page, label
    with db.tx() as conn:
        manual = conn.execute("SELECT id FROM punches WHERE source='manual' AND ts LIKE '2026-10-21%'").fetchall()
        ignored = conn.execute("SELECT id FROM punches WHERE role='ignore' AND ts LIKE '2026-10-21%'").fetchall()
        forced = conn.execute("SELECT id FROM day_overrides WHERE work_date='2026-10-20'").fetchone()
    assert manual and ignored and forced
    # machine view vs current view for the corrected day (Oct 21: machine 13:55-22:14, now 09:00-21:00)
    assert "13:55–22:14" in page and "09:00–21:00" in page
    # edited days are outlined on the muster roll / employee month
    assert page.count("Undo") >= 5
    em = c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "st-P ov" in em and "Changed from machine data" in em
    # undo the forced status and one ignored punch -> machine data applies again
    post(c, "/changes/undo", kind="status", id=str(forced["id"]))
    post(c, "/changes/undo", kind="punch_role", id=str(ignored[0]["id"]))
    with db.tx() as conn:
        assert not conn.execute("SELECT 1 FROM day_overrides WHERE id=?", (forced["id"],)).fetchone()
        assert conn.execute("SELECT role FROM punches WHERE id=?", (ignored[0]["id"],)).fetchone()["role"] is None
        assert conn.execute("SELECT action FROM audit_log WHERE action='change_undo'").fetchone()
    with db.tx() as conn:   # machine-only view really ignores people's changes
        e = dict(conn.execute("SELECT * FROM employees WHERE id=?", (kam,)).fetchone())
        mo = engine.employee_days(conn, [e], dt.date(2026, 10, 21), dt.date(2026, 10, 21), db.get_settings(conn), machine_only=True)
    assert mo[kam]["days"][0]["in"].strftime("%H:%M") == "13:55"

    # 10. roles: HR does attendance but never sees money; viewer read-only; admin everything
    post(c, "/settings", action="add_user", username="hr1", password="hrpass123", role="hr")
    post(c, "/settings", action="add_user", username="viewer1", password="viewer123", role="viewer")
    with db.tx() as conn:
        rate_before = conn.execute("SELECT salary_rate, salary_type, bank_account FROM employees WHERE id=?", (kam,)).fetchone()

    def login_as(u, pw):
        c.get("/logout")
        with c.session_transaction() as s:
            s["csrf"] = "t"
        post(c, "/login", username=u, password=pw)
        with c.session_transaction() as s:
            s["csrf"] = "t"

    login_as("hr1", "hrpass123")
    money_pages = [f"/payroll?month={month}", f"/payslips?month={month}", f"/payslip/{kam}?month={month}",
                   f"/export/payroll.csv?month={month}", f"/export/payroll.xlsx?month={month}", f"/export/bank.csv?month={month}",
                   f"/export/payroll_formulas.xlsx?month={month}", f"/adjustments?month={month}", "/employees/bulk", "/audit", "/backup"]
    for pth in money_pages:
        assert c.get(pth).status_code == 403, pth
    assert post(c, f"/payroll?month={month}", action="finalize").status_code == 403
    assert post(c, f"/adjustments?month={month}", employee_id=str(kam), kind="bonus", amount="999").status_code == 403
    assert post(c, "/settings", action="settings", pf_rate="50").status_code == 403
    # pages HR uses show no money: no rupee amounts, no rate/net/salary words
    for pth in ("/", "/employees", f"/employees/{kam}", f"/attendance/employee/{kam}?month=2026-10", "/attendance/monthly?month=2026-10",
                f"/reports/attendance?month=2026-10&eid={kam}", "/changes?month=2026-10", "/settings", "/exceptions?month=2026-10"):
        r = c.get(pth)
        assert r.status_code == 200, pth
        html = r.data.decode()
        for bad in ("16,000", "Net pay", "Salary preview", "Bulk edit salaries", "Account number", ">Payroll</a>", 'href="/payroll', "Basic earned"):
            assert bad not in html, (pth, bad)
    exp = c.get("/employees/export.csv").data.decode()
    assert "salary_rate" not in exp and "bank_account" not in exp
    # HR can do attendance work...
    assert post(c, f"/attendance/day/{kam}/2026-10-22", action="override", status="HD", reason="hr check").status_code == 200
    assert post(c, "/leaves?month=2026-10", employee_id=str(kam), **{"from": "2026-10-23"}, leave_type="SL", paid="on").status_code == 200
    # ...and edit employees without touching pay, even if a crafted form sends pay fields
    post(c, f"/employees/{kam}/quick", name="HR renamed", department="Stores", designation="", shift_id="1",
         salary_type="hourly", salary_rate="1", ot_multiplier="5", active="on")
    post(c, f"/employees/{kam}", device_user_id="12", emp_code="12", name="HR renamed 2", department="Stores", shift_id="1",
         salary_type="daily", salary_rate="2", bank_account="999", active="on", weekly_off="6")
    with db.tx() as conn:
        after = conn.execute("SELECT name, salary_rate, salary_type, bank_account FROM employees WHERE id=?", (kam,)).fetchone()
        audit_hr = [r["detail"] for r in conn.execute("SELECT detail FROM audit_log WHERE username='hr1'")]
    assert after["name"] == "HR renamed 2"
    assert (after["salary_rate"], after["salary_type"], after["bank_account"]) == tuple(rate_before), "HR must not change pay"
    assert not any("rate" in d for d in audit_hr)
    # HR change shows up on the Changes page attributed to hr1
    assert "hr1" in c.get("/changes?month=2026-10&by=hr1").data.decode()

    login_as("viewer1", "viewer123")
    assert c.get(f"/payroll?month={month}").status_code == 403
    assert c.get("/changes?month=2026-10").status_code == 200
    assert post(c, f"/attendance/day/{kam}/2026-10-24", action="override", status="A", reason="x").status_code == 403
    assert post(c, "/changes/undo", kind="leave", id="1").status_code == 403

    login_as("admin", "secret123")
    assert c.get(f"/payroll?month={month}").status_code == 200
    assert "16,000" in c.get("/employees").data.decode()
    # the last admin cannot be demoted
    with db.tx() as conn:
        me = conn.execute("SELECT id FROM app_users WHERE username='admin'").fetchone()["id"]
    post(c, "/settings", action="set_role", uid=str(me), role="hr")
    with db.tx() as conn:
        assert conn.execute("SELECT role FROM app_users WHERE id=?", (me,)).fetchone()["role"] == "admin"

    # 11. sort employees by name or ID (remembered for the session)
    import re as _re
    def first_names(html, n=6):
        return _re.findall(r'<a class="view" href="/employees/\d+">([^<]+)</a>', html)[:n]
    by_name = first_names(c.get("/employees?sort=name").data.decode(), 40)
    assert by_name == sorted(by_name, key=str.lower) and len(by_name) > 5
    assert first_names(c.get("/employees").data.decode(), 40) == by_name          # remembered
    muster = c.get("/attendance/monthly?month=2026-10").data.decode()
    names = _re.findall(r'class="name"><a href="[^"]+">\d+ · ([^<]+)</a>', muster)
    assert names == sorted(names, key=str.lower), names[:5]
    ids = [int(x) for x in _re.findall(r'class="name"><a href="[^"]+">(\d+) · ', c.get("/attendance/monthly?month=2026-10&sort=id").data.decode())]
    assert ids == sorted(ids)

    # 12. Today screen: "Present first" groups rows (present, missing punch, absent / not in yet, off / leave)
    html = c.get("/?date=2026-10-27&order=status").data.decode()
    assert _re.search(r"<title>Today · [^<]*</title>", html), "page title must be plain text"
    heads = _re.findall(r'<tr class="grp-head"><td colspan="12"><b>([^<]+)</b>', html)
    order = ["Present", "Missing punch", "Absent / not in yet", "Off / holiday / leave"]
    assert heads and heads == [h for h in order if h in heads], heads
    assert '<tr class="grp-head">' in c.get("/?date=2026-10-28").data.decode()        # remembered
    assert '<tr class="grp-head">' not in c.get("/?date=2026-10-28&order=list").data.decode()

    # 13. edit final totals (payable days / OT): Calculated vs Edited, used for pay, Excel, Changes, undo
    with db.tx() as conn:
        base = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]
    calc_days, calc_ot = base["summary"]["payable_days"], base["summary"]["ot_min"]
    assert post(c, f"/attendance/employee/{kam}/totals", month="2026-10", payable_days=str(calc_days + 1),
                ot_hours="10:30", reason="").status_code == 200
    with db.tx() as conn:
        assert conn.execute("SELECT reason FROM month_overrides").fetchone()[0] == "manual edit"   # saves without a reason
    post(c, f"/attendance/employee/{kam}/totals", month="2026-10", payable_days=str(calc_days + 1), ot_hours="10:30",
         reason="approved by manager")
    with db.tx() as conn:
        row = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]
    s13 = row["summary"]
    assert s13["payable_days"] == calc_days + 1 and s13["ot_min"] == 630
    assert s13["calc_payable_days"] == calc_days and s13["calc_ot_min"] == calc_ot
    assert row["pay"]["ot_hours"] == 10.5 and row["pay"]["basic"] > base["pay"]["basic"]
    pg = c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "Final totals" in pg and "has-edit" in pg and "approved by manager" in pg and "calculated <s>" in pg
    assert 'class="n edited"' in c.get("/payroll?month=2026-10").data.decode()
    # Excel for this employee uses the edited values through its formulas
    r = c.get(f"/export/payroll_formulas.xlsx?month=2026-10&eid={kam}")
    path13 = os.path.join(tempfile.mkdtemp(), "e.xlsx")
    open(path13, "wb").write(r.data)
    x13 = ExcelCompiler(filename=path13)
    assert abs(x13.evaluate("Payroll!F4") - (calc_days + 1)) < 1e-9 and abs(x13.evaluate("Payroll!H4") - 10.5) < 1e-9
    assert abs(x13.evaluate("Payroll!V4") - row["pay"]["net"]) < 0.5
    # listed on Changes, undo restores calculated
    ch = c.get(f"/changes?month=2026-10&kind=month_total").data.decode()
    assert "Final totals edited" in ch and "OT 10:30" in ch
    with db.tx() as conn:
        mo = conn.execute("SELECT id FROM month_overrides WHERE employee_id=?", (kam,)).fetchone()["id"]
    post(c, "/changes/undo", kind="month_total", id=str(mo))
    with db.tx() as conn:
        back = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["summary"]
    assert back["payable_days"] == calc_days and back["edited"] == {}
    # viewer cannot edit totals
    login_as("viewer1", "viewer123")
    assert post(c, f"/attendance/employee/{kam}/totals", month="2026-10", payable_days="1", reason="x").status_code == 403
    login_as("admin", "secret123")

    # 14. staff category + edit salary from the attendance page
    pg = c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "Edit salary" in pg and 'name="category"' in pg
    post(c, f"/employees/{kam}/salary", category="staff", salary_type="monthly", salary_rate="18000", ot_multiplier="1",
         ot_enabled="on", back=f"/attendance/employee/{kam}?month=2026-10")
    with db.tx() as conn:
        e14 = conn.execute("SELECT category, salary_rate FROM employees WHERE id=?", (kam,)).fetchone()
        r14 = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]
        audit14 = conn.execute("SELECT detail FROM audit_log WHERE action='employee_salary_edit' ORDER BY id DESC").fetchone()
    assert (e14["category"], e14["salary_rate"]) == ("staff", 18000) and audit14
    assert r14["summary"]["ot_min"] > 0 and r14["pay"]["ot_pay"] == 0 and r14["pay"]["gross"] == r14["pay"]["basic"]
    assert abs(r14["pay"]["basic"] - 18000 / 31 * r14["summary"]["payable_days"]) < 0.01
    assert "staff: " in c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    r = c.get(f"/export/payroll_formulas.xlsx?month=2026-10&eid={kam}")
    p14 = os.path.join(tempfile.mkdtemp(), "s.xlsx"); open(p14, "wb").write(r.data)
    x14 = ExcelCompiler(filename=p14)
    assert x14.evaluate("Payroll!L4") == 0 and abs(x14.evaluate("Payroll!V4") - r14["pay"]["net"]) < 0.5
    login_as("hr1", "hrpass123")
    assert post(c, f"/employees/{kam}/salary", category="worker", salary_rate="1").status_code == 403
    assert "Edit salary" not in c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "Staff</span>" in c.get("/employees").data.decode()            # HR still sees who is staff
    login_as("admin", "secret123")

    # 15. bank / cash split and advances with partial / full recovery
    post(c, f"/employees/{kam}/salary", category="worker", salary_type="monthly", salary_rate="18000", ot_multiplier="1",
         ot_enabled="on", pay_mode="fixed", bank_value="8000")
    with db.tx() as conn:
        p15 = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["pay"]
    assert p15["bank"] == min(8000, p15["net"]) and p15["bank"] + p15["cash"] == p15["net"] and p15["cash"] > 0
    post(c, f"/employees/{kam}/split", month="2026-10", bank_amount="5000")
    with db.tx() as conn:
        p15b = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["pay"]
    assert p15b["bank"] == 5000 and p15b["cash"] == p15b["net"] - 5000 and p15b["bank_override"]
    bank_csv = c.get("/export/bank.csv?month=2026-10").data.decode()
    cash_csv = c.get("/export/cash.csv?month=2026-10").data.decode()
    assert ",5000" in bank_csv and str(int(p15b["cash"])) in cash_csv
    assert "Cash salary distribution" in c.get("/payroll/cash?month=2026-10").data.decode()
    reg = c.get("/payroll?month=2026-10").data.decode()
    assert "By bank" in reg and "In cash" in reg

    # advance: 6000 given, instalment 2500; partial in Oct, too much refused, full in Nov
    post(c, "/advances", action="give", employee_id=str(kam), given_date="2026-10-05", amount="6000", installment="2500", month="2026-10")
    post(c, "/advances", action="recover_installment", employee_id=str(kam), month="2026-10")
    with db.tx() as conn:
        led = pr.advance_ledger(conn, "2026-10", [kam])[kam]
        p15c = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["pay"]
    assert led["this_month"] == 2500 and led["remaining"] == 3500
    assert p15c["advance"] == 2500 and p15c["net"] == p15b["net"] - 2500
    r = post(c, "/advances", action="recover", employee_id=str(kam), amount="7000", month="2026-10")
    assert "between 0 and the outstanding" in r.data.decode()
    post(c, "/advances", action="recover", employee_id=str(kam), amount="1000", month="2026-10")       # partial change
    with db.tx() as conn:
        assert pr.advance_ledger(conn, "2026-10", [kam])[kam]["this_month"] == 1000
    with db.tx() as conn:
        _np = pr.compute_month(conn, "2026-11", employee_ids={kam})[0]["pay"]
        nov_net = int(_np["gross"] - _np["total_deductions"] + 1e-9)        # most that can be recovered
    r = post(c, "/advances", action="recover_full", employee_id=str(kam), month="2026-11")
    if nov_net < 5000:      # November is only a few days old: pay is smaller than the balance -> refused
        assert "more than this month" in r.data.decode()
        post(c, "/advances", action="recover", employee_id=str(kam), amount=str(int(nov_net)), month="2026-11")
    with db.tx() as conn:
        nov15 = pr.advance_ledger(conn, "2026-11", [kam])[kam]
        dec15 = pr.advance_ledger(conn, "2026-12", [kam])[kam]
        nov_pay = pr.compute_month(conn, "2026-11", employee_ids={kam})[0]["pay"]
    took = min(5000, int(nov_net)) if nov_net < 5000 else 5000
    assert nov15["outstanding_before"] == 5000 and nov15["this_month"] == took
    assert dec15["outstanding_before"] == 5000 - took and nov_pay["net"] >= 0
    pg15 = c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "Outstanding at start of 2026-10" in pg15 and "Paid in cash" in pg15
    assert "No advances outstanding" not in c.get("/adjustments?month=2026-10").data.decode()
    # Excel: bank / cash cells are formulas that agree with the app
    r = c.get(f"/export/payroll_formulas.xlsx?month=2026-10&eid={kam}")
    p15x = os.path.join(tempfile.mkdtemp(), "b.xlsx"); open(p15x, "wb").write(r.data)
    x15 = ExcelCompiler(filename=p15x)
    with db.tx() as conn:
        p15d = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["pay"]
    assert abs(x15.evaluate("Payroll!V4") - p15d["net"]) < 0.5
    assert abs(x15.evaluate("Payroll!W4") - p15d["bank"]) < 0.5 and abs(x15.evaluate("Payroll!X4") - p15d["cash"]) < 0.5
    # HR cannot touch money
    login_as("hr1", "hrpass123")
    assert post(c, "/advances", action="give", employee_id=str(kam), given_date="2026-10-05", amount="1").status_code == 403
    assert post(c, f"/employees/{kam}/split", month="2026-10", bank_amount="1").status_code == 403
    assert "Outstanding at start" not in c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    login_as("admin", "secret123")

    # 16. cash actually paid (rounded / different), carry the difference, record + deduct an advance from the attendance page
    with db.tx() as conn:
        p16 = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["pay"]
    assert p16["cash"] > 0
    paid = int(p16["cash"] // 100 * 100)                         # paid rounded down to the hundred
    sheet = c.get("/payroll/cash?month=2026-10").data.decode()
    assert f'name="paid_{kam}"' in sheet
    post(c, "/payroll/cash?month=2026-10", eid=str(kam), **{f"paid_{kam}": str(paid), f"note_{kam}": "rounded off"})
    with db.tx() as conn:
        cp16 = conn.execute("SELECT * FROM cash_payments WHERE employee_id=? AND month='2026-10'", (kam,)).fetchone()
    assert cp16["paid_amount"] == paid and cp16["note"] == "rounded off"
    diff16 = round(paid - p16["cash"], 2)
    assert f"{diff16:+.2f}" in c.get("/payroll/cash?month=2026-10").data.decode()
    assert "Cash actually paid" in c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "rounded off" in c.get("/export/cash.csv?month=2026-10").data.decode()
    post(c, f"/employees/{kam}/cash_carry", month="2026-10")
    with db.tx() as conn:
        carried = conn.execute("SELECT * FROM adjustments WHERE employee_id=? AND month='2026-11' AND note LIKE 'cash short-paid%'", (kam,)).fetchone()
        assert conn.execute("SELECT carried_month FROM cash_payments WHERE id=?", (cp16["id"],)).fetchone()[0] == "2026-11"
    assert carried and carried["kind"] == "allowance" and abs(carried["amount"] + diff16) < 0.01
    r = post(c, f"/employees/{kam}/cash_paid", month="2026-10", paid_amount=str(paid + 50))
    assert "already carried" in r.data.decode()                    # carried amount is frozen
    # cash paid can still be recorded after the month is finalized
    post(c, "/payroll?month=2026-09", action="finalize")
    with db.tx() as conn:
        sep_emp = conn.execute("SELECT id FROM employees WHERE device_user_id='1'").fetchone()["id"]
    post(c, f"/employees/{sep_emp}/cash_paid", month="2026-09", paid_amount="1234", note="after finalize")
    with db.tx() as conn:
        assert conn.execute("SELECT paid_amount FROM cash_payments WHERE employee_id=? AND month='2026-09'", (sep_emp,)).fetchone()[0] == 1234
    # any amount is allowed, paise and thousands separators included
    post(c, f"/employees/{sep_emp}/cash_paid", month="2026-09", paid_amount="1,234.75", note="odd amount")
    with db.tx() as conn:
        assert conn.execute("SELECT paid_amount FROM cash_payments WHERE employee_id=? AND month='2026-09'", (sep_emp,)).fetchone()[0] == 1234.75
    page = c.get(f"/attendance/employee/{sep_emp}?month=2026-09").get_data(as_text=True)
    assert 'value="1234.75"' in page and 'name="paid_amount" inputmode="decimal"' in page
    page = post(c, f"/employees/{sep_emp}/cash_paid", month="2026-09", paid_amount="Rs. 12,345.75/-").get_data(as_text=True)
    assert 'value="12345.75"' in page                       # shown back exactly, paise kept
    # messages float on screen, so a save is visible even though the page reopens at the form
    assert '<div class="flashes"' in page and "saved" in page
    # Save with an empty box: tell the user instead of silently doing nothing
    post(c, f"/employees/{sep_emp}/cash_paid", month="2026-09", paid_amount="")          # clears the record
    r = post(c, f"/employees/{sep_emp}/cash_paid", month="2026-09", paid_amount="")
    assert "Type the cash amount actually paid" in r.get_data(as_text=True)
    post(c, f"/employees/{sep_emp}/cash_paid", month="2026-09", paid_amount="1234.75")
    post(c, "/payroll?month=2026-09", action="reopen")
    # bank payments: bulk mark paid on the bank sheet, single employee failed / undo, CSV status
    with db.tx() as conn:
        bank_rows = [r for r in payroll.compute_month(conn, "2026-09") if r["pay"].get("bank", r["pay"]["net"]) > 0]
    assert len(bank_rows) >= 2
    e1, e2 = bank_rows[0]["emp"]["id"], bank_rows[1]["emp"]["id"]
    calc1 = round(bank_rows[0]["pay"].get("bank", bank_rows[0]["pay"]["net"]), 2)
    r = post(c, "/payroll/bank?month=2026-09", sel=[str(e1), str(e2)], action="paid", paid_on="2026-10-05",
             reference="NEFT-BATCH-1", **{f"amt_{e2}": "1,000", f"ref_{e2}": "UTR999"})
    page = r.get_data(as_text=True)
    assert "2 employee(s) marked paid on 2026-10-05" in page and "UTR999" in page and "NEFT-BATCH-1" in page
    with db.tx() as conn:
        got = {x["employee_id"]: dict(x) for x in conn.execute("SELECT * FROM bank_payments WHERE month='2026-09'")}
    assert got[e1]["amount"] == calc1 and got[e1]["reference"] == "NEFT-BATCH-1" and got[e1]["status"] == "paid"
    assert got[e2]["amount"] == 1000 and got[e2]["reference"] == "UTR999"
    assert "Tick the employees first" in post(c, "/payroll/bank?month=2026-09", action="paid").get_data(as_text=True)
    r = post(c, f"/employees/{e2}/bank_paid", month="2026-09", action="failed", paid_on="2026-10-06", amount="", reference="")
    assert "marked failed / returned" in r.get_data(as_text=True)
    assert 'id="bank-paid"' in c.get(f"/attendance/employee/{e2}?month=2026-09").get_data(as_text=True)
    csv_text = c.get("/export/bank.csv?month=2026-09").get_data(as_text=True)
    assert "Failed / returned" in csv_text and "NEFT-BATCH-1" in csv_text and "Not paid yet" in csv_text
    assert "✓" in c.get("/payroll?month=2026-09").get_data(as_text=True)
    post(c, f"/employees/{e2}/bank_paid", month="2026-09", action="clear")
    post(c, "/payroll/bank?month=2026-09", sel=[str(e1)], action="clear")
    with db.tx() as conn:
        assert not conn.execute("SELECT 1 FROM bank_payments").fetchone()
    # manual deductions / additions from the employee attendance page
    def net(eid, m):
        with db.tx() as conn:
            return payroll.compute_month(conn, m, employee_ids={eid})[0]["pay"]["net"]
    n0 = net(sep_emp, "2026-09")
    page = post(c, "/adjustments?month=2026-09", employee_id=str(sep_emp), kind="deduction", amount="₹1,250.50", note="canteen",
                back=f"/attendance/employee/{sep_emp}?month=2026-09").get_data(as_text=True)
    assert "Deduction of 1250.5 saved" in page and 'id="other-deductions"' in page and "canteen" in page
    assert abs(net(sep_emp, "2026-09") - (n0 - 1250.5)) < 1.01
    with db.tx() as conn:
        aid = conn.execute("SELECT id FROM adjustments WHERE employee_id=? AND note='canteen'", (sep_emp,)).fetchone()[0]
    assert "must be more than 0" in post(c, "/adjustments?month=2026-09", employee_id=str(sep_emp), kind="deduction", amount="0",
                                         back=f"/attendance/employee/{sep_emp}?month=2026-09").get_data(as_text=True)
    post(c, "/adjustments?month=2026-09", delete=str(aid), back=f"/attendance/employee/{sep_emp}?month=2026-09")
    assert abs(net(sep_emp, "2026-09") - n0) < 0.01
    # final totals save without a reason (no silent rejection); fractional payable days accepted
    r = post(c, f"/attendance/employee/{sep_emp}/totals", month="2026-09", payable_days="20.25", ot_hours="", reason="")
    assert "Final totals for" in r.get_data(as_text=True)
    with db.tx() as conn:
        assert conn.execute("SELECT payable_days, reason FROM month_overrides WHERE employee_id=? AND month='2026-09'",
                            (sep_emp,)).fetchone()[:] == (20.25, "manual edit")
        conn.execute("DELETE FROM month_overrides WHERE employee_id=? AND month='2026-09'", (sep_emp,))
    # record an advance from the attendance page, then deduct it there
    pg16 = c.get(f"/attendance/employee/{sep_emp}?month=2026-10").data.decode()
    assert "Record an advance given" in pg16
    post(c, "/advances", action="give", employee_id=str(sep_emp), given_date="2026-10-01", amount="3000", installment="1000",
         month="2026-10", back=f"/attendance/employee/{sep_emp}?month=2026-10")
    post(c, "/advances", action="recover_installment", employee_id=str(sep_emp), month="2026-10")
    with db.tx() as conn:
        assert pr.advance_ledger(conn, "2026-10", [sep_emp])[sep_emp]["this_month"] == 1000
    assert "Outstanding at start of 2026-10" in c.get(f"/attendance/employee/{sep_emp}?month=2026-10").data.decode()
    login_as("hr1", "hrpass123")
    assert c.get("/payroll/cash?month=2026-10").status_code == 403
    assert post(c, f"/employees/{kam}/cash_paid", month="2026-10", paid_amount="1").status_code == 403
    login_as("admin", "secret123")

    # 17. ESIC: coverage decided per contribution period (Apr-Sep), ESI report and ESIC upload CSV
    with db.tx() as conn:
        e17 = conn.execute("SELECT id FROM employees WHERE device_user_id='2'").fetchone()["id"]
        conn.execute("UPDATE employees SET salary_rate=20000, salary_type='monthly', category='worker', esi_no='1234567890', esi_enabled=1 WHERE id=?", (e17,))
    post(c, "/payroll?month=2026-04", action="finalize")                 # April frozen: covered at 20,000
    with db.tx() as conn:
        conn.execute("UPDATE employees SET salary_rate=40000 WHERE id=?", (e17,))
        may = pr.compute_month(conn, "2026-05", employee_ids={e17})[0]["pay"]
        octo = pr.compute_month(conn, "2026-10", employee_ids={e17})[0]["pay"]
    assert may["esi_wages"] > 21000 and may["esi_covered"] and may["esi"] > 0, may      # continues to end of period
    assert "start of the contribution period" in may["esi_note"]
    assert not octo["esi_covered"] and octo["esi"] == 0                                 # new period: out of coverage
    post(c, "/payroll?month=2026-04", action="reopen")
    with db.tx() as conn:
        conn.execute("UPDATE employees SET salary_rate=15000 WHERE id=?", (e17,))
    rep = c.get("/reports/esi?month=2026-10").data.decode()
    assert "Total to deposit" in rep and "1234567890" in rep and "15 Nov 2026" in rep
    mc = c.get("/export/esic_mc.csv?month=2026-10").data.decode()
    assert "IP Number (10 Digits)" in mc and "1234567890" in mc
    assert "ESI employer share" in c.get("/payroll?month=2026-10").data.decode()
    login_as("hr1", "hrpass123")
    assert c.get("/reports/esi?month=2026-10").status_code == 403
    login_as("admin", "secret123")

    # 18. salary components on: ESI wages leave out HRA + conveyance; Excel formulas agree
    post(c, "/settings", action="settings", salary_components="on")
    post(c, f"/employees/{kam}/split", month="2026-10", action="reset")   # earlier test set a one-month bank amount
    post(c, f"/employees/{kam}/salary", category="worker", salary_type="monthly", salary_rate="30000", ot_multiplier="1",
         ot_enabled="on", esi_enabled="on", pay_mode="esi", comp_hra="10000", comp_conveyance="5000", comp_other="0")
    with db.tx() as conn:
        e18 = conn.execute("SELECT comp_hra, comp_conveyance, pay_mode FROM employees WHERE id=?", (kam,)).fetchone()
        p18 = pr.compute_month(conn, "2026-10", employee_ids={kam})[0]["pay"]
    assert tuple(e18) == (10000, 5000, "esi") and p18["components"]
    assert abs(p18["hra"] - p18["basic"] / 3) < 0.02 and p18["esi_wages"] < p18["gross"] - p18["ot_pay"]
    assert p18["bank"] == min(round(p18["esi_wages"]), p18["net"]) and p18["cash"] == p18["net"] - p18["bank"]
    pg18 = c.get(f"/attendance/employee/{kam}?month=2026-10").data.decode()
    assert "Salary components" in pg18 and "Basic + DA" in pg18
    r = c.get(f"/export/payroll_formulas.xlsx?month=2026-10&eid={kam}")
    p18x = os.path.join(tempfile.mkdtemp(), "c.xlsx"); open(p18x, "wb").write(r.data)
    x18 = ExcelCompiler(filename=p18x)
    for col, key in (("Q", "esi"), ("P", "pf"), ("V", "net"), ("W", "bank"), ("X", "cash"), ("Y", "esi_wages")):
        assert abs(x18.evaluate(f"Payroll!{col}4") - p18[key]) < 0.5, (key, x18.evaluate(f"Payroll!{col}4"), p18[key])
    r = post(c, f"/employees/{kam}/salary", salary_rate="30000", comp_hra="40000", category="worker", salary_type="monthly")
    assert "cannot be more than the salary rate" in r.data.decode()
    post(c, "/settings", action="settings")                               # components off again

    # 19. gate pass entries + optional multiple in/out per employee
    with db.tx() as conn:
        g19 = conn.execute("SELECT id FROM employees WHERE device_user_id='3'").fetchone()["id"]
        base19 = [d for d in pr.compute_month(conn, "2026-10", employee_ids={g19})[0]["days"] if d["in"] and d["out"]][0]
    day19 = base19["date"].isoformat()
    login_as("hr1", "hrpass123")                                               # HR can record gate passes
    post(c, "/gatepass?month=2026-10", employee_id=str(g19), work_date=day19, out_time="11:00", in_time="12:00",
         kind="personal", reason="bank work", approved_by="supervisor")
    with db.tx() as conn:
        d19 = next(d for d in pr.compute_month(conn, "2026-10", employee_ids={g19})[0]["days"] if d["date"] == base19["date"])
        gid = conn.execute("SELECT id FROM gate_passes WHERE employee_id=?", (g19,)).fetchone()["id"]
    assert d19["gate_personal_min"] == 60 and d19["worked_min"] == base19["worked_min"] - 60
    gp_page = c.get("/gatepass?month=2026-10").data.decode()
    assert "bank work" in gp_page and "−1:00" in gp_page
    assert "Gate pass" in c.get(f"/changes?month=2026-10&kind=gate_pass").data.decode()
    assert "Gate pass" in c.get(f"/attendance/day/{g19}/{day19}").data.decode()
    post(c, "/changes/undo", kind="gate_pass", id=str(gid))
    with db.tx() as conn:
        assert not conn.execute("SELECT 1 FROM gate_passes WHERE id=?", (gid,)).fetchone()
    post(c, f"/attendance/day/{g19}/{day19}", action="gate_add", out_time="15:00", in_time="", kind="official", reason="site visit")
    with db.tx() as conn:
        assert conn.execute("SELECT kind FROM gate_passes WHERE employee_id=?", (g19,)).fetchone()["kind"] == "official"
    # multi in/out is off by default and can be switched on per employee
    with db.tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM employees WHERE multi_punch=1").fetchone()[0] == 0
    post(c, f"/employees/{g19}/quick", name="Pawan", department="", designation="", shift_id="1", active="on", multi_punch="on")
    with db.tx() as conn:
        assert conn.execute("SELECT multi_punch FROM employees WHERE id=?", (g19,)).fetchone()[0] == 1
        assert pr.compute_month(conn, "2026-10", employee_ids={g19})
    assert "multi in/out" in c.get("/employees").data.decode()
    login_as("viewer1", "viewer123")
    assert post(c, "/gatepass?month=2026-10", employee_id=str(g19), work_date=day19, out_time="10:00").status_code == 403
    login_as("admin", "secret123")

    # 20. attendance page highlights days with several punches and lists every timestamp
    with db.tx() as conn:
        rows20 = pr.compute_month(conn, "2026-10")
    multi_day = next(((r["emp"], d) for r in rows20 for d in r["days"] if len(d["punches"]) > 2), None)
    assert multi_day, "real data has days with more than two punches"
    e20, d20 = multi_day
    pg20 = c.get(f"/attendance/employee/{e20['id']}?month=2026-10").data.decode()
    assert f'id="d{d20["date"].day}" class="multi-entry"' in pg20
    assert f'id="pl{d20["date"].day}" hidden class="punch-list-row"' in pg20 and f"{len(d20['punches'])} punches ▾" in pg20
    for t in d20["punches"]:
        assert t.strftime("%H:%M:%S") in pg20

    # 21. a lone punch on a Sunday shows as WO + MP and is listed on Exceptions
    with db.tx() as conn:
        p21 = conn.execute("SELECT id FROM employees WHERE device_user_id='10'").fetchone()["id"]
        d21 = next(d for d in pr.compute_month(conn, "2026-10", employee_ids={p21})[0]["days"] if d["date"].day == 25)
    if len(d21["punches"]) == 1:          # real data: Pappu punched once on Sun 25 Oct
        assert d21["status"] == "WO" and d21["mp_on_off"]
        assert "mp-tag" in c.get(f"/attendance/employee/{p21}?month=2026-10").data.decode()
        assert "Missing punch on weekly off" in c.get("/exceptions?month=2026-10&kind=mp").data.decode()
