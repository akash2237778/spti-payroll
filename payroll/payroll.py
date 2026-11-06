"""Salary calculation and payroll runs."""
import calendar
import datetime as dt
import json
from decimal import Decimal, ROUND_HALF_UP

from . import db, engine

ADJ_EARNINGS = ("allowance", "bonus")
ADJ_DEDUCTIONS = ("deduction", "advance", "tds")


def round_half_up(x):
    """Same rounding as Excel's ROUND(x, 0), so the formula workbook matches to the rupee."""
    return float(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def r2(x):
    return round(float(x) + 1e-9, 2)


def calculate(emp, summary, month, settings, adjustments=()):
    """Pure salary calculation for one employee-month. Returns a dict of line items."""
    y, m = map(int, month.split("-"))
    cal_days = calendar.monthrange(y, m)[1]
    divisor = cal_days if settings.get("month_days_mode", "calendar") == "calendar" else int(settings.get("fixed_month_days") or 30)
    std_hours = (summary["std_min"] or 480) / 60.0
    rate = float(emp["salary_rate"] or 0)
    stype = emp["salary_type"] or "monthly"
    payable = summary["payable_days"]

    if stype == "monthly":
        per_day = rate / divisor
        basic = per_day * payable
        hour_rate = per_day / std_hours
    elif stype == "daily":
        per_day = rate
        basic = per_day * payable
        hour_rate = per_day / std_hours
    else:  # hourly
        hour_rate = rate
        per_day = rate * std_hours
        normal_hours = max(0.0, (summary["worked_min"] - summary.get("ot_min_gross", summary["ot_min"])) / 60.0)
        basic = rate * (normal_hours + summary["paid_off_days"] * std_hours)

    # Staff are paid on payable days only: overtime hours are recorded but never paid.
    is_staff = (emp.get("category") or "worker") == "staff"
    ot_hours = summary["ot_min"] / 60.0 if emp["ot_enabled"] and not is_staff else 0.0
    ot_pay = ot_hours * hour_rate * float(emp["ot_multiplier"] or 1.0)

    earn_adj = {k: 0.0 for k in ADJ_EARNINGS}
    ded_adj = {k: 0.0 for k in ADJ_DEDUCTIONS}
    adj_lines = []
    for a in adjustments:
        kind = a["kind"]
        if kind in earn_adj:
            earn_adj[kind] += float(a["amount"])
        elif kind in ded_adj:
            ded_adj[kind] += float(a["amount"])
        adj_lines.append({"kind": kind, "amount": float(a["amount"]), "note": a["note"]})

    gross = basic + ot_pay + earn_adj["allowance"] + earn_adj["bonus"]

    pf = 0.0
    if emp["pf_enabled"]:
        pf = min(basic, float(settings.get("pf_wage_ceiling") or 15000)) * float(settings.get("pf_rate") or 12) / 100
    esi = 0.0
    if emp["esi_enabled"] and gross <= float(settings.get("esi_gross_ceiling") or 21000):
        esi = gross * float(settings.get("esi_rate") or 0.75) / 100

    total_ded = pf + esi + sum(ded_adj.values())
    net = gross - total_ded
    return {
        "salary_type": stype, "rate": rate, "divisor": divisor, "per_day": r2(per_day), "hour_rate": r2(hour_rate),
        "payable_days": payable, "basic": r2(basic), "ot_hours": r2(ot_hours), "ot_pay": r2(ot_pay),
        "allowance": r2(earn_adj["allowance"]), "bonus": r2(earn_adj["bonus"]), "gross": r2(gross),
        "pf": r2(pf), "esi": r2(esi), "advance": r2(ded_adj["advance"]), "tds": r2(ded_adj["tds"]),
        "other_deduction": r2(ded_adj["deduction"]), "total_deductions": r2(total_ded),
        "net": round_half_up(net), "adjustments": adj_lines,
        "ot_hours_gross": r2(summary.get("ot_min_gross", summary["ot_min"]) / 60.0),
        "late_penalty_hours": r2(summary.get("late_penalty_min", 0) / 60.0),
        "std_hours": r2(std_hours), "category": "staff" if is_staff else "worker",
    }


def compute_month(conn, month, employee_ids=None, settings=None, today=None):
    """Attendance + salary for all (active-in-month) employees. Returns list of rows."""
    settings = settings or db.get_settings(conn)
    start, end = engine.month_range(month)
    q = """SELECT * FROM employees WHERE (join_date IS NULL OR join_date <= ?)
           AND (exit_date IS NULL OR exit_date = '' OR exit_date >= ?)"""
    args = [end.isoformat(), start.isoformat()]
    emps = [dict(e) for e in conn.execute(q + " ORDER BY CAST(device_user_id AS INTEGER)", args)]
    if employee_ids is not None:
        emps = [e for e in emps if e["id"] in employee_ids]
    # skip employees with no activity at all in month who are flagged inactive
    data = engine.employee_days(conn, emps, start, end, settings, today=today)
    adjs = {}
    for a in conn.execute("SELECT * FROM adjustments WHERE month = ?", (month,)):
        adjs.setdefault(a["employee_id"], []).append(dict(a))
    overrides = month_overrides(conn, month)
    rows = []
    for e in emps:
        d = data[e["id"]]
        summary = engine.summarize(d["days"], d["shift"], settings, e["salary_type"], d.get("before", ()), d.get("after", ()))
        apply_month_override(summary, overrides.get(e["id"]))
        if not e["active"] and summary["worked_min"] == 0 and not adjs.get(e["id"]):
            continue
        pay = calculate(e, summary, month, settings, adjs.get(e["id"], []))
        rows.append({"emp": e, "days": d["days"], "shift": d["shift"], "summary": summary, "pay": pay})
    return rows


def month_overrides(conn, month):
    return {r["employee_id"]: dict(r) for r in conn.execute("SELECT * FROM month_overrides WHERE month = ?", (month,))}


def apply_month_override(summary, ov):
    """Final payable days / OT edited by a person win over the calculated ones; the calculated values are kept
    in calc_* so screens can show Calculated vs Edited."""
    summary["calc_payable_days"] = summary["payable_days"]
    summary["calc_ot_min"] = summary["ot_min"]
    summary["edited"] = {}
    if not ov:
        return summary
    if ov.get("payable_days") is not None:
        summary["payable_days"] = float(ov["payable_days"])
        summary["edited"]["payable_days"] = True
    if ov.get("ot_min") is not None:
        summary["ot_min"] = float(ov["ot_min"])
        summary["edited"]["ot_min"] = True
    if summary["edited"]:
        summary["edit_reason"], summary["edit_by"], summary["edit_at"] = ov.get("reason") or "", ov.get("created_by") or "", ov.get("created_at") or ""
    return summary


def run_status(conn, month):
    r = conn.execute("SELECT * FROM payroll_runs WHERE month = ?", (month,)).fetchone()
    return r["status"] if r else None


def is_locked(conn, date_str):
    return run_status(conn, date_str[:7]) == "final"


def save_run(conn, month, user, finalize=False):
    if run_status(conn, month) == "final":
        raise ValueError(f"Payroll for {month} is already finalized")
    rows = compute_month(conn, month)
    conn.execute("INSERT OR IGNORE INTO payroll_runs(month) VALUES (?)", (month,))
    run_id = conn.execute("SELECT id FROM payroll_runs WHERE month = ?", (month,)).fetchone()["id"]
    conn.execute("DELETE FROM payroll_items WHERE run_id = ?", (run_id,))
    for row in rows:
        details = {"summary": row["summary"], "pay": row["pay"],
                   "emp": {k: row["emp"][k] for k in ("name", "device_user_id", "emp_code", "department", "designation",
                                                      "bank_name", "bank_account", "ifsc", "uan", "esi_no")},
                   "days": [{"date": d["date"].isoformat(), "status": d["status"], "worked_min": d["worked_min"],
                             "ot_min": d["ot_min"]} for d in row["days"]]}
        conn.execute("INSERT INTO payroll_items(run_id, employee_id, gross, total_deductions, net, details) VALUES (?,?,?,?,?,?)",
                     (run_id, row["emp"]["id"], row["pay"]["gross"], row["pay"]["total_deductions"], row["pay"]["net"],
                      json.dumps(details, default=str)))
    if finalize:
        conn.execute("UPDATE payroll_runs SET status='final', finalized_at=?, finalized_by=? WHERE id=?",
                     (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user, run_id))
    db.audit(conn, user, "payroll_finalize" if finalize else "payroll_save", {"month": month, "employees": len(rows)})
    return run_id, len(rows)


def load_saved(conn, month):
    run = conn.execute("SELECT * FROM payroll_runs WHERE month = ?", (month,)).fetchone()
    if not run:
        return None, []
    items = []
    for it in conn.execute("SELECT * FROM payroll_items WHERE run_id = ? ORDER BY employee_id", (run["id"],)):
        d = json.loads(it["details"])
        d["employee_id"] = it["employee_id"]
        items.append(d)
    return dict(run), items
