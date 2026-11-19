"""The read-only shop-floor link used by ToolTrack: token-protected, employees + attendance, no pay data."""
import datetime as dt

import pytest

from payroll import db


@pytest.fixture()
def client(tmp_path):
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path / "t.db")
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


def test_shopfloor_people(client):
    c = client
    today = dt.date.today()
    with db.tx() as conn:
        conn.execute("INSERT INTO employees (device_user_id, emp_code, name, salary_rate, bank_account) VALUES ('7', 'E7', 'Ravi', 25000, '999')")
        conn.execute("INSERT INTO employees (device_user_id, emp_code, name) VALUES ('8', 'E8', 'Suresh')")
        conn.execute("INSERT INTO punches (device_user_id, ts) VALUES ('7', ?)", (f"{today} 08:55:00",))
    # off until an admin switches it on
    assert c.get("/api/shopfloor/people").status_code == 403
    c.post("/settings", data={"action": "shopfloor_token", "csrf": "t"})
    with db.tx() as conn:
        token = conn.execute("SELECT value FROM settings WHERE key = 'shopfloor_token'").fetchone()[0]
    assert token
    anon = c.application.test_client()          # ToolTrack has no payroll session, only the token
    assert anon.get("/api/shopfloor/people", headers={"Authorization": "Bearer nope"}).status_code == 403
    r = anon.get("/api/shopfloor/people", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    people = {p["name"]: p for p in r.get_json()["people"]}
    assert people["Ravi"]["present"] and people["Ravi"]["in"] == "08:55" and people["Ravi"]["code"] == "E7"
    assert not people["Suresh"]["present"]
    assert "salary_rate" not in people["Ravi"] and "bank_account" not in people["Ravi"]
    # switching off stops it
    c.post("/settings", data={"action": "shopfloor_token", "off": "1", "csrf": "t"})
    assert anon.get("/api/shopfloor/people", headers={"Authorization": f"Bearer {token}"}).status_code == 403


class _FakeRec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeDevice:
    """Stands in for the attendance machine: one employee punched in this morning."""
    def __init__(self, when):
        self.when = when
    def disable_device(self): pass
    def enable_device(self): pass
    def disconnect(self): pass
    def get_serialnumber(self): return "TEST123"
    def get_users(self): return [_FakeRec(user_id="21", name="New Joiner", privilege=0)]
    def get_attendance(self): return [_FakeRec(user_id="21", timestamp=self.when, status=1, punch=0)]


def test_shopfloor_sync_reads_machines(client, monkeypatch):
    c = client
    from payroll import device
    when = dt.datetime.combine(dt.date.today(), dt.time(8, 50))
    monkeypatch.setattr(device, "_open", lambda dev, timeout=20: _FakeDevice(when))
    with db.tx() as conn:
        conn.execute("DELETE FROM devices")
        conn.execute("INSERT INTO devices (name, ip, active) VALUES ('Gate', '10.0.0.9', 1)")
    c.post("/settings", data={"action": "shopfloor_token", "csrf": "t"})
    with db.tx() as conn:
        token = conn.execute("SELECT value FROM settings WHERE key = 'shopfloor_token'").fetchone()[0]
    anon = c.application.test_client()                 # no session, no form token: only the Bearer token
    assert anon.post("/api/shopfloor/sync").status_code == 403
    r = anon.post("/api/shopfloor/sync", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.data[:300]
    d = r.get_json()
    assert d["machines"] and d["machines"][0]["ok"], d["machines"]
    p = {x["name"]: x for x in d["people"]}["New Joiner"]          # read from the machine just now
    assert p["present"] and p["in"] == "08:50"
    # asked again straight away: the machines are not read again
    d2 = anon.post("/api/shopfloor/sync", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert "less than a minute ago" in d2["machines"][0]["message"]
