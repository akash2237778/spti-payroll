"""Salary calculation and payroll runs."""
import calendar
import datetime as dt
import json
import math
from decimal import Decimal, ROUND_HALF_UP

from . import db, engine

ADJ_EARNINGS = ("allowance", "bonus")
ADJ_DEDUCTIONS = ("deduction", "advance", "tds")


def round_half_up(x):
    """Same rounding as Excel's ROUND(x, 0), so the formula workbook matches to the rupee."""
    return float(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def r2(x):
    return round(float(x) + 1e-9, 2)


PAY_MODES = {"bank": "All by bank", "cash": "All in cash", "fixed": "Fixed amount by bank, rest cash",
             "percent": "Percentage by bank, rest cash", "esi": "ESI wages by bank, rest cash"}


def split_bank_cash(emp, net, bank_override=None, esi_wages_amount=None):
    """(bank, cash, how) for a net amount: this month's override wins, else the employee's default pay mode."""
    mode = (emp.get("pay_mode") or "bank")
    val = float(emp.get("bank_value") or 0)
    if bank_override is not None:
        bank, how = float(bank_override), "set for this month"
    elif mode == "cash":
        bank, how = 0.0, PAY_MODES["cash"]
    elif mode == "fixed":
        bank, how = val, f"fixed {val:g} by bank"
    elif mode == "percent":
        bank, how = round_half_up(net * val / 100), f"{val:g}% by bank"
    elif mode == "esi":
        bank, how = round_half_up(esi_wages_amount or 0), "ESI wages by bank"
    else:
        bank, how = net, PAY_MODES["bank"]
    bank = max(0.0, min(float(net), bank))
    return bank, float(net) - bank, how


def esi_applicable(emp, settings):
    """ESI is deducted only for employees with ESI ticked (owner, 2026-11-06: unticked = no ESI)."""
    return bool(emp.get("esi_enabled"))


def esi_ceiling(emp, settings):
    if emp.get("disabled_person"):
        return float(settings.get("esi_disabled_ceiling") or 25000)
    return float(settings.get("esi_gross_ceiling") or 21000)


def esi_wages(basic, ot_pay, allowance=0.0, bonus=0.0, excluded_parts=0.0):
    """Wages u/s 2(88) Code on Social Security 2020: total remuneration minus the excluded heads - overtime
    allowance, and HRA / conveyance when the salary is split into components (`excluded_parts`, already inside
    `basic`) - except that excluded heads above one-half of total remuneration are added back."""
    total = basic + ot_pay + allowance + bonus
    excluded = ot_pay + excluded_parts
    return total - excluded + max(0.0, excluded - total / 2)


def salary_parts(emp, settings, earned):
    """Split earned salary into Basic+DA / HRA / Conveyance / Other allowance in the employee's proportions.
    Without components (setting off or nothing entered) everything is Basic+DA."""
    rate = float(emp.get("salary_rate") or 0)
    parts = {"basic_da": earned, "hra": 0.0, "conveyance": 0.0, "other": 0.0}
    if settings.get("salary_components") != "1" or rate <= 0:
        return parts
    h, c, o = (float(emp.get(k) or 0) for k in ("comp_hra", "comp_conveyance", "comp_other"))
    if h + c + o <= 0 or h + c + o > rate + 0.005:
        return parts
    parts.update(hra=earned * h / rate, conveyance=earned * c / rate, other=earned * o / rate)
    parts["basic_da"] = earned - parts["hra"] - parts["conveyance"] - parts["other"]
    return parts


def round_up(x):
    """ESIC: each contribution is rounded to the next higher rupee."""
    return float(math.ceil(round(x, 6)))


def contribution_period(month):
    """ESIC contribution periods: April-September and October-March. Returns the period's first month."""
    y, m = map(int, month.split("-"))
    if 4 <= m <= 9:
        return f"{y}-04"
    return f"{y if m >= 10 else y - 1}-10"


def esi_contribution(emp, settings, *, basic, ot_pay, allowance=0.0, bonus=0.0, payable_days=0, covered=None, excluded_parts=0.0):
    """Employee and employer ESI for one month.
    covered=None decides coverage on this month's wages; True/False is the contribution-period decision
    (once covered in a period, contributions continue to its end even if wages rise above the ceiling)."""
    out = {"esi": 0.0, "esi_employer": 0.0, "esi_wages": 0.0, "esi_covered": False, "esi_note": ""}
    if not esi_applicable(emp, settings):
        out["esi_note"] = "ESI not applicable"
        return out
    wages = esi_wages(basic, ot_pay, allowance, bonus, excluded_parts)
    out["esi_wages"] = r2(wages)
    within = wages <= esi_ceiling(emp, settings)
    is_covered = within if covered is None else bool(covered or within)
    out["esi_covered"] = is_covered
    if not is_covered:
        out["esi_note"] = f"ESI wages above {esi_ceiling(emp, settings):,.0f}: not covered this period"
        return out
    if wages <= 0:
        out["esi_note"] = "no wages this month"
        return out
    if not within:
        out["esi_note"] = "covered from the start of the contribution period"
    out["esi_employer"] = round_up(wages * float(settings.get("esi_employer_rate") or 3.25) / 100)
    daily = wages / payable_days if payable_days else 0
    if payable_days and daily <= float(settings.get("esi_low_wage_daily") or 176):
        out["esi_note"] = (out["esi_note"] + "; " if out["esi_note"] else "") + \
            f"average daily wage {daily:,.2f} ≤ {float(settings.get('esi_low_wage_daily') or 176):g}: no employee share"
        return out
    out["esi"] = round_up(wages * float(settings.get("esi_rate") or 0.75) / 100)
    return out


def calculate(emp, summary, month, settings, adjustments=(), bank_override=None, esi_covered=None):
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

    parts = salary_parts(emp, settings, basic)
    pf = 0.0
    if emp["pf_enabled"]:   # PF on Basic + DA (the whole salary when it is not split into components)
        pf = min(parts["basic_da"], float(settings.get("pf_wage_ceiling") or 15000)) * float(settings.get("pf_rate") or 12) / 100
    esi_info = esi_contribution(emp, settings, basic=basic, ot_pay=ot_pay, allowance=earn_adj["allowance"],
                                bonus=earn_adj["bonus"], payable_days=payable, covered=esi_covered,
                                excluded_parts=parts["hra"] + parts["conveyance"])
    esi = esi_info["esi"]

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
        **dict(zip(("bank", "cash", "pay_how"), split_bank_cash(emp, round_half_up(net), bank_override, esi_info["esi_wages"]))),
        "basic_da": r2(parts["basic_da"]), "hra": r2(parts["hra"]), "conveyance": r2(parts["conveyance"]),
        "other_allowance": r2(parts["other"]), "components": parts["hra"] + parts["conveyance"] + parts["other"] > 0,
        "bank_override": bank_override is not None,
        "esi_wages": esi_info["esi_wages"], "esi_employer": esi_info["esi_employer"],
        "esi_covered": esi_info["esi_covered"], "esi_note": esi_info["esi_note"],
    }


def compute_month(conn, month, employee_ids=None, settings=None, today=None, _esi_cache=None):
    """Attendance + salary for all (active-in-month) employees. Returns list of rows."""
    settings = settings or db.get_settings(conn)
    _esi_cache = {} if _esi_cache is None else _esi_cache
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
    splits = {r["employee_id"]: r["bank_amount"] for r in conn.execute("SELECT * FROM pay_splits WHERE month = ?", (month,))}
    rows = []
    for e in emps:
        d = data[e["id"]]
        summary = engine.summarize(d["days"], d["shift"], settings, e["salary_type"], d.get("before", ()), d.get("after", ()))
        apply_month_override(summary, overrides.get(e["id"]))
        if not e["active"] and summary["worked_min"] == 0 and not adjs.get(e["id"]):
            continue
        pay = calculate(e, summary, month, settings, adjs.get(e["id"], []), splits.get(e["id"]))
        if (not pay["esi_covered"] and esi_applicable(e, settings) and pay["esi_wages"] > 0
                and esi_covered_earlier_in_period(conn, e["id"], month, settings, _esi_cache)):
            pay = calculate(e, summary, month, settings, adjs.get(e["id"], []), splits.get(e["id"]), esi_covered=True)
        rows.append({"emp": e, "days": d["days"], "shift": d["shift"], "summary": summary, "pay": pay})
    return rows


def esi_covered_earlier_in_period(conn, eid, month, settings, cache=None):
    """True if the employee was ESI-covered in an earlier month of the same contribution period
    (finalized months use their frozen figures)."""
    cache = {} if cache is None else cache
    start = contribution_period(month)
    m = start
    while m < month:
        key = (eid, m)
        if key not in cache:
            run = conn.execute("SELECT id, status FROM payroll_runs WHERE month = ?", (m,)).fetchone()
            item = None
            if run and run["status"] == "final":
                item = conn.execute("SELECT details FROM payroll_items WHERE run_id = ? AND employee_id = ?", (run["id"], eid)).fetchone()
            if item:
                cache[key] = bool(json.loads(item["details"])["pay"].get("esi_covered"))
            else:
                rows = compute_month(conn, m, employee_ids={eid}, settings=settings, _esi_cache=cache)
                cache[key] = bool(rows and rows[0]["pay"]["esi_covered"])
        if cache[key]:
            return True
        y, mo = map(int, m.split("-"))
        m = f"{y + (mo == 12)}-{1 if mo == 12 else mo + 1:02d}"
    return False


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


def advance_ledger(conn, month, employee_ids=None):
    """Per employee: advances given (up to this month), recovered before / in this month, balance.
    Recoveries are the 'advance' adjustments, one month at a time."""
    end = month + "-31"
    led = {}
    q_ids = "" if employee_ids is None else f" AND employee_id IN ({','.join(str(int(i)) for i in employee_ids)})"
    for r in conn.execute(f"SELECT * FROM advances WHERE given_date <= ?{q_ids} ORDER BY given_date, id", (end,)):
        e = led.setdefault(r["employee_id"], {"given": 0.0, "recovered_before": 0.0, "this_month": 0.0, "items": [], "installment": None})
        e["given"] += float(r["amount"])
        e["items"].append(dict(r))
        if r["installment"]:
            e["installment"] = float(r["installment"])
    for r in conn.execute(f"SELECT employee_id, month, SUM(amount) AS s FROM adjustments WHERE kind = 'advance'{q_ids} GROUP BY employee_id, month"):
        e = led.setdefault(r["employee_id"], {"given": 0.0, "recovered_before": 0.0, "this_month": 0.0, "items": [], "installment": None})
        if r["month"] < month:
            e["recovered_before"] += float(r["s"])
        elif r["month"] == month:
            e["this_month"] += float(r["s"])
    for e in led.values():
        e["outstanding_before"] = round(e["given"] - e["recovered_before"], 2)
        e["remaining"] = round(e["outstanding_before"] - e["this_month"], 2)
        inst = e["installment"] or e["outstanding_before"]
        e["suggest"] = max(0.0, round(min(inst, e["outstanding_before"]), 2))
    return led


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
