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
