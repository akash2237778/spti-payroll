"""Flask web application for SPTI Payroll."""
import csv
import datetime as dt
import functools
import io
import math
import os
import secrets

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import changes as chg, db, device, engine, payroll, reports

app = Flask(__name__)


def _secret_key():
    path = os.path.join(os.path.dirname(db.DB_PATH), "secret.key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.config["SECRET_KEY"] = os.environ.get("PAYROLL_SECRET") or _secret_key()
app.jinja_env.globals.update(fmt_min=engine.fmt_min, PAY_MODES=payroll.PAY_MODES, day_credit=reports.day_credit, shift_times=engine.shift_bounds, std_minutes=engine.standard_minutes,STATUS_LABELS=engine.STATUS_LABELS,
                             OVERRIDE_STATUSES=engine.OVERRIDE_STATUSES, WEEKDAYS=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])


@app.template_filter("money")
def money(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return v


@app.template_filter("dayname")
def dayname(v):
    try:
        return dt.date.fromisoformat(str(v)[:10]).strftime("%a")
    except ValueError:
        return ""


@app.template_filter("days")
def days_fmt(v):
    v = float(v or 0)
    return f"{v:g}"


# ------------------------------------------------------------------ request plumbing / auth

@app.before_request
def before():
    g.conn = db.connect()
    g.settings = db.get_settings(g.conn)
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST" and request.endpoint not in ("static",):
        if request.form.get("csrf") != session.get("csrf"):
            abort(400, "Invalid form token, please reload the page")
    g.user = None
    if session.get("uid"):
        g.user = g.conn.execute("SELECT * FROM app_users WHERE id = ?", (session["uid"],)).fetchone()
    open_endpoints = ("login", "setup", "static", "healthz")
    if request.endpoint in open_endpoints:
        return
    if not g.conn.execute("SELECT 1 FROM app_users").fetchone():
        return redirect(url_for("setup"))
    if not g.user:
        return redirect(url_for("login", next=request.path))


@app.teardown_request
def teardown(exc):
    conn = g.pop("conn", None)
    if conn is not None:
        if exc is None:
            conn.commit()
        conn.close()


@app.context_processor
def inject():
    return {"user": g.get("user"), "settings": g.get("settings", {}), "csrf": session.get("csrf"),
            "cur": g.get("settings", {}).get("currency", "₹"),
            "can_pay": can_pay(), "can_edit": can_edit(), "is_admin": is_admin(), "ROLES": ROLES,
            "emp_sort": emp_sort() if g.get("user") else "id", "url_with": url_with,
            "printed_at": dt.datetime.now().strftime("%d %b %Y %H:%M")}


# ------------------------------------------------------------------ employee sort order (ID or name)

def emp_sort():
    """Current sort for employee lists. ?sort=id|name on any page changes it and is remembered for the session."""
    want = request.args.get("sort")
    if want in ("id", "name"):
        session["emp_sort"] = want
    return session.get("emp_sort", "id")


def emp_order_sql(alias=""):
    a = f"{alias}." if alias else ""
    by_id = f"CAST({a}device_user_id AS INTEGER)"
    return f"LOWER({a}name), {by_id}" if emp_sort() == "name" else by_id


def sort_rows(rows):
    """Sort computed rows ({'emp': ...}) the same way as the SQL lists."""
    def key(r):
        e = r["emp"]
        try:
            num = int(e["device_user_id"])
        except (TypeError, ValueError):
            num = 10 ** 9
        return ((e["name"] or "").lower(), num) if emp_sort() == "name" else (num, (e["name"] or "").lower())
    return sorted(rows, key=key)


def url_with(**kw):
    """The current page's URL with some query arguments replaced (used by the sort switch)."""
    args = {**(request.view_args or {}), **request.args.to_dict(), **kw}
    return url_for(request.endpoint, **args)


# ------------------------------------------------------------------ roles
# admin  : everything, including salaries, payroll, settings and users
# hr     : all attendance work (punch fixes, leaves, holidays, shifts, employees) but no money anywhere
# viewer : read-only attendance, no money
ROLES = {"admin": "Admin - everything", "hr": "HR - attendance, no salaries", "viewer": "Viewer - read only, no salaries"}
PAY_ROLES = {"admin"}
EDIT_ROLES = {"admin", "hr"}
# Employee fields only someone who may see pay can read or change.
PAY_FIELDS = ["category", "salary_type", "salary_rate", "ot_enabled", "ot_multiplier", "pf_enabled", "esi_enabled", "disabled_person",
              "bank_name", "bank_account", "ifsc", "uan", "esi_no", "notes", "pay_mode", "bank_value",
              "comp_hra", "comp_conveyance", "comp_other"]


def _role():
    return g.user["role"] if g.get("user") else None


def is_admin():
    return _role() == "admin"


def can_pay():
    return _role() in PAY_ROLES


def can_edit():
    return _role() in EDIT_ROLES


def require_admin():
    if not is_admin():
        abort(403, "Admin access required")


def require_pay():
    if not can_pay():
        abort(403, "Salary information is restricted to admin users")


def require_edit():
    if not can_edit():
        abort(403, "Your account is read-only")


def _guard(check):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            check()
            return fn(*a, **kw)
        return wrapper
    return deco


admin_required = _guard(require_admin)
pay_required = _guard(require_pay)
edit_required = _guard(require_edit)


def audit(action, detail=""):
    db.audit(g.conn, g.user["username"] if g.user else "-", action, detail)


def cur_month():
    m = request.args.get("month") or dt.date.today().strftime("%Y-%m")
    try:
        dt.datetime.strptime(m, "%Y-%m")
    except ValueError:
        abort(400, "Bad month")
    return m


def month_nav(month):
    d = dt.datetime.strptime(month, "%Y-%m").date()
    prev = (d - dt.timedelta(days=1)).strftime("%Y-%m")
    nxt = (d + dt.timedelta(days=32)).strftime("%Y-%m")
    return prev, nxt


def ensure_unlocked(date_or_month):
    if payroll.is_locked(g.conn, date_or_month):
        abort(409, f"Payroll for {date_or_month[:7]} is finalized; reopen it before changing attendance.")


@app.route("/healthz")
def healthz():
    """Container health check: the database answers."""
    g.conn.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if g.conn.execute("SELECT 1 FROM app_users").fetchone():
        return redirect(url_for("login"))
    if request.method == "POST":
        u, p = request.form["username"].strip(), request.form["password"]
        if len(u) < 3 or len(p) < 6:
            flash("Username min 3 chars, password min 6 chars", "error")
        else:
            g.conn.execute("INSERT INTO app_users(username, password_hash, role) VALUES (?,?, 'admin')",
                           (u, generate_password_hash(p)))
            flash("Admin account created. Please sign in.", "ok")
            return redirect(url_for("login"))
    return render_template("login.html", setup=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = g.conn.execute("SELECT * FROM app_users WHERE username = ?", (request.form["username"].strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session["uid"] = u["id"]
            session["csrf"] = secrets.token_hex(16)
            db.audit(g.conn, u["username"], "login")
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        flash("Invalid username or password", "error")
    return render_template("login.html", setup=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ dashboard

PRESENCE_GROUPS = [(0, "Present"), (1, "Missing punch"), (2, "Absent / not in yet"), (3, "Off / holiday / leave")]


def presence_group(r):
    """(order, label) for the Today screen's 'Present first' sort."""
    if r["status"] == "MP":
        return PRESENCE_GROUPS[1]
    if r["punches"] or r["status"] in ("P", "HD", "WOP", "HP"):
        return PRESENCE_GROUPS[0]
    if r["status"] in ("WO", "H", "L", "LWP"):
        return PRESENCE_GROUPS[3]
    return PRESENCE_GROUPS[2]


@app.route("/")
def dashboard():
    today = dt.date.today()
    day = request.args.get("date")
    day = dt.date.fromisoformat(day) if day else today
    emps = [dict(e) for e in g.conn.execute("SELECT * FROM employees WHERE active = 1 ORDER BY " + emp_order_sql())]
    data = engine.employee_days(g.conn, emps, day, day, g.settings)
    rows = []
    stats = {"total": len(emps), "present": 0, "absent": 0, "late": 0, "missing": 0, "off": 0}
    for e in emps:
        r = data[e["id"]]["days"][0]
        rows.append((e, r))
        if r["punches"] or r["status"] in ("P", "HD"):
            stats["present"] += 1
        elif r["status"] in ("WO", "H", "L"):
            stats["off"] += 1
        else:
            stats["absent"] += 1
        if r["late_min"] or (r["in"] and len(r["punches"]) == 1 and
                             (r["in"] - engine.shift_bounds(day, r["shift"])[0]).total_seconds() / 60
                             > int(r["shift"]["grace_in_min"] or 0)):
            stats["late"] += 1
        if r["status"] == "MP":
            stats["missing"] += 1
    # "Present first": group by attendance, keeping the ID / name order inside each group
    want = request.args.get("order")
    if want in ("status", "list"):
        session["today_order"] = want
    today_order = session.get("today_order", "list")
    groups = [presence_group(r) for _, r in rows]
    if today_order == "status":
        order = sorted(range(len(rows)), key=lambda i: groups[i][0])
        rows, groups = [rows[i] for i in order], [groups[i] for i in order]
    group_counts = {}
    for gk in groups:
        group_counts[gk] = group_counts.get(gk, 0) + 1
    devices = g.conn.execute("SELECT * FROM devices").fetchall()
    last_punch = g.conn.execute("SELECT MAX(ts) FROM punches").fetchone()[0]
    total_punches = g.conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0]
    holiday = g.conn.execute("SELECT * FROM holidays WHERE hdate = ?", (day.isoformat(),)).fetchone()
    return render_template("dashboard.html", rows=rows, stats=stats, day=day, devices=devices, holiday=holiday,
                           today_order=today_order, groups=groups, group_counts=group_counts,
                           edited=edited_set(day, day),
                           last_punch=last_punch, total_punches=total_punches,
                           prev=(day - dt.timedelta(days=1)).isoformat(), nxt=(day + dt.timedelta(days=1)).isoformat())


# ------------------------------------------------------------------ devices

@app.route("/devices", methods=["GET", "POST"])
def devices():
    if request.method == "POST":
        require_admin()
        f = request.form
        if f.get("id"):
            g.conn.execute("UPDATE devices SET name=?, ip=?, port=?, password=?, active=? WHERE id=?",
                           (f["name"], f["ip"].strip(), int(f["port"] or 4370), int(f["password"] or 0),
                            1 if f.get("active") else 0, int(f["id"])))
        else:
            g.conn.execute("INSERT INTO devices(name, ip, port, password) VALUES (?,?,?,?)",
                           (f["name"], f["ip"].strip(), int(f["port"] or 4370), int(f["password"] or 0)))
        audit("device_save", dict(f))
        flash("Device saved", "ok")
        return redirect(url_for("devices"))
    return render_template("devices.html", devices=g.conn.execute("SELECT * FROM devices").fetchall())


@app.route("/devices/<int:did>/<action>", methods=["POST"])
@edit_required
def device_action(did, action):
    dev = g.conn.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone() or abort(404)
    g.conn.commit()  # release locks before the long sync opens its own connection
    try:
        if action in ("settime", "delete"):
            require_admin()
        if action == "test":
            info = device.test_connection(dev)
            flash(f"Connected: serial {info['serial']}, firmware {info['firmware']}, {info['users']} users, device time {info['device_time']}", "ok")
        elif action == "sync":
            res = device.sync_device(did, g.user["username"])
            flash(res.get("message") or res.get("error"), "ok" if res["ok"] else "error")
        elif action == "settime":
            device.set_device_time(dev)
            audit("device_set_time", dev["ip"])
            flash("Device clock set to this computer's time", "ok")
        elif action == "delete":
            g.conn.execute("DELETE FROM devices WHERE id = ?", (did,))
            flash("Device removed (its punches are kept)", "ok")
        else:
            abort(404)
    except Exception as e:
        flash(f"Device error: {e}", "error")
    return redirect(request.form.get("back") or url_for("devices"))


# ------------------------------------------------------------------ employees

EMP_FIELDS = ["device_user_id", "emp_code", "name", "department", "designation", "phone", "join_date", "exit_date",
              "shift_id", "category", "salary_type", "salary_rate", "ot_multiplier", "bank_name", "bank_account", "ifsc", "uan",
              "esi_no", "notes", "pay_mode", "bank_value", "comp_hra", "comp_conveyance", "comp_other"]
EMP_FLAGS = ["active", "ot_enabled", "pf_enabled", "esi_enabled", "paid_break", "disabled_person", "multi_punch"]


@app.route("/employees")
def employees():
    show = request.args.get("show", "active")
    q = "SELECT e.*, s.name AS shift_name FROM employees e LEFT JOIN shifts s ON s.id = e.shift_id"
    if show == "active":
        q += " WHERE e.active = 1"
    elif show == "inactive":
        q += " WHERE e.active = 0"
    rows = g.conn.execute(q + " ORDER BY " + emp_order_sql("e")).fetchall()
    last = {r["device_user_id"]: r["m"] for r in g.conn.execute("SELECT device_user_id, MAX(ts) m FROM punches GROUP BY device_user_id")}
    return render_template("employees.html", rows=rows, show=show, last=last, shifts=g.conn.execute("SELECT * FROM shifts").fetchall())


@app.route("/employees/<int:eid>/quick", methods=["POST"])
@edit_required
def employee_quick(eid):
    """Inline edit from the employees list: the handful of fields people change often."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    name = f.get("name", "").strip() or emp["name"]
    if can_pay():
        stype = f.get("salary_type") if f.get("salary_type") in ("monthly", "daily", "hourly") else emp["salary_type"]
        try:
            rate = float(f.get("salary_rate") or 0)
            mult = float(f.get("ot_multiplier") or 1)
        except ValueError:
            flash("Rate and OT multiplier must be numbers", "error")
            return redirect(request.referrer or url_for("employees"))
        ot_on = 1 if f.get("ot_enabled") else 0
        category = f.get("category") if f.get("category") in ("worker", "staff") else (emp["category"] or "worker")
    else:  # HR / viewer: pay fields are neither shown nor changeable
        stype, rate, mult, ot_on = emp["salary_type"], emp["salary_rate"], emp["ot_multiplier"], emp["ot_enabled"]
        category = emp["category"] or "worker"
    vals = (name, f.get("department", "").strip(), f.get("designation", "").strip(), int(f.get("shift_id") or 0) or None,
            stype, rate, ot_on, mult, 1 if f.get("active") else 0, 1 if f.get("paid_break") else 0, category,
            1 if f.get("multi_punch") else 0, eid)
    g.conn.execute("""UPDATE employees SET name=?, department=?, designation=?, shift_id=?, salary_type=?, salary_rate=?,
                      ot_enabled=?, ot_multiplier=?, active=?, paid_break=?, category=?, multi_punch=? WHERE id=?""", vals)
    detail = {"id": eid, "name": name, "dept": vals[1], "shift": vals[3], "active": vals[8], "paid_break": vals[9],
              "multi_punch": vals[11]}
    if can_pay():
        detail.update(type=stype, rate=rate, ot=ot_on, mult=mult, category=category)
    audit("employee_quick_edit", detail)
    flash(f"Saved {name}", "ok")
    back = f.get("back", "")
    return redirect((back if back.startswith("/") and not back.startswith("//") else url_for("employees")) + f"#emp-{eid}")


@app.route("/employees/<int:eid>", methods=["GET", "POST"])
@app.route("/employees/new", methods=["GET", "POST"], defaults={"eid": None})
def employee_edit(eid):
    emp = None
    if eid:
        emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    if request.method == "POST":
        require_edit()
        f = request.form
        vals = {k: (f.get(k, "").strip() or None) for k in EMP_FIELDS}
        if not vals["name"] or not vals["device_user_id"]:
            flash("Name and Device User ID are required", "error")
            return redirect(request.url)
        vals["salary_rate"] = float(vals["salary_rate"] or 0)
        vals["ot_multiplier"] = float(vals["ot_multiplier"] or 1)
        vals["salary_type"] = vals["salary_type"] if vals["salary_type"] in ("monthly", "daily", "hourly") else "monthly"
        vals["category"] = vals["category"] if vals["category"] in ("worker", "staff") else "worker"
        vals["pay_mode"] = vals["pay_mode"] if vals["pay_mode"] in payroll.PAY_MODES else "bank"
        vals["bank_value"] = max(0.0, float(vals["bank_value"] or 0))
        for k in ("comp_hra", "comp_conveyance", "comp_other"):
            vals[k] = max(0.0, float(vals[k] or 0))
        if can_pay() and vals["comp_hra"] + vals["comp_conveyance"] + vals["comp_other"] > vals["salary_rate"] + 0.005:
            flash("HRA + conveyance + other allowance cannot be more than the salary rate", "error")
            return redirect(request.url)
        vals["shift_id"] = int(vals["shift_id"]) if vals["shift_id"] else None
        for k in EMP_FLAGS:
            vals[k] = 1 if f.get(k) else 0
        vals["weekly_off"] = ",".join(f.getlist("weekly_off"))
        for k in ("department", "designation", "phone", "bank_name", "bank_account", "ifsc", "uan", "esi_no", "notes"):
            vals[k] = vals[k] or ""
        if not can_pay():
            for k in PAY_FIELDS:  # HR cannot see or change pay; existing values stay, new hires get defaults
                vals.pop(k, None)
        cols = list(vals)
        try:
            if eid:
                g.conn.execute(f"UPDATE employees SET {', '.join(c + '=?' for c in cols)} WHERE id = ?", [vals[c] for c in cols] + [eid])
            else:
                cur = g.conn.execute(f"INSERT INTO employees({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [vals[c] for c in cols])
                eid = cur.lastrowid
        except Exception as e:
            flash(f"Could not save: {e}", "error")
            return redirect(request.url)
        detail = {"id": eid, "name": vals["name"]}
        if can_pay():
            detail.update(rate=vals["salary_rate"], type=vals["salary_type"])
        audit("employee_save", detail)
        flash("Employee saved", "ok")
        return redirect(url_for("employees"))
    shifts = g.conn.execute("SELECT * FROM shifts").fetchall()
    return render_template("employee_edit.html", emp=emp, shifts=shifts, history=shift_history(eid) if eid else [])


def shift_history(eid):
    return g.conn.execute("""SELECT a.*, s.name AS shift_name, s.start_time, s.end_time FROM employee_shifts a
                             JOIN shifts s ON s.id = a.shift_id WHERE a.employee_id = ? ORDER BY a.from_date DESC, a.id DESC""",
                          (eid,)).fetchall()


def _ensure_range_unlocked(from_date, to_date):
    a = dt.date.fromisoformat(from_date)
    b = dt.date.fromisoformat(to_date) if to_date else dt.date.today()
    for r in g.conn.execute("SELECT month FROM payroll_runs WHERE status = 'final'"):
        if a.strftime("%Y-%m") <= r["month"] <= b.strftime("%Y-%m"):
            abort(409, f"Payroll for {r['month']} is finalized; reopen it before changing shifts in that month.")


@app.route("/employees/<int:eid>/shifts", methods=["POST"])
@edit_required
def employee_shifts(eid):
    """Dated shift roster. Several changes per month are allowed; on overlap the newest entry wins."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    if f.get("delete"):
        row = g.conn.execute("SELECT * FROM employee_shifts WHERE id=? AND employee_id=?", (int(f["delete"]), eid)).fetchone() or abort(404)
        _ensure_range_unlocked(row["from_date"], row["to_date"])
        g.conn.execute("DELETE FROM employee_shifts WHERE id = ?", (row["id"],))
        audit("shift_assign_delete", {"emp": eid, "from": row["from_date"], "to": row["to_date"], "shift": row["shift_id"]})
        flash("Shift change removed; attendance recalculated", "ok")
    else:
        frm, to = f["from_date"], f.get("to_date") or None
        if to and to < frm:
            flash("'To' date must be on or after 'From' date", "error")
            return redirect(f.get("back") or url_for("att_employee", eid=eid))
        _ensure_range_unlocked(frm, to)
        shift = g.conn.execute("SELECT * FROM shifts WHERE id = ?", (int(f["shift_id"]),)).fetchone() or abort(400)
        g.conn.execute("INSERT INTO employee_shifts(employee_id, shift_id, from_date, to_date, reason, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                       (eid, shift["id"], frm, to, f.get("reason", ""), g.user["username"], dt.datetime.now().strftime(TS_FMT)))
        audit("shift_assign", {"emp": eid, "name": emp["name"], "shift": shift["name"], "from": frm, "to": to, "reason": f.get("reason")})
        flash(f"{emp['name']}: {shift['name']} shift from {frm}{' to ' + to if to else ' onwards'}. Punches re-paired.", "ok")
    return redirect(f.get("back") or url_for("att_employee", eid=eid))


@app.route("/employees/<int:eid>/salary", methods=["POST"])
@pay_required
def employee_salary(eid):
    """Edit an employee's salary settings from the attendance page (applies to every month not yet finalized)."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    try:
        rate = float(f.get("salary_rate") or 0)
        mult = float(f.get("ot_multiplier") or 1)
        if rate < 0 or mult < 0:
            raise ValueError
    except ValueError:
        flash("Rate and OT multiplier must be positive numbers", "error")
        return redirect(f.get("back") or url_for("att_employee", eid=eid))
    vals = {"category": f.get("category") if f.get("category") in ("worker", "staff") else "worker",
            "salary_type": f.get("salary_type") if f.get("salary_type") in ("monthly", "daily", "hourly") else "monthly",
            "salary_rate": rate, "ot_enabled": 1 if f.get("ot_enabled") else 0, "ot_multiplier": mult,
            "pf_enabled": 1 if f.get("pf_enabled") else 0, "esi_enabled": 1 if f.get("esi_enabled") else 0,
            "pay_mode": f.get("pay_mode") if f.get("pay_mode") in payroll.PAY_MODES else (emp["pay_mode"] or "bank"),
            "bank_value": max(0.0, float(f.get("bank_value") or 0))}
    if g.settings.get("salary_components") == "1":
        for k in ("comp_hra", "comp_conveyance", "comp_other"):
            vals[k] = max(0.0, float(f.get(k) or 0))
        if vals["comp_hra"] + vals["comp_conveyance"] + vals["comp_other"] > rate + 0.005:
            flash("HRA + conveyance + other allowance cannot be more than the salary rate", "error")
            return redirect(f.get("back") or url_for("att_employee", eid=eid))
    before = {k: emp[k] for k in vals}
    g.conn.execute(f"UPDATE employees SET {', '.join(k + '=?' for k in vals)} WHERE id=?", list(vals.values()) + [eid])
    audit("employee_salary_edit", {"id": eid, "name": emp["name"], "from": before, "to": vals})
    flash(f"Salary settings for {emp['name']} saved; unfinalized months recalculated", "ok")
    back = f.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("att_employee", eid=eid))


@app.route("/employees/<int:eid>/split", methods=["POST"])
@pay_required
def pay_split(eid):
    """Bank amount for one month (rest of net pay in cash); reset = back to the employee default."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    month = f["month"]
    dt.datetime.strptime(month, "%Y-%m")
    ensure_unlocked(month)
    back = f.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("att_employee", eid=eid, month=month)
    if f.get("action") == "reset" or not f.get("bank_amount", "").strip():
        g.conn.execute("DELETE FROM pay_splits WHERE employee_id=? AND month=?", (eid, month))
        flash(f"{emp['name']}: bank / cash back to the default ({payroll.PAY_MODES.get(emp['pay_mode'] or 'bank')})", "ok")
    else:
        try:
            amount = float(f["bank_amount"])
            assert amount >= 0
        except (ValueError, AssertionError):
            flash("Bank amount must be a positive number", "error")
            return redirect(back)
        g.conn.execute("""INSERT INTO pay_splits(employee_id, month, bank_amount, note, created_by, created_at) VALUES (?,?,?,?,?,?)
                          ON CONFLICT(employee_id, month) DO UPDATE SET bank_amount=excluded.bank_amount, note=excluded.note,
                          created_by=excluded.created_by, created_at=excluded.created_at""",
                       (eid, month, amount, f.get("note", ""), g.user["username"], dt.datetime.now().strftime(TS_FMT)))
        flash(f"{emp['name']} {month}: {g.settings.get('currency', '₹')}{amount:,.0f} by bank, the rest in cash", "ok")
    audit("pay_split", {"emp": eid, "month": month, "bank": f.get("bank_amount"), "action": f.get("action")})
    return redirect(back)


def set_advance_recovery(emp, month, amount, note=""):
    """Replace this month's advance recovery for an employee. Returns an error message or None."""
    ensure_unlocked(month)
    led = payroll.advance_ledger(g.conn, month, [emp["id"]]).get(emp["id"])
    if not led or led["outstanding_before"] <= 0:
        return "No advance outstanding for this employee"
    if amount < 0 or amount > led["outstanding_before"] + 0.005:
        return f"Recovery must be between 0 and the outstanding {led['outstanding_before']:,.2f}"
    rows = payroll.compute_month(g.conn, month, employee_ids={emp["id"]})
    if rows:
        pay = rows[0]["pay"]
        # exact pay before any advance recovery, rounded DOWN so net pay can never end up below zero
        net_without = math.floor(pay["gross"] - (pay["total_deductions"] - pay["advance"]) + 1e-9)
        if amount > net_without + 1e-9:
            return f"Recovery {amount:,.2f} is more than this month's pay before recovery ({net_without:,.2f})"
    g.conn.execute("DELETE FROM adjustments WHERE employee_id=? AND month=? AND kind='advance'", (emp["id"], month))
    if amount > 0:
        g.conn.execute("INSERT INTO adjustments(employee_id, month, kind, amount, note) VALUES (?,?, 'advance', ?, ?)",
                       (emp["id"], month, amount, note or "advance recovery"))
    audit("advance_recovery", {"emp": emp["id"], "month": month, "amount": amount, "outstanding_before": led["outstanding_before"]})
    return None


@app.route("/advances", methods=["POST"])
@pay_required
def advances_action():
    f = request.form
    month = f.get("month") or cur_month()
    back = f.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("adjustments", month=month)
    act = f.get("action")
    if act == "give":
        emp = g.conn.execute("SELECT * FROM employees WHERE id=?", (int(f["employee_id"]),)).fetchone() or abort(404)
        try:
            amount = float(f["amount"])
            inst = float(f["installment"]) if f.get("installment", "").strip() else None
            assert amount > 0 and (inst is None or inst > 0)
        except (ValueError, AssertionError):
            flash("Advance and instalment must be positive amounts", "error")
            return redirect(back)
        g.conn.execute("INSERT INTO advances(employee_id, given_date, amount, installment, note, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                       (emp["id"], f["given_date"], amount, inst, f.get("note", ""), g.user["username"], dt.datetime.now().strftime(TS_FMT)))
        audit("advance_given", {"emp": emp["id"], "date": f["given_date"], "amount": amount, "installment": inst})
        flash(f"Advance of {amount:,.0f} recorded for {emp['name']}", "ok")
    elif act == "delete_advance":
        a = g.conn.execute("SELECT * FROM advances WHERE id=?", (int(f["id"]),)).fetchone() or abort(404)
        g.conn.execute("DELETE FROM advances WHERE id=?", (a["id"],))
        audit("advance_delete", dict(a))
        flash("Advance entry removed", "ok")
    elif act in ("recover", "recover_full", "recover_installment"):
        emp = g.conn.execute("SELECT * FROM employees WHERE id=?", (int(f["employee_id"]),)).fetchone() or abort(404)
        led = payroll.advance_ledger(g.conn, month, [emp["id"]]).get(emp["id"]) or {}
        try:
            amount = (led.get("outstanding_before", 0) if act == "recover_full" else
                      led.get("suggest", 0) if act == "recover_installment" else float(f.get("amount") or 0))
        except ValueError:
            flash("Recovery must be an amount", "error")
            return redirect(back)
        err = set_advance_recovery(emp, month, round(amount, 2), f.get("note", ""))
        flash(err or f"{emp['name']} {month}: advance recovery set to {amount:,.2f}", "error" if err else "ok")
    return redirect(back)


@app.route("/employees/bulk", methods=["GET", "POST"])
@pay_required
def employees_bulk():
    """Quick grid to set salary type/rate/OT/PF/ESI for many employees at once."""
    if request.method == "POST":
        f = request.form
        n = 0
        for eid in f.getlist("id"):
            g.conn.execute("""UPDATE employees SET department=?, salary_type=?, salary_rate=?, ot_enabled=?, ot_multiplier=?,
                              pf_enabled=?, esi_enabled=?, shift_id=?, paid_break=?, category=?, pay_mode=?, bank_value=?,
                              comp_hra=COALESCE(?, comp_hra), comp_conveyance=COALESCE(?, comp_conveyance),
                              comp_other=COALESCE(?, comp_other) WHERE id=?""",
                           (f.get(f"department_{eid}", ""), f.get(f"salary_type_{eid}", "monthly"),
                            float(f.get(f"salary_rate_{eid}") or 0), 1 if f.get(f"ot_enabled_{eid}") else 0,
                            float(f.get(f"ot_multiplier_{eid}") or 1), 1 if f.get(f"pf_enabled_{eid}") else 0,
                            1 if f.get(f"esi_enabled_{eid}") else 0, int(f.get(f"shift_id_{eid}") or 0) or None,
                            1 if f.get(f"paid_break_{eid}") else 0,
                            f.get(f"category_{eid}") if f.get(f"category_{eid}") in ("worker", "staff") else "worker",
                            f.get(f"pay_mode_{eid}") if f.get(f"pay_mode_{eid}") in payroll.PAY_MODES else "bank",
                            max(0.0, float(f.get(f"bank_value_{eid}") or 0)),
                            *[(max(0.0, float(f.get(f"{k}_{eid}") or 0)) if f"{k}_{eid}" in f else None)
                              for k in ("comp_hra", "comp_conveyance", "comp_other")], int(eid)))
            n += 1
        audit("employee_bulk_save", f"{n} employees")
        flash(f"Updated {n} employees", "ok")
        return redirect(url_for("employees_bulk"))
    rows = g.conn.execute("SELECT * FROM employees WHERE active = 1 ORDER BY " + emp_order_sql()).fetchall()
    return render_template("employees_bulk.html", rows=rows, shifts=g.conn.execute("SELECT * FROM shifts").fetchall())


@app.route("/employees/import", methods=["POST"])
@admin_required
def employees_import():
    """CSV import: device_user_id,name,department,designation,salary_type,salary_rate,... (any EMP_FIELDS headers)."""
    file = request.files.get("file")
    if not file:
        abort(400)
    reader = csv.DictReader(io.StringIO(file.read().decode("utf-8-sig")))
    n = 0
    for row in reader:
        uid = (row.get("device_user_id") or "").strip()
        if not uid:
            continue
        vals = {k: row[k].strip() for k in EMP_FIELDS + EMP_FLAGS + ["weekly_off"] if k in row and row[k] is not None and row[k].strip() != ""}
        vals.setdefault("name", f"User {uid}")
        vals["device_user_id"] = uid
        exists = g.conn.execute("SELECT id FROM employees WHERE device_user_id = ?", (uid,)).fetchone()
        if exists:
            sets = [k for k in vals if k != "device_user_id"]
            if sets:
                g.conn.execute(f"UPDATE employees SET {', '.join(k + '=?' for k in sets)} WHERE id=?", [vals[k] for k in sets] + [exists["id"]])
        else:
            g.conn.execute(f"INSERT INTO employees({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", list(vals.values()))
        n += 1
    audit("employee_import", f"{n} rows")
    flash(f"Imported/updated {n} employees", "ok")
    return redirect(url_for("employees"))


@app.route("/employees/export.csv")
def employees_export():
    rows = g.conn.execute("SELECT * FROM employees ORDER BY " + emp_order_sql()).fetchall()
    cols = [c for c in EMP_FIELDS + EMP_FLAGS + ["weekly_off"] if can_pay() or c not in PAY_FIELDS]
    return csv_response("employees.csv", cols, [[r[c] for c in cols] for r in rows])


# ------------------------------------------------------------------ shifts / holidays

@app.route("/shifts", methods=["GET", "POST"])
def shifts():
    if request.method == "POST":
        require_edit()
        f = request.form
        ints = ["break_min", "break_after_min", "grace_in_min", "grace_out_min", "ot_after_min", "ot_round_min", "ot_grace_min",
                "day_start_hour"]
        vals = [f["name"], f["start_time"], f["end_time"]] + [int(f.get(k) or 0) for k in ints] + \
               [int(float(f.get("full_day_hrs") or 8) * 60), int(float(f.get("half_day_hrs") or 4) * 60)]
        cols = ["name", "start_time", "end_time"] + ints + ["full_day_min", "half_day_min", "break_start"]
        vals.append(f.get("break_start") or None)
        if f.get("id"):
            g.conn.execute(f"UPDATE shifts SET {', '.join(c + '=?' for c in cols)} WHERE id=?", vals + [int(f["id"])])
        else:
            g.conn.execute(f"INSERT INTO shifts({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        audit("shift_save", dict(f))
        flash("Shift saved", "ok")
        return redirect(url_for("shifts"))
    rows = g.conn.execute("SELECT s.*, (SELECT COUNT(*) FROM employees e WHERE e.shift_id = s.id) AS n FROM shifts s").fetchall()
    return render_template("shifts.html", rows=rows)


NATIONAL_HOLIDAYS = [("01-26", "Republic Day"), ("08-15", "Independence Day"), ("10-02", "Gandhi Jayanti")]


@app.route("/holidays", methods=["GET", "POST"])
def holidays():
    year = request.args.get("year") or str(dt.date.today().year)
    if request.method == "POST":
        require_edit()
        f = request.form
        if f.get("delete"):
            h = g.conn.execute("SELECT * FROM holidays WHERE id = ?", (int(f["delete"]),)).fetchone() or abort(404)
            ensure_unlocked(h["hdate"])
            g.conn.execute("DELETE FROM holidays WHERE id = ?", (h["id"],))
            flash(f"Removed holiday {h['hdate']} {h['name']}", "ok")
        elif f.get("action") == "national":
            n = 0
            for md, name in NATIONAL_HOLIDAYS:
                d = f"{year}-{md}"
                if not payroll.is_locked(g.conn, d):
                    n += g.conn.execute("INSERT OR IGNORE INTO holidays(hdate, name, paid) VALUES (?,?,1)", (d, name)).rowcount
            flash(f"Added {n} national holiday(s) for {year} as paid holidays", "ok")
        else:
            ensure_unlocked(f["hdate"])
            g.conn.execute("INSERT OR REPLACE INTO holidays(hdate, name, paid) VALUES (?,?,?)",
                           (f["hdate"], f["name"].strip() or "Holiday", 1 if f.get("paid") else 0))
            flash(f"{f['hdate']} marked as {'paid' if f.get('paid') else 'unpaid'} holiday: {f['name']}. Everyone's attendance recalculated.", "ok")
        audit("holiday_save", {k: v for k, v in f.items() if k != "csrf"})
        back = f.get("back", "")
        return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("holidays", year=year))
    rows = g.conn.execute("SELECT * FROM holidays WHERE hdate LIKE ? ORDER BY hdate", (year + "%",)).fetchall()
    return render_template("holidays.html", rows=rows, year=year, national=NATIONAL_HOLIDAYS)


# ------------------------------------------------------------------ attendance

@app.route("/attendance/daily")
def att_daily():
    day = dt.date.fromisoformat(request.args.get("date") or dt.date.today().isoformat())
    return redirect(url_for("dashboard", date=day.isoformat()))


@app.route("/attendance/monthly")
def att_monthly():
    month = cur_month()
    rows = sort_rows(payroll.compute_month(g.conn, month))
    start, end = engine.month_range(month)
    dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    prev, nxt = month_nav(month)
    hol = {r["hdate"]: r for r in g.conn.execute("SELECT * FROM holidays WHERE hdate LIKE ?", (month + "%",))}
    return render_template("muster.html", rows=rows, dates=dates, month=month, prev=prev, nxt=nxt, hol=hol,
                           edited=edited_set(start, end))


@app.route("/attendance/employee/<int:eid>")
def att_employee(eid):
    month = cur_month()
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    rows = payroll.compute_month(g.conn, month, employee_ids={eid})
    if not rows:
        start, end = engine.month_range(month)
        d = engine.employee_days(g.conn, [dict(emp)], start, end, g.settings)[eid]
        summ = engine.summarize(d["days"], d["shift"], g.settings, emp["salary_type"], d.get("before", ()), d.get("after", ()))
        payroll.apply_month_override(summ, payroll.month_overrides(g.conn, month).get(eid))
        row = {"emp": dict(emp), "days": d["days"], "summary": summ, "shift": d["shift"],
               "pay": payroll.calculate(dict(emp), summ, month, g.settings)}
    else:
        row = rows[0]
    prev, nxt = month_nav(month)
    locked = payroll.run_status(g.conn, month) == "final"
    others = g.conn.execute("SELECT id, name, device_user_id FROM employees WHERE active = 1 ORDER BY " + emp_order_sql()).fetchall()
    m_start, m_end = engine.month_range(month)
    adv = payroll.advance_ledger(g.conn, month, [eid]).get(eid) if can_pay() else None
    if can_pay():
        attach_cash_paid([row], month)
    return render_template("employee_month.html", adv=adv, PAY_MODES=payroll.PAY_MODES, row=row, emp=emp, month=month, prev=prev, nxt=nxt, locked=locked, others=others,
                           edited=edited_set(m_start, m_end, eid),
                           history=shift_history(eid), shifts=g.conn.execute("SELECT * FROM shifts").fetchall())


TS_FMT = "%Y-%m-%d %H:%M:%S"


def _punch_work_date(emp, ts):
    shift = g.conn.execute("SELECT * FROM shifts WHERE id = ?", (emp["shift_id"],)).fetchone() \
        or g.conn.execute("SELECT * FROM shifts ORDER BY id LIMIT 1").fetchone()
    return engine.work_date_of(ts, shift).isoformat()


def add_manual_punch(emp, ts, role, reason):
    """Insert (or re-role) a punch. Pairing is recomputed from the whole stream on every read,
    so all following days re-pair automatically."""
    ensure_unlocked(_punch_work_date(emp, ts))
    s = ts.strftime(TS_FMT)
    note = f"by {g.user['username']}: {reason}"
    now = dt.datetime.now().strftime(TS_FMT)
    cur = g.conn.execute("""INSERT OR IGNORE INTO punches(device_user_id, ts, source, note, role, changed_by, changed_at)
                            VALUES (?,?, 'manual', ?, ?, ?, ?)""", (emp["device_user_id"], s, note, role, g.user["username"], now))
    if not cur.rowcount:
        g.conn.execute("UPDATE punches SET role=?, deleted=0, changed_by=?, changed_at=?, note=CASE WHEN source='device' THEN ? ELSE note END "
                       "WHERE device_user_id=? AND ts=?", (role, g.user["username"], now, note, emp["device_user_id"], s))
    audit("punch_add", {"emp": emp["id"], "ts": s, "role": role, "reason": reason})


def set_punch_role(emp, punch_id, role, reason=None):
    p = g.conn.execute("SELECT * FROM punches WHERE id=? AND device_user_id=?", (punch_id, emp["device_user_id"])).fetchone() or abort(404)
    ensure_unlocked(_punch_work_date(emp, dt.datetime.strptime(p["ts"], TS_FMT)))
    g.conn.execute("UPDATE punches SET role=?, changed_by=?, changed_at=? WHERE id=?",
                   (role or None, g.user["username"] if role else None, dt.datetime.now().strftime(TS_FMT) if role else None, punch_id))
    if reason is not None and p["source"] == "device":
        g.conn.execute("UPDATE punches SET note=? WHERE id=?", (f"by {g.user['username']}: {reason}" if role else "", punch_id))
    audit("punch_role", {"emp": emp["id"], "ts": p["ts"], "role": role or "auto"})


def fix_shift(emp, work_date, tin, tout, reason):
    """Replace a day's shift with confirmed IN/OUT punches. The day's old punches that fall outside the new
    times are marked 'ignore' (still visible, restorable) so they don't linger as unpaired punches."""
    if tout <= tin or (tout - tin).total_seconds() > float(g.settings.get("max_duty_hours") or 26) * 3600:
        return "Out must be after In and within the maximum duty length"
    rec = engine.employee_days(g.conn, [dict(emp)], work_date, work_date, g.settings)[emp["id"]]["days"][0]
    for sess in rec["sessions"]:
        for pid, ts in zip(sess["ids"], sess["punches"]):
            if pid and (ts < tin or ts > tout):
                set_punch_role(emp, pid, "ignore", f"replaced by corrected shift {tin:%H:%M}-{tout:%H:%M}" + (f" ({reason})" if reason else ""))
    g.conn.execute("UPDATE punches SET role=NULL WHERE device_user_id=? AND ts > ? AND ts < ? AND role IN ('in','out')",
                   (emp["device_user_id"], tin.strftime(TS_FMT), tout.strftime(TS_FMT)))
    add_manual_punch(emp, tin, "in", reason)
    add_manual_punch(emp, tout, "out", reason)
    return None


def _dtl(v):
    """Parse an <input type=datetime-local> value."""
    return dt.datetime.strptime(v.replace("T", " ")[:16], "%Y-%m-%d %H:%M")


@app.route("/attendance/employee/<int:eid>/penalty", methods=["POST"])
@edit_required
def late_penalty(eid):
    """Late-comer penalty, chosen day by day on the employee's sheet: OT minutes removed for that day."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    month = f.get("month") or cur_month()
    ensure_unlocked(f.get("date") or month)
    if f.get("action") in ("all", "clear_all"):
        start, end = engine.month_range(month)
        days = engine.employee_days(g.conn, [dict(emp)], start, end, g.settings)[eid]["days"]
        n = 0
        for d in days:
            ds = d["date"].isoformat()
            if f["action"] == "clear_all":
                n += g.conn.execute("DELETE FROM late_penalties WHERE employee_id=? AND work_date=?", (eid, ds)).rowcount
            elif d["late_min"] and not d["late_penalty_set"] and d["late_penalty_suggest"] > 0:
                g.conn.execute("INSERT INTO late_penalties(employee_id, work_date, minutes, note, created_by, created_at) VALUES (?,?,?,?,?,?)",
                               (eid, ds, d["late_penalty_suggest"], f"late {d['late_min']}m", g.user["username"],
                                dt.datetime.now().strftime(TS_FMT)))
                n += 1
        audit("late_penalty_" + f["action"], {"emp": eid, "month": month, "days": n})
        flash(f"{'Removed' if f['action'] == 'clear_all' else 'Applied'} late penalty on {n} day(s)", "ok")
    else:
        ds = f["date"]
        try:
            minutes = int(float(f.get("minutes") or 0))
        except ValueError:
            minutes = 0
        if minutes > 0:
            g.conn.execute("""INSERT INTO late_penalties(employee_id, work_date, minutes, note, created_by, created_at) VALUES (?,?,?,?,?,?)
                              ON CONFLICT(employee_id, work_date) DO UPDATE SET minutes=excluded.minutes, created_by=excluded.created_by,
                              created_at=excluded.created_at""",
                           (eid, ds, minutes, f.get("note", ""), g.user["username"], dt.datetime.now().strftime(TS_FMT)))
        else:
            g.conn.execute("DELETE FROM late_penalties WHERE employee_id=? AND work_date=?", (eid, ds))
        audit("late_penalty", {"emp": eid, "date": ds, "minutes": minutes})
    back = f.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("att_employee", eid=eid, month=month))


def parse_hours(text):
    """'95:30', '95.5' or '95' -> minutes; '' -> None."""
    text = (text or "").strip()
    if not text:
        return None
    if ":" in text:
        h, m = text.split(":", 1)
        mins = int(h or 0) * 60 + int(m or 0)
    else:
        mins = round(float(text) * 60)
    if mins < 0:
        raise ValueError("negative")
    return mins


@app.route("/attendance/employee/<int:eid>/totals", methods=["POST"])
@edit_required
def month_totals(eid):
    """Manually set the month's final payable days and/or overtime (calculated values are kept for comparison)."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    month = f["month"]
    dt.datetime.strptime(month, "%Y-%m")
    ensure_unlocked(month)
    back = f.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("att_employee", eid=eid, month=month)
    if f.get("action") == "reset":
        g.conn.execute("DELETE FROM month_overrides WHERE employee_id=? AND month=?", (eid, month))
        audit("month_totals_reset", {"emp": eid, "month": month})
        flash("Final totals back to the calculated values", "ok")
        return redirect(back)
    try:
        days = float(f["payable_days"]) if f.get("payable_days", "").strip() else None
        ot = parse_hours(f.get("ot_hours"))
    except ValueError:
        flash("Payable days must be a number; overtime like 95:30 or 95.5", "error")
        return redirect(back)
    start, end = engine.month_range(month)
    if days is not None and not (0 <= days <= (end - start).days + 1):
        flash(f"Payable days must be between 0 and {(end - start).days + 1}", "error")
        return redirect(back)
    rows = payroll.compute_month(g.conn, month, employee_ids={eid})
    calc_days = rows[0]["summary"]["calc_payable_days"] if rows else None
    calc_ot = rows[0]["summary"]["calc_ot_min"] if rows else None
    if days is not None and calc_days is not None and abs(days - calc_days) < 1e-9:
        days = None   # same as calculated: nothing to override
    if ot is not None and calc_ot is not None and ot == int(calc_ot):
        ot = None
    if days is None and ot is None:
        g.conn.execute("DELETE FROM month_overrides WHERE employee_id=? AND month=?", (eid, month))
        flash("Same as calculated - no edit saved", "ok")
        return redirect(back)
    if not f.get("reason", "").strip():
        flash("Please give a reason for editing the final totals", "error")
        return redirect(back)
    g.conn.execute("""INSERT INTO month_overrides(employee_id, month, payable_days, ot_min, calc_payable_days, calc_ot_min,
                                                  reason, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(employee_id, month) DO UPDATE SET payable_days=excluded.payable_days, ot_min=excluded.ot_min,
                      calc_payable_days=excluded.calc_payable_days, calc_ot_min=excluded.calc_ot_min, reason=excluded.reason,
                      created_by=excluded.created_by, created_at=excluded.created_at""",
                   (eid, month, days, ot, calc_days, calc_ot, f["reason"].strip(), g.user["username"],
                    dt.datetime.now().strftime(TS_FMT)))
    audit("month_totals_edit", {"emp": eid, "month": month, "payable_days": [calc_days, days], "ot_min": [calc_ot, ot],
                                "reason": f["reason"].strip()})
    flash(f"Final totals for {emp['name']} {month} edited", "ok")
    return redirect(back)


@app.route("/attendance/day/<int:eid>/<date>", methods=["GET", "POST"])
def att_day(eid, date):
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    d = dt.date.fromisoformat(date)
    if request.method == "POST":
        require_edit()
        f = request.form
        act = f.get("action")
        if act in ("override", "clear_override", "leave", "clear_leave"):
            ensure_unlocked(date)
        if act == "override":
            status = f.get("status") or None
            if not status:
                g.conn.execute("DELETE FROM day_overrides WHERE employee_id=? AND work_date=?", (eid, date))
            else:
                g.conn.execute("""INSERT OR REPLACE INTO day_overrides(employee_id, work_date, status, in_time, out_time, reason, created_by, created_at)
                                  VALUES (?,?,?,NULL,NULL,?,?,?)""", (eid, date, status, f.get("reason", ""), g.user["username"],
                                                                     dt.datetime.now().strftime(TS_FMT)))
            audit("day_override", {"emp": eid, "date": date, "status": status, "reason": f.get("reason")})
        elif act == "clear_override":
            g.conn.execute("DELETE FROM day_overrides WHERE employee_id=? AND work_date=?", (eid, date))
            audit("day_override_clear", {"emp": eid, "date": date})
        elif act == "leave":
            g.conn.execute("""INSERT OR REPLACE INTO leaves(employee_id, leave_date, leave_type, paid, half_day, reason, created_by, created_at)
                              VALUES (?,?,?,?,?,?,?,?)""", (eid, date, f.get("leave_type", "CL"), 1 if f.get("paid") else 0,
                                                           1 if f.get("half_day") else 0, f.get("reason", ""), g.user["username"],
                                                           dt.datetime.now().strftime(TS_FMT)))
            audit("leave_add", {"emp": eid, "date": date, "type": f.get("leave_type")})
        elif act == "clear_leave":
            g.conn.execute("DELETE FROM leaves WHERE employee_id=? AND leave_date=?", (eid, date))
            audit("leave_clear", {"emp": eid, "date": date})
        elif act == "fix_session":
            err = fix_shift(emp, d, _dtl(f["in_dt"]), _dtl(f["out_dt"]), f.get("reason", ""))
            if err:
                flash(err, "error")
                return redirect(request.url)
        elif act == "quick":
            # One-row fix from the month view: optional status + optional corrected in/out.
            status = f.get("status") or None
            if status != (f.get("orig_status") or None):
                ensure_unlocked(date)
                if status:
                    g.conn.execute("""INSERT OR REPLACE INTO day_overrides(employee_id, work_date, status, in_time, out_time, reason, created_by, created_at)
                                      VALUES (?,?,?,NULL,NULL,?,?,?)""", (eid, date, status, f.get("reason", ""), g.user["username"],
                                                                         dt.datetime.now().strftime(TS_FMT)))
                else:
                    g.conn.execute("DELETE FROM day_overrides WHERE employee_id=? AND work_date=?", (eid, date))
                audit("day_override", {"emp": eid, "date": date, "status": status, "reason": f.get("reason"), "via": "quick"})
            tin, tout = f.get("in_dt"), f.get("out_dt")
            if tin and tout and (tin != f.get("orig_in") or tout != f.get("orig_out")):
                err = fix_shift(emp, d, _dtl(tin), _dtl(tout), f.get("reason", "") or "quick fix")
                if err:
                    flash(err, "error")
                    return redirect(f.get("back") or request.url)
        elif act == "gate_add":
            err = save_gate_pass(eid, dict(f, work_date=date))
            if err:
                flash(err, "error")
                return redirect(f.get("back") or request.url)
        elif act == "gate_delete":
            delete_gate_pass(int(f["gate_id"]))
        elif act == "apply_suggest":
            add_manual_punch(emp, dt.datetime.strptime(f["ts"], TS_FMT), f["role"], "suggested fix accepted")
            if f.get("orphan_id") and f["orphan_id"] != "None":
                set_punch_role(emp, int(f["orphan_id"]), f["orphan_role"])
        elif act == "add_punch":
            add_manual_punch(emp, _dtl(f["ts"]), f.get("role") or None, f.get("reason", ""))
        elif act == "set_role":
            set_punch_role(emp, int(f["punch_id"]), f.get("role"))
        elif act in ("delete_punch", "restore_punch"):
            set_punch_role(emp, int(f["punch_id"]), "ignore" if act == "delete_punch" else None)
        flash("Saved - following days were re-paired automatically", "ok")
        return redirect(f.get("back") or request.url)
    data = engine.employee_days(g.conn, [dict(emp)], d, d, g.settings)[eid]
    rec = data["days"][0]
    lo = dt.datetime.combine(d - dt.timedelta(days=1), dt.time(12))
    hi = dt.datetime.combine(d + dt.timedelta(days=2), dt.time(12))
    raw = g.conn.execute("SELECT * FROM punches WHERE device_user_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                         (emp["device_user_id"], lo.strftime(TS_FMT), hi.strftime(TS_FMT))).fetchall()
    mine = {t.strftime(TS_FMT) for s in rec["sessions"] for t in s["punches"]}
    ov = g.conn.execute("SELECT * FROM day_overrides WHERE employee_id=? AND work_date=?", (eid, date)).fetchone()
    lv = g.conn.execute("SELECT * FROM leaves WHERE employee_id=? AND leave_date=?", (eid, date)).fetchone()
    gates = g.conn.execute("SELECT * FROM gate_passes WHERE employee_id=? AND work_date=? ORDER BY out_time", (eid, date)).fetchall()
    first = rec["sessions"][0] if rec["sessions"] else None
    sug = rec["suggest"]
    day_shift = rec["shift"]
    default_in = first["start"] if first else dt.datetime.combine(d, engine.parse_hm(day_shift["start_time"]))
    if first and first["end"]:
        default_out = first["end"]
    elif sug and sug["role"] == "out":
        default_out = sug["ts"]
    else:
        default_out = engine.shift_bounds(d, day_shift)[1]
    if sug and sug["role"] == "in":
        default_in, default_out = sug["ts"], sug["of"]
    return render_template("day_edit.html", emp=emp, d=d, rec=rec, raw=raw, mine=mine, ov=ov, lv=lv, shift=day_shift, gates=gates,
                           shifts=g.conn.execute("SELECT * FROM shifts").fetchall(),
                           default_in=default_in, default_out=default_out, typical=data["typical"],
                           locked=payroll.is_locked(g.conn, date), back=request.args.get("back", ""))


def _exception_items(rows):
    long_h = float(g.settings.get("long_duty_flag_hours") or 16)
    items = []
    for r in rows:
        for d in r["days"]:
            if d["override"]:
                continue
            kind = reason = None
            if d.get("mp_on_off"):
                kind, reason = "mp", "Missing punch on weekly off / holiday (paid as off day; add the other punch for overtime)"
            elif d["status"] == "MP":
                kind, reason = "mp", "Missing punch"
            elif "unpaired punch" in d["flags"]:
                kind, reason = "mp", "Extra unpaired punch"
            elif "odd in/out punches" in d["flags"]:
                kind, reason = "mp", "Odd number of in/out punches (one missing?)"
            elif "long duty" in d["flags"]:
                h = max((s["end"] - s["start"]).total_seconds() / 3600 for s in d["sessions"] if not s["orphan"])
                kind, reason = "long", f"Long duty {h:.1f} h"
            elif "too short" in d["flags"]:
                kind, reason = "short", f"Too short ({engine.fmt_min(d['worked_min'])} worked)"
            if kind:
                long_s = next((s for s in d["sessions"] if not s["orphan"] and
                               (s["end"] - s["start"]).total_seconds() / 3600 >= long_h), None) if kind == "long" else None
                orphan = next((s for s in d["sessions"] if s["orphan"]), None)
                items.append({"emp": r["emp"], "d": d, "kind": kind, "reason": reason, "long": long_s, "orphan": orphan})
    return items


@app.route("/exceptions", methods=["GET", "POST"])
def exceptions():
    month = cur_month()
    if request.method == "POST":
        require_edit()
        f = request.form
        emps = {}

        def emp_of(eid):
            if eid not in emps:
                emps[eid] = g.conn.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone() or abort(404)
            return emps[eid]
        if f.get("action") == "apply_suggestions":
            # value: "<emp id>|<suggested ts>|<in|out>|<orphan punch id>|<orphan role>"
            n = 0
            for v in f.getlist("pick"):
                eid, ts, role, pid, orole = v.split("|")
                emp = emp_of(int(eid))
                add_manual_punch(emp, dt.datetime.strptime(ts, TS_FMT), role, "suggested fix accepted")
                if pid and pid != "None":
                    set_punch_role(emp, int(pid), orole)
                n += 1
            flash(f"Applied {n} suggested punch(es); attendance re-paired", "ok")
        elif f.get("action") == "confirm_long":
            emp = emp_of(int(f["eid"]))
            set_punch_role(emp, int(f["start_id"]), "in")
            set_punch_role(emp, int(f["end_id"]), "out")
            flash("Long duty confirmed", "ok")
        elif f.get("action") == "confirm_long_all":
            n = 0
            for v in f.getlist("pick_long"):
                eid, sid, eid2 = v.split("|")
                emp = emp_of(int(eid))
                set_punch_role(emp, int(sid), "in")
                set_punch_role(emp, int(eid2), "out")
                n += 1
            flash(f"Confirmed {n} long duties", "ok")
        elif f.get("action") == "split_long":
            emp = emp_of(int(f["eid"]))
            set_punch_role(emp, int(f["end_id"]), "in")  # that 'out' was really the next arrival
            flash("Split: the later punch is now an arrival; the earlier one shows as a missing punch with a suggestion", "ok")
        return redirect(url_for("exceptions", month=month, kind=request.args.get("kind", "")))
    rows = sort_rows(payroll.compute_month(g.conn, month))
    items = _exception_items(rows)
    kind = request.args.get("kind", "")
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in ("mp", "long", "short")}
    if kind:
        items = [i for i in items if i["kind"] == kind]
    prev, nxt = month_nav(month)
    return render_template("exceptions.html", items=items, month=month, prev=prev, nxt=nxt, counts=counts, kind=kind)


def save_gate_pass(emp_id, f):
    """Add a gate pass from a form. Returns an error message or None."""
    work_date = f.get("work_date", "")
    try:
        dt.date.fromisoformat(work_date)
        engine.parse_hm(f["out_time"])
        if f.get("in_time"):
            engine.parse_hm(f["in_time"])
    except (ValueError, KeyError):
        return "Enter a date and an out time (and the return time if they came back)"
    ensure_unlocked(work_date)
    kind = f.get("kind") if f.get("kind") in ("personal", "official") else "personal"
    g.conn.execute("""INSERT INTO gate_passes(employee_id, work_date, out_time, in_time, kind, reason, approved_by, created_by, created_at)
                      VALUES (?,?,?,?,?,?,?,?,?)""",
                   (emp_id, work_date, f["out_time"], f.get("in_time") or None, kind, f.get("reason", "").strip(),
                    f.get("approved_by", "").strip(), g.user["username"], dt.datetime.now().strftime(TS_FMT)))
    audit("gate_pass_add", {"emp": emp_id, "date": work_date, "out": f["out_time"], "in": f.get("in_time"), "kind": kind})
    return None


def delete_gate_pass(gid):
    gp = g.conn.execute("SELECT * FROM gate_passes WHERE id = ?", (gid,)).fetchone() or abort(404)
    ensure_unlocked(gp["work_date"])
    g.conn.execute("DELETE FROM gate_passes WHERE id = ?", (gid,))
    audit("gate_pass_delete", {k: gp[k] for k in gp.keys()})


@app.route("/gatepass", methods=["GET", "POST"])
def gate_passes():
    """Gate passes: employees going out of the premises during the shift (personal time is deducted)."""
    month = cur_month()
    if request.method == "POST":
        require_edit()
        f = request.form
        if f.get("delete"):
            delete_gate_pass(int(f["delete"]))
            flash("Gate pass removed; attendance recalculated", "ok")
        else:
            err = save_gate_pass(int(f["employee_id"]), f)
            flash(err or "Gate pass added; attendance recalculated", "error" if err else "ok")
        back = f.get("back", "")
        return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("gate_passes", month=month))
    start, end = engine.month_range(month)
    rows = g.conn.execute("""SELECT gp.*, e.name, e.device_user_id FROM gate_passes gp JOIN employees e ON e.id = gp.employee_id
                             WHERE gp.work_date BETWEEN ? AND ? ORDER BY gp.work_date DESC, gp.out_time""",
                          (start.isoformat(), end.isoformat())).fetchall()
    # minutes each pass actually changed (from the attendance engine)
    ids = {r["employee_id"] for r in rows}
    effect = {}
    if ids:
        emps = [dict(e) for e in g.conn.execute(f"SELECT * FROM employees WHERE id IN ({','.join('?' * len(ids))})", list(ids))]
        for eid, d in engine.employee_days(g.conn, emps, start, end, g.settings).items():
            for day in d["days"]:
                if day.get("gate_personal_min") or day.get("gate_official_min"):
                    effect[(eid, day["date"].isoformat())] = (day.get("gate_personal_min", 0), day.get("gate_official_min", 0), day)
    emps_all = g.conn.execute("SELECT id, name, device_user_id FROM employees WHERE active=1 ORDER BY " + emp_order_sql()).fetchall()
    prev, nxt = month_nav(month)
    return render_template("gatepass.html", rows=rows, effect=effect, emps=emps_all, month=month, prev=prev, nxt=nxt,
                           today=dt.date.today().isoformat(), locked=payroll.run_status(g.conn, month) == "final")


@app.route("/leaves", methods=["GET", "POST"])
def leaves():
    month = cur_month()
    if request.method == "POST":
        require_edit()
        f = request.form
        if f.get("delete"):
            g.conn.execute("DELETE FROM leaves WHERE id = ?", (int(f["delete"]),))
        else:
            a, b = dt.date.fromisoformat(f["from"]), dt.date.fromisoformat(f.get("to") or f["from"])
            n = 0
            while a <= b:
                ensure_unlocked(a.isoformat())
                g.conn.execute("""INSERT OR REPLACE INTO leaves(employee_id, leave_date, leave_type, paid, half_day, reason, created_by, created_at)
                                  VALUES (?,?,?,?,?,?,?,?)""", (int(f["employee_id"]), a.isoformat(), f["leave_type"],
                                                               1 if f.get("paid") else 0, 1 if f.get("half_day") else 0, f.get("reason", ""),
                                                               g.user["username"], dt.datetime.now().strftime(TS_FMT)))
                a += dt.timedelta(days=1)
                n += 1
            flash(f"{n} leave day(s) recorded", "ok")
        audit("leave_save", dict(f))
        return redirect(url_for("leaves", month=month))
    rows = g.conn.execute("""SELECT l.*, e.name, e.device_user_id FROM leaves l JOIN employees e ON e.id = l.employee_id
                             WHERE l.leave_date LIKE ? ORDER BY l.leave_date, e.name""", (month + "%",)).fetchall()
    emps = g.conn.execute("SELECT id, name, device_user_id FROM employees WHERE active=1 ORDER BY name").fetchall()
    year = month[:4]
    balance = g.conn.execute("""SELECT e.name, l.leave_type, SUM(CASE WHEN l.half_day THEN 0.5 ELSE 1 END) AS n
                                FROM leaves l JOIN employees e ON e.id=l.employee_id WHERE l.leave_date LIKE ?
                                GROUP BY e.name, l.leave_type ORDER BY e.name""", (year + "%",)).fetchall()
    prev, nxt = month_nav(month)
    return render_template("leaves.html", rows=rows, emps=emps, month=month, prev=prev, nxt=nxt, balance=balance, year=year)


@app.route("/adjustments", methods=["GET", "POST"])
@pay_required
def adjustments():
    month = cur_month()
    if request.method == "POST":
        require_pay()
        ensure_unlocked(month)
        f = request.form
        if f.get("delete"):
            g.conn.execute("DELETE FROM adjustments WHERE id = ?", (int(f["delete"]),))
        else:
            if f["kind"] not in ("allowance", "bonus", "deduction", "tds"):
                abort(400, "Advance recoveries are set in the Advances section")
            g.conn.execute("INSERT INTO adjustments(employee_id, month, kind, amount, note) VALUES (?,?,?,?,?)",
                           (int(f["employee_id"]), month, f["kind"], float(f["amount"]), f.get("note", "")))
        audit("adjustment_save", dict(f))
        return redirect(url_for("adjustments", month=month))
    rows = g.conn.execute("""SELECT a.*, e.name, e.device_user_id FROM adjustments a JOIN employees e ON e.id=a.employee_id
                             WHERE a.month = ? ORDER BY e.name""", (month,)).fetchall()
    emps = g.conn.execute("SELECT id, name, device_user_id FROM employees WHERE active=1 ORDER BY name").fetchall()
    ledger = payroll.advance_ledger(g.conn, month)
    names = {e["id"]: e for e in g.conn.execute("SELECT id, name, device_user_id FROM employees")}
    open_adv = sorted(((names[k], v) for k, v in ledger.items() if k in names and (v["outstanding_before"] > 0 or v["this_month"])),
                      key=lambda kv: kv[0]["name"].lower())
    given = g.conn.execute("""SELECT a.*, e.name, e.device_user_id FROM advances a JOIN employees e ON e.id=a.employee_id
                              ORDER BY a.given_date DESC, a.id DESC LIMIT 200""").fetchall()
    prev, nxt = month_nav(month)
    return render_template("adjustments.html", rows=rows, emps=emps, month=month, prev=prev, nxt=nxt, open_adv=open_adv,
                           given=given, locked=payroll.run_status(g.conn, month) == "final", today=dt.date.today().isoformat())


# ------------------------------------------------------------------ changes made on top of machine data

def edited_set(start, end, emp_id=None):
    """(employee_id, date) pairs a person changed - used to highlight cells. Holidays apply to all, so skipped."""
    return {(c["emp"]["id"], c["date"]) for c in chg.collect(g.conn, start, end, emp_id) if c["emp"] and c["kind"] != "month_total"}


@app.route("/changes")
def changes_view():
    month = cur_month()
    start, end = engine.month_range(month)
    emp_id = request.args.get("eid", type=int)
    kind, by = request.args.get("kind", ""), request.args.get("by", "")
    allc = chg.collect(g.conn, start, end, emp_id)
    counts = {k: sum(1 for c in allc if c["kind"] == k) for k in chg.KINDS}
    users = sorted({c["by"] for c in allc if c["by"]})
    items = [c for c in allc if (not kind or c["kind"] == kind) and (not by or c["by"] == by)]
    cmp = chg.compare(g.conn, items, start, end, g.settings)
    # group by employee-day so the machine vs current view is shown once per day
    groups = {}
    for c in items:
        key = (c["emp"]["id"] if c["emp"] else 0, c["date"])
        grp = groups.setdefault(key, {"emp": c["emp"], "date": c["date"], "items": [], "cmp": cmp.get(key)})
        grp["items"].append(c)
    for key, val in cmp.items():  # days changed only indirectly (shift roster)
        if key not in groups and kind in ("", "roster") and not by:
            emp = next((c["emp"] for c in items if c["emp"] and c["emp"]["id"] == key[0]), None)
            if emp:
                groups[key] = {"emp": emp, "date": key[1], "items": [], "cmp": val}
    order = sorted(groups.values(), key=lambda gr: (gr["date"], (gr["emp"] or {}).get("name", "")), reverse=True)
    emps = g.conn.execute("SELECT id, name, device_user_id FROM employees WHERE active=1 ORDER BY name").fetchall()
    prev, nxt = month_nav(month)
    locked = payroll.run_status(g.conn, month) == "final"
    return render_template("changes.html", groups=order, counts=counts, total=len(allc), month=month, prev=prev, nxt=nxt,
                           emps=emps, users=users, kind=kind, by=by, emp_id=emp_id, KINDS=chg.KINDS, locked=locked)


@app.route("/changes/undo", methods=["POST"])
@edit_required
def changes_undo():
    """Revert one manual change so the machine's data applies again."""
    f = request.form
    kind, rid = f["kind"], int(f["id"])
    table = {"punch_added": "punches", "punch_role": "punches", "status": "day_overrides", "leave": "leaves",
             "penalty": "late_penalties", "roster": "employee_shifts", "holiday": "holidays",
             "month_total": "month_overrides", "gate_pass": "gate_passes"}.get(kind) or abort(400)
    row = g.conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone() or abort(404)
    date = (row["ts"][:10] if table == "punches" else row["month"] + "-01" if table == "month_overrides"
            else row["work_date"] if "work_date" in row.keys()
            else row["leave_date"] if table == "leaves" else row["hdate"] if table == "holidays" else row["from_date"])
    if table == "employee_shifts":
        _ensure_range_unlocked(row["from_date"], row["to_date"])
    else:
        ensure_unlocked(date)
    if kind == "punch_added":
        if row["source"] != "manual":
            abort(400, "Only manually added punches can be removed; machine punches can only be ignored")
        g.conn.execute("DELETE FROM punches WHERE id = ?", (rid,))
    elif kind == "punch_role":
        g.conn.execute("UPDATE punches SET role=NULL, deleted=0, changed_by=NULL, changed_at=NULL WHERE id = ?", (rid,))
    else:
        g.conn.execute(f"DELETE FROM {table} WHERE id = ?", (rid,))
    audit("change_undo", {"kind": kind, "id": rid, "date": date, "row": {k: row[k] for k in row.keys() if k != "note"}})
    flash("Change undone - the machine's data applies again for that day (attendance recalculated)", "ok")
    back = f.get("back", "")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("changes_view"))


# ------------------------------------------------------------------ payroll

def payroll_rows(month):
    """Finalized months come from the saved snapshot; others are computed live."""
    run, saved = payroll.load_saved(g.conn, month)
    if run and run["status"] == "final":
        return run, sort_rows([{"emp": dict(it["emp"], id=it["employee_id"]), "summary": it["summary"], "pay": it["pay"]} for it in saved])
    return run, sort_rows(payroll.compute_month(g.conn, month))


@app.route("/payroll", methods=["GET", "POST"])
@pay_required
def payroll_view():
    month = cur_month()
    if request.method == "POST":
        require_pay()
        act = request.form.get("action")
        try:
            if act in ("save", "finalize"):
                _, n = payroll.save_run(g.conn, month, g.user["username"], finalize=(act == "finalize"))
                flash(f"Payroll {month} {'finalized and locked' if act == 'finalize' else 'saved as draft'} ({n} employees)", "ok")
            elif act == "reopen":
                g.conn.execute("UPDATE payroll_runs SET status='draft' WHERE month=?", (month,))
                audit("payroll_reopen", month)
                flash(f"Payroll {month} reopened", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("payroll_view", month=month))
    run, rows = payroll_rows(month)
    totals = {k: sum(r["pay"][k] for r in rows) for k in ("basic", "ot_pay", "allowance", "bonus", "gross", "pf", "esi",
                                                          "advance", "tds", "other_deduction", "total_deductions", "net")}
    attach_cash_paid(rows, month)
    totals["esi_employer"] = sum(r["pay"].get("esi_employer", 0) for r in rows)
    totals["bank"] = sum(r["pay"].get("bank", r["pay"]["net"]) for r in rows)
    totals["cash"] = sum(r["pay"].get("cash", 0) for r in rows)
    unpriced = [r for r in rows if not r["pay"]["rate"]]
    prev, nxt = month_nav(month)
    return render_template("payroll.html", rows=rows, run=run, month=month, totals=totals, prev=prev, nxt=nxt, unpriced=unpriced)


@app.route("/payslip/<int:eid>")
@pay_required
def payslip(eid):
    month = cur_month()
    _, rows = payroll_rows(month)
    rows = [r for r in rows if r["emp"]["id"] == eid]
    if not rows:
        abort(404, "No payroll data for this employee in this month")
    return render_template("payslips.html", rows=rows, month=month)


@app.route("/payslips")
@pay_required
def payslips():
    month = cur_month()
    _, rows = payroll_rows(month)
    return render_template("payslips.html", rows=[r for r in rows if r["pay"]["net"] or r["summary"]["payable_days"]], month=month)


# ------------------------------------------------------------------ exports

def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


PAY_COLS = [("Emp ID", lambda r: r["emp"]["device_user_id"]), ("Name", lambda r: r["emp"]["name"]),
            ("Department", lambda r: r["emp"].get("department", "")), ("Salary Type", lambda r: r["pay"]["salary_type"]),
            ("Rate", lambda r: r["pay"]["rate"]), ("Present", lambda r: r["summary"].get("cnt_P", 0)),
            ("Half Days", lambda r: r["summary"].get("cnt_HD", 0)), ("Absent", lambda r: r["summary"].get("cnt_A", 0)),
            ("Weekly Off", lambda r: r["summary"].get("cnt_WO", 0)), ("Worked on Off", lambda r: r["summary"].get("cnt_WOP", 0) + r["summary"].get("cnt_HP", 0)),
            ("Leaves", lambda r: r["summary"].get("leave_credit", 0)), ("Missing Punch", lambda r: r["summary"].get("cnt_MP", 0)),
            ("Late Marks", lambda r: r["summary"].get("late_count", 0)), ("Payable Days", lambda r: r["summary"]["payable_days"]),
            ("Worked Hrs", lambda r: round(r["summary"]["worked_min"] / 60, 2)), ("OT Hrs", lambda r: r["pay"]["ot_hours"]),
            ("Basic Earned", lambda r: r["pay"]["basic"]), ("OT Pay", lambda r: r["pay"]["ot_pay"]),
            ("Allowance", lambda r: r["pay"]["allowance"]), ("Bonus", lambda r: r["pay"]["bonus"]), ("Gross", lambda r: r["pay"]["gross"]),
            ("PF", lambda r: r["pay"]["pf"]), ("ESI", lambda r: r["pay"]["esi"]), ("Advance", lambda r: r["pay"]["advance"]),
            ("TDS", lambda r: r["pay"]["tds"]), ("Other Ded.", lambda r: r["pay"]["other_deduction"]),
            ("Total Ded.", lambda r: r["pay"]["total_deductions"]), ("Net Pay", lambda r: r["pay"]["net"]),
            ("Bank", lambda r: r["emp"].get("bank_name", "")), ("Account", lambda r: r["emp"].get("bank_account", "")),
            ("IFSC", lambda r: r["emp"].get("ifsc", ""))]


def xlsx_response(name, sheets):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for title, header, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F4E79")
        for r in rows:
            ws.append(list(r))
        ws.freeze_panes = "C2"
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 5), 30)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/payroll.<fmt>")
@pay_required
def export_payroll(fmt):
    month = cur_month()
    _, rows = payroll_rows(month)
    header = [c[0] for c in PAY_COLS]
    data = [[f(r) for _, f in PAY_COLS] for r in rows]
    if fmt == "csv":
        return csv_response(f"payroll_{month}.csv", header, data)
    return xlsx_response(f"payroll_{month}.xlsx", [(f"Payroll {month}", header, data)])


def report_rows(month, eid=None):
    """Live day tables; for a finalized month the frozen summary/pay snapshot is used for the money."""
    rows = sort_rows(payroll.compute_month(g.conn, month, employee_ids={eid} if eid else None))
    run, saved = payroll.load_saved(g.conn, month)
    if run and run["status"] == "final":
        snap = {it["employee_id"]: it for it in saved}
        for r in rows:
            if r["emp"]["id"] in snap:
                r["summary"], r["pay"] = snap[r["emp"]["id"]]["summary"], snap[r["emp"]["id"]]["pay"]
    return run, rows


@app.route("/reports/attendance")
def attendance_report():
    """Printable attendance statement per employee, with the salary calculation written out."""
    month = cur_month()
    eid = request.args.get("eid", type=int)
    run, rows = report_rows(month, eid)
    if not eid:
        rows = [r for r in rows if r["summary"]["worked_min"] or r["summary"]["payable_days"]]
    for r in rows:
        r["calc"] = reports.calc_lines(r, g.settings, g.settings.get("currency", "₹")) if can_pay() else []
        r["credit"] = [reports.day_credit(d, r["summary"].get("wo_paid", 1)) for d in r["days"]]
    return render_template("attendance_report.html", rows=rows, month=month, run=run)


@app.route("/export/payroll_formulas.xlsx")
@pay_required
def export_formula_xlsx():
    month = cur_month()
    eid = request.args.get("eid", type=int)
    _, rows = report_rows(month, eid)
    buf = reports.build_workbook(rows, month, g.settings, g.settings.get("company_name", ""))
    name = f"payroll_formulas_{month}" + (f"_{rows[0]['emp']['device_user_id']}" if eid and rows else "") + ".xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/bank.csv")
@pay_required
def export_bank():
    month = cur_month()
    _, rows = payroll_rows(month)
    return csv_response(f"bank_transfer_{month}.csv", ["Emp ID", "Name", "Bank", "Account No", "IFSC", "Amount"],
                        [[r["emp"]["device_user_id"], r["emp"]["name"], r["emp"].get("bank_name", ""), r["emp"].get("bank_account", ""),
                          r["emp"].get("ifsc", ""), r["pay"].get("bank", r["pay"]["net"])] for r in rows if r["pay"].get("bank", r["pay"]["net"]) > 0])


@app.route("/export/cash.csv")
@pay_required
def export_cash():
    month = cur_month()
    _, rows = payroll_rows(month)
    rows = attach_cash_paid(rows, month)
    return csv_response(f"cash_payments_{month}.csv",
                        ["Emp ID", "Name", "Department", "Net pay", "By bank", "Cash (calculated)", "Cash paid", "Difference", "Note", "Signature"],
                        [[r["emp"]["device_user_id"], r["emp"]["name"], r["emp"].get("department", ""), r["pay"]["net"],
                          r["pay"].get("bank", r["pay"]["net"]), r["pay"].get("cash", 0),
                          r["cash_paid"]["paid_amount"] if r["cash_paid"] else "", r["cash_diff"] if r["cash_paid"] else "",
                          r["cash_paid"]["note"] if r["cash_paid"] else "", ""]
                         for r in rows if r["pay"].get("cash", 0) > 0 or r["cash_paid"]])


def cash_paid_map(month, employee_ids=None):
    """{employee_id: row} of cash actually paid for the month."""
    q, args = "SELECT * FROM cash_payments WHERE month = ?", [month]
    if employee_ids:
        q += f" AND employee_id IN ({','.join('?' * len(employee_ids))})"
        args += list(employee_ids)
    return {r["employee_id"]: dict(r) for r in g.conn.execute(q, args)}


def attach_cash_paid(rows, month):
    paid = cash_paid_map(month)
    for r in rows:
        cp = paid.get(r["emp"]["id"])
        r["cash_paid"] = cp
        r["cash_diff"] = round(cp["paid_amount"] - r["pay"].get("cash", 0), 2) if cp else None
    return rows


def save_cash_paid(eid, month, text, note=""):
    """Record (or clear, if blank) the cash actually paid. Allowed after finalizing: it does not change the calculation."""
    text = (text or "").strip()
    if not text:
        n = g.conn.execute("DELETE FROM cash_payments WHERE employee_id=? AND month=? AND carried_month IS NULL", (eid, month)).rowcount
        return n
    amount = round(float(text.replace(",", "")), 2)   # any amount (rounded off or not), to the paisa
    if amount < 0:
        raise ValueError("negative")
    old = g.conn.execute("SELECT * FROM cash_payments WHERE employee_id=? AND month=?", (eid, month)).fetchone()
    if old and old["carried_month"] and abs(old["paid_amount"] - amount) > 0.005:
        raise ValueError(f"difference already carried to {old['carried_month']}; undo that adjustment first")
    g.conn.execute("""INSERT INTO cash_payments(employee_id, month, paid_amount, note, created_by, created_at) VALUES (?,?,?,?,?,?)
                      ON CONFLICT(employee_id, month) DO UPDATE SET paid_amount=excluded.paid_amount, note=excluded.note,
                      created_by=excluded.created_by, created_at=excluded.created_at""",
                   (eid, month, amount, note, g.user["username"], dt.datetime.now().strftime(TS_FMT)))
    audit("cash_paid", {"emp": eid, "month": month, "paid": amount, "note": note})
    return 1


@app.route("/payroll/cash", methods=["GET", "POST"])
@pay_required
def cash_sheet():
    """Printable cash distribution sheet: calculated cash, cash actually paid, difference, signature."""
    month = cur_month()
    if request.method == "POST":
        f = request.form
        n, errors = 0, []
        for eid in f.getlist("eid"):
            try:
                n += save_cash_paid(int(eid), month, f.get(f"paid_{eid}"), f.get(f"note_{eid}", "").strip())
            except ValueError as e:
                errors.append(f"#{eid}: {e}")
        flash(f"Saved cash paid for {n} employee(s)" + (f"; not saved: {', '.join(errors)}" if errors else ""),
              "error" if errors else "ok")
        return redirect(url_for("cash_sheet", month=month))
    _, rows = payroll_rows(month)
    paid = cash_paid_map(month)
    rows = attach_cash_paid([r for r in rows if r["pay"].get("cash", 0) > 0 or r["emp"]["id"] in paid], month)
    return render_template("cash_sheet.html", rows=rows, month=month, total=sum(r["pay"].get("cash", 0) for r in rows),
                           total_paid=sum(r["cash_paid"]["paid_amount"] for r in rows if r["cash_paid"]),
                           next_month=month_nav(month)[1])


@app.route("/employees/<int:eid>/cash_paid", methods=["POST"])
@pay_required
def cash_paid(eid):
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    month = f["month"]
    back = f.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("att_employee", eid=eid, month=month)
    try:
        save_cash_paid(eid, month, f.get("paid_amount"), f.get("note", "").strip())
        flash(f"Cash paid to {emp['name']} for {month} saved", "ok")
    except ValueError as e:
        flash(f"Not saved: {e}", "error")
    return redirect(back)


@app.route("/employees/<int:eid>/cash_carry", methods=["POST"])
@pay_required
def cash_carry(eid):
    """Carry the difference between cash paid and calculated cash into next month's pay."""
    emp = g.conn.execute("SELECT * FROM employees WHERE id = ?", (eid,)).fetchone() or abort(404)
    f = request.form
    month = f["month"]
    nxt = month_nav(month)[1]
    back = f.get("back", "")
    back = back if back.startswith("/") and not back.startswith("//") else url_for("att_employee", eid=eid, month=month)
    cp = g.conn.execute("SELECT * FROM cash_payments WHERE employee_id=? AND month=?", (eid, month)).fetchone()
    rows = attach_cash_paid([r for r in payroll_rows(month)[1] if r["emp"]["id"] == eid], month)
    if not cp or not rows or cp["carried_month"]:
        flash("Nothing to carry (no cash payment recorded, or already carried)", "error")
        return redirect(back)
    ensure_unlocked(nxt)
    diff = rows[0]["cash_diff"]
    if abs(diff) < 0.005:
        flash("Cash paid equals the calculated cash - nothing to carry", "ok")
        return redirect(back)
    kind, amount = ("allowance", -diff) if diff < 0 else ("deduction", diff)
    g.conn.execute("INSERT INTO adjustments(employee_id, month, kind, amount, note) VALUES (?,?,?,?,?)",
                   (eid, nxt, kind, round(amount, 2), f"cash {'short-paid' if diff < 0 else 'over-paid'} in {month}"))
    g.conn.execute("UPDATE cash_payments SET carried_month=? WHERE id=?", (nxt, cp["id"]))
    audit("cash_carry", {"emp": eid, "from": month, "to": nxt, "kind": kind, "amount": amount})
    flash(f"{emp['name']}: {abs(diff):,.2f} {'added to' if diff < 0 else 'deducted from'} {nxt} pay", "ok")
    return redirect(back)


def esi_rows(month):
    _, rows = payroll_rows(month)
    covered = [r for r in rows if r["pay"].get("esi_covered")]
    out = [r for r in rows if not r["pay"].get("esi_covered") and r["pay"].get("esi_wages", 0) > 0
           and payroll.esi_applicable(r["emp"], g.settings)]
    return covered, out


@app.route("/reports/esi")
@pay_required
def esi_report():
    """Monthly ESI contribution statement (employee 0.75% + employer 3.25% on wages u/s 2(88))."""
    month = cur_month()
    covered, out = esi_rows(month)
    tot = {k: sum(r["pay"].get(k, 0) for r in covered) for k in ("esi_wages", "esi", "esi_employer")}
    y, m = map(int, month.split("-"))
    due = dt.date(y + (m == 12), 1 if m == 12 else m + 1, 15)
    period = payroll.contribution_period(month)
    prev, nxt = month_nav(month)
    return render_template("esi_report.html", covered=covered, out=out, tot=tot, month=month, due=due, period=period,
                           prev=prev, nxt=nxt)


@app.route("/export/esic_mc.csv")
@pay_required
def export_esic_mc():
    """Monthly contribution file in the ESIC portal's upload layout (IP number, name, days, wages, reason code)."""
    month = cur_month()
    covered, _ = esi_rows(month)
    return csv_response(f"esic_monthly_contribution_{month}.csv",
                        ["IP Number (10 Digits)", "IP Name (Only alphabets and space)", "No of Days for which wages paid/payable during the month",
                         "Total Monthly Wages", "Reason Code for Zero workings days", "Last Working Day"],
                        [[r["emp"].get("esi_no", ""), "".join(ch for ch in r["emp"]["name"] if ch.isalpha() or ch == " ").strip(),
                          int(r["summary"]["payable_days"]) if float(r["summary"]["payable_days"]).is_integer() else r["summary"]["payable_days"],
                          int(round(r["pay"]["esi_wages"])), "" if r["summary"]["payable_days"] else "0", ""] for r in covered])


@app.route("/export/muster.xlsx")
def export_muster():
    month = cur_month()
    rows = sort_rows(payroll.compute_month(g.conn, month))
    start, end = engine.month_range(month)
    dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    header = ["Emp ID", "Name"] + [d.strftime("%d %a") for d in dates] + ["Present", "Half", "Absent", "WO", "WOP", "MP", "Payable", "Worked Hrs", "OT Hrs"]
    status_rows, hours_rows = [], []
    for r in rows:
        s = r["summary"]
        tail = [s.get("cnt_P", 0), s.get("cnt_HD", 0), s.get("cnt_A", 0), s.get("cnt_WO", 0), s.get("cnt_WOP", 0),
                s.get("cnt_MP", 0), s["payable_days"], round(s["worked_min"] / 60, 2), round(s["ot_min"] / 60, 2)]
        status_rows.append([r["emp"]["device_user_id"], r["emp"]["name"]] + [d["status"] for d in r["days"]] + tail)
        hours_rows.append([r["emp"]["device_user_id"], r["emp"]["name"]] +
                          [(f"{d['in']:%H:%M}-{d['out']:%H:%M} ({engine.fmt_min(d['worked_min'])})" if d["in"] and d["out"]
                            else (f"{d['in']:%H:%M} ?" if d["in"] else "")) for d in r["days"]] + tail)
    return xlsx_response(f"muster_{month}.xlsx", [("Status", header, status_rows), ("In-Out Hours", header, hours_rows)])


@app.route("/export/punches.csv")
def export_punches():
    month = cur_month()
    rows = g.conn.execute("""SELECT p.device_user_id, e.name, p.ts, p.source, p.deleted FROM punches p
                             LEFT JOIN employees e ON e.device_user_id = p.device_user_id WHERE p.ts LIKE ? ORDER BY p.ts""",
                          (month + "%",)).fetchall()
    return csv_response(f"punches_{month}.csv", ["Emp ID", "Name", "Timestamp", "Source", "Deleted"], [list(r) for r in rows])


# ------------------------------------------------------------------ settings / users / audit

@app.route("/settings", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        require_admin()
        f = request.form
        act = f.get("action")
        if act == "settings":
            for k in db.DEFAULT_SETTINGS:
                if k in f:
                    g.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, f[k].strip()))
            for k in ("weekly_off_paid_monthly", "weekly_off_paid_daily", "sandwich_rule", "sandwich_paid_leave", "short_day_rule",
                      "salary_components"):
                g.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (k, "1" if f.get(k) else "0"))
            audit("settings_save", {k: f[k] for k in f if k != "csrf"})
            flash("Settings saved", "ok")
        elif act == "add_user":
            role = f.get("role") if f.get("role") in ROLES else "hr"
            if len(f["password"]) < 6:
                flash("Password must be at least 6 characters", "error")
            else:
                try:
                    g.conn.execute("INSERT INTO app_users(username, password_hash, role) VALUES (?,?,?)",
                                   (f["username"].strip(), generate_password_hash(f["password"]), role))
                    audit("user_add", {"username": f["username"].strip(), "role": role})
                    flash(f"User {f['username'].strip()} added as {ROLES[role]}", "ok")
                except Exception as e:
                    flash(f"Could not add user: {e}", "error")
        elif act in ("delete_user", "set_role", "reset_password"):
            target = g.conn.execute("SELECT * FROM app_users WHERE id=?", (int(f["uid"]),)).fetchone() or abort(404)
            admins = g.conn.execute("SELECT COUNT(*) FROM app_users WHERE role='admin'").fetchone()[0]
            if act == "delete_user":
                if target["id"] == g.user["id"]:
                    flash("You cannot delete yourself", "error")
                elif target["role"] == "admin" and admins <= 1:
                    flash("Cannot delete the last admin", "error")
                else:
                    g.conn.execute("DELETE FROM app_users WHERE id=?", (target["id"],))
                    audit("user_delete", target["username"])
                    flash(f"Deleted user {target['username']}", "ok")
            elif act == "set_role":
                role = f.get("role")
                if role not in ROLES:
                    abort(400)
                if target["role"] == "admin" and role != "admin" and admins <= 1:
                    flash("Cannot remove the last admin", "error")
                else:
                    g.conn.execute("UPDATE app_users SET role=? WHERE id=?", (role, target["id"]))
                    audit("user_role", {"username": target["username"], "from": target["role"], "to": role})
                    flash(f"{target['username']} is now {ROLES[role]}", "ok")
            else:
                if len(f.get("password", "")) < 6:
                    flash("Password must be at least 6 characters", "error")
                else:
                    g.conn.execute("UPDATE app_users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), target["id"]))
                    audit("user_password_reset", target["username"])
                    flash(f"Password reset for {target['username']}", "ok")
        return redirect(url_for("settings_view"))
    users = g.conn.execute("SELECT id, username, role, created_at FROM app_users").fetchall()
    return render_template("settings.html", users=users)


@app.route("/password", methods=["POST"])
def change_password():
    f = request.form
    if not check_password_hash(g.user["password_hash"], f["old"]):
        flash("Current password is wrong", "error")
    elif len(f["new"]) < 6:
        flash("New password must be at least 6 characters", "error")
    else:
        g.conn.execute("UPDATE app_users SET password_hash=? WHERE id=?", (generate_password_hash(f["new"]), g.user["id"]))
        audit("password_change")
        flash("Password changed", "ok")
    return redirect(url_for("settings_view"))


@app.route("/audit")
@admin_required
def audit_view():
    rows = g.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
    return render_template("audit.html", rows=rows)


@app.route("/backup")
@admin_required
def backup():
    g.conn.commit()
    path = os.path.join(os.path.dirname(db.DB_PATH), f"backup_{dt.datetime.now():%Y%m%d_%H%M%S}.db")
    src = db.connect()
    dst = __import__("sqlite3").connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    audit("backup", path)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
def http_error(e):
    return render_template("error.html", e=e), e.code


def create_app(start_sync=True):
    db.init_db()
    if start_sync:
        device.start_auto_sync()
    return app
