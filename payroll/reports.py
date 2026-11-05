"""Per-employee attendance statements: day-level helpers, plain-language calculation lines, and an
Excel workbook in which every total and every rupee is a live formula over the day table."""
import io
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import engine

HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
SUB_FILL = PatternFill("solid", fgColor="DCE6F1")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
BOLD = Font(bold=True)
WHITE_BOLD = Font(bold=True, color="FFFFFF")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def day_credit(d, wo_paid):
    """Payable credit this day contributes (before the month-level late-mark deduction)."""
    off = d["off_credit"] if (d["off_kind"] != "wo" or wo_paid) else 0.0
    return d["work_credit"] + d.get("extra_credit", 0.0) + off


def money(v, cur="₹"):
    return f"{cur}{float(v):,.2f}"


def num(v):
    v = float(v or 0)
    return f"{v:g}" if v == int(v) else f"{v:.2f}".rstrip("0").rstrip(".")


def calc_lines(row, settings, cur="₹"):
    """The salary calculation spelled out step by step, as (label, working, result) tuples."""
    s, p = row["summary"], row["pay"]
    lines = []
    wo = s.get("wo_credit", 0) if s.get("wo_paid", 1) else 0
    parts = [f"{num(s.get('work_credit', 0))} worked"]
    if s.get("extra_credit"):
        parts.append(f"{num(s['extra_credit'])} extra (work on off-day/holiday/long duty)")
    if wo:
        parts.append(f"{num(wo)} paid weekly off")
    if s.get("holiday_credit"):
        parts.append(f"{num(s['holiday_credit'])} paid holiday")
    if s.get("leave_credit"):
        parts.append(f"{num(s['leave_credit'])} paid leave")
    working = " + ".join(parts)
    if s.get("late_deduction_days"):
        working += f" − {num(s['late_deduction_days'])} late-mark deduction"
    lines.append(("Payable days", working, num(s["payable_days"])))
    std_h = p.get("std_hours") or s["std_min"] / 60
    if p["salary_type"] == "monthly":
        lines.append(("Per-day rate", f"{money(p['rate'], cur)} ÷ {p['divisor']} days", money(p["per_day"], cur)))
        lines.append(("Basic earned", f"{money(p['per_day'], cur)} × {num(s['payable_days'])} payable days", money(p["basic"], cur)))
        lines.append(("Hourly rate (for OT)", f"{money(p['per_day'], cur)} ÷ {num(std_h)} standard hours", money(p["hour_rate"], cur)))
    elif p["salary_type"] == "daily":
        lines.append(("Basic earned", f"{money(p['rate'], cur)} per day × {num(s['payable_days'])} payable days", money(p["basic"], cur)))
        lines.append(("Hourly rate (for OT)", f"{money(p['rate'], cur)} ÷ {num(std_h)} standard hours", money(p["hour_rate"], cur)))
    else:
        normal = max(0.0, (s["worked_min"] - s.get("ot_min_gross", s["ot_min"])) / 60)
        lines.append(("Basic earned", f"{money(p['rate'], cur)}/h × ({num(round(normal, 2))} normal hours + "
                      f"{num(s['paid_off_days'])} paid off-days × {num(std_h)} h)", money(p["basic"], cur)))
    gross_ot = s.get("ot_min_gross", s["ot_min"]) / 60
    if s.get("late_penalty_min") or s.get("early_short_min"):
        parts = [f"{engine.fmt_min(s.get('ot_min_gross', 0))} earned"]
        if s.get("late_penalty_min"):
            parts.append(f"{engine.fmt_min(s['late_penalty_min'])} late penalty ({num(s.get('late_penalty_days', 0))} day(s))")
        if s.get("early_short_min"):
            parts.append(f"{engine.fmt_min(s['early_short_min'])} short on early-leave full days ({num(s.get('early_short_days', 0))} day(s))")
        lines.append(("Overtime hours", " − ".join(parts), engine.fmt_min(s["ot_min"])))
        if s.get("late_penalty_unrecovered_min"):
            lines.append(("", f"deductions larger than OT; {engine.fmt_min(s['late_penalty_unrecovered_min'])} not recovered (OT cannot go below 0)", ""))
    else:
        lines.append(("Overtime hours", f"{num(round(gross_ot, 2))} h beyond standard shift", engine.fmt_min(s["ot_min"])))
    lines.append(("Overtime pay", f"{num(p['ot_hours'])} h × {money(p['hour_rate'], cur)} × {num(row['emp'].get('ot_multiplier') or 1)}"
                  + ("" if row["emp"].get("ot_enabled", 1) else " (not OT eligible)"), money(p["ot_pay"], cur)))
    g_parts = [f"{money(p['basic'], cur)} basic", f"{money(p['ot_pay'], cur)} OT"]
    if p["allowance"]:
        g_parts.append(f"{money(p['allowance'], cur)} allowance")
    if p["bonus"]:
        g_parts.append(f"{money(p['bonus'], cur)} bonus")
    lines.append(("Gross", " + ".join(g_parts), money(p["gross"], cur)))
    if p["pf"]:
        lines.append(("PF", f"{settings.get('pf_rate')}% × min({money(p['basic'], cur)}, {money(settings.get('pf_wage_ceiling'), cur)})", money(p["pf"], cur)))
    if p["esi"]:
        lines.append(("ESI", f"{settings.get('esi_rate')}% × {money(p['gross'], cur)}", money(p["esi"], cur)))
    for k, label in (("advance", "Advance recovery"), ("tds", "TDS"), ("other_deduction", "Other deduction")):
        if p[k]:
            lines.append((label, "", money(p[k], cur)))
    lines.append(("Total deductions", "", money(p["total_deductions"], cur)))
    lines.append(("Net pay", f"{money(p['gross'], cur)} − {money(p['total_deductions'], cur)}, rounded to the rupee", money(p["net"], cur)))
    return lines


# ------------------------------------------------------------------------------------------------
# Excel with formulas
# ------------------------------------------------------------------------------------------------

def _sheet_name(emp, used):
    base = re.sub(r"[\[\]:*?/\\']", "", f"{emp['device_user_id']} {emp['name']}")[:31] or "Employee"
    name, n = base, 2
    while name.lower() in used:
        name = f"{base[:28]}_{n}"
        n += 1
    used.add(name.lower())
    return name


def _hdr(ws, row, values):
    for i, v in enumerate(values, 1):
        c = ws.cell(row=row, column=i, value=v)
        c.font, c.fill, c.border = WHITE_BOLD, HEAD_FILL, BOX
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _one_window(d, shift, paid_break):
    """Excel's worked-minutes formula looks at one lunch window (the in-day's); use it only when that is enough."""
    if paid_break or not shift.get("break_start"):
        return True
    a, b = d["in"], d["out"]
    one = 0.0
    ws = engine.dt.datetime.combine(a.date(), engine.parse_hm(shift["break_start"]))
    we = ws + engine.dt.timedelta(minutes=int(shift["break_min"] or 0))
    one = max(0.0, (min(b, we) - max(a, ws)).total_seconds() / 60)
    return abs(one - engine.break_minutes(a, b, shift, paid_break)) < 1e-6


def _simple_session(d):
    """Day whose worked minutes can be re-derived from In/Out alone (one plain in->out session)."""
    comp = [s for s in d["sessions"] if not s["orphan"]]
    return len(comp) == 1 and len(comp[0]["punches"]) == 2 and not any(s["orphan"] for s in d["sessions"]) \
        and "manual times" not in d["flags"]


def build_workbook(rows, month, settings, company=""):
    wb = Workbook()
    rules = wb.active
    rules.title = "Rules"
    R = {}
    rule_rows = [
        ("PF employee %", float(settings.get("pf_rate") or 0), "pf_rate"),
        ("PF wage ceiling", float(settings.get("pf_wage_ceiling") or 0), "pf_ceil"),
        ("ESI employee %", float(settings.get("esi_rate") or 0), "esi_rate"),
        ("ESI applies if gross ≤", float(settings.get("esi_gross_ceiling") or 0), "esi_ceil"),
        ("Late marks per ½-day deduction (0 = off)", int(settings.get("late_marks_per_halfday") or 0), "late_half"),
    ]
    rules["A1"] = f"{company} payroll rules used for {month}"
    rules["A1"].font = Font(bold=True, size=13)
    for i, (label, val, key) in enumerate(rule_rows, 3):
        rules.cell(row=i, column=1, value=label)
        c = rules.cell(row=i, column=2, value=val)
        c.fill = INPUT_FILL
        R[key] = f"Rules!$B${i}"
    rules["A12"] = "Late penalty: on each employee sheet, column K (yellow) holds the OT minutes taken off for that day."
    rules["A13"] = "Yellow cells are inputs: change them and every sheet recalculates."
    rules["A14"] = "Each employee has a sheet with the day-by-day table; the Payroll sheet pulls totals from those sheets."
    rules.column_dimensions["A"].width = 48
    rules.column_dimensions["B"].width = 14

    pay_ws = wb.create_sheet("Payroll", 0)
    used = {"rules", "payroll"}
    refs = []
    for row in rows:
        refs.append(_employee_sheet(wb, row, month, settings, R, _sheet_name(row["emp"], used)))

    cols = [("Emp ID", None), ("Name", None), ("Department", None), ("Type", "type"), ("Rate", "rate"),
            ("Payable days", "payable"), ("Worked hrs", "worked_h"), ("OT hrs (net)", "ot_h"), ("Per day", "per_day"),
            ("Hour rate", "hour_rate"), ("Basic", "basic"), ("OT pay", "ot_pay"), ("Allowance", "allow"), ("Bonus", "bonus"),
            ("Gross", "gross"), ("PF", "pf"), ("ESI", "esi"), ("Advance", "adv"), ("TDS", "tds"), ("Other ded.", "oth"),
            ("Total ded.", "ded"), ("Net pay", "net")]
    pay_ws["A1"] = f"{company} payroll register {month} — every figure links to the employee's sheet"
    pay_ws["A1"].font = Font(bold=True, size=13)
    _hdr(pay_ws, 3, [c[0] for c in cols])
    r = 4
    for row, (sheet, cells) in zip(rows, refs):
        e = row["emp"]
        pay_ws.cell(row=r, column=1, value=e["device_user_id"])
        name = pay_ws.cell(row=r, column=2, value=e["name"])
        name.hyperlink = f"#'{sheet}'!A1"
        name.font = Font(color="0563C1", underline="single")
        pay_ws.cell(row=r, column=3, value=e.get("department") or "")
        for ci, (_, key) in enumerate(cols[3:], 4):
            c = pay_ws.cell(row=r, column=ci, value=f"='{sheet}'!{cells[key]}")
            if ci >= 9:
                c.number_format = "#,##0.00"
        pay_ws.cell(row=r, column=len(cols)).number_format = "#,##0"
        r += 1
    last = r - 1
    pay_ws.cell(row=r, column=2, value="TOTAL").font = BOLD
    for ci in range(6, len(cols) + 1):
        if ci in (9, 10):
            continue
        L = get_column_letter(ci)
        c = pay_ws.cell(row=r, column=ci, value=f"=SUM({L}4:{L}{last})" if last >= 4 else 0)
        c.font = BOLD
        c.number_format = "#,##0.00"
    for ci in range(1, len(cols) + 1):
        pay_ws.column_dimensions[get_column_letter(ci)].width = 16 if ci == 2 else 11
    pay_ws.freeze_panes = "C4"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _employee_sheet(wb, row, month, settings, R, title):
    ws = wb.create_sheet(title)
    e, s, p, days = row["emp"], row["summary"], row["pay"], row["days"]
    wo_paid = 1 if s.get("wo_paid", 1) else 0
    ws["A1"] = f"{e['name']} (#{e['device_user_id']}) — attendance & salary {month}"
    ws["A1"].font = Font(bold=True, size=13)
    params = [("Salary type", p["salary_type"]), ("Rate", p["rate"]), ("Month divisor (days)", p["divisor"]),
              ("Standard shift hours", round(s["std_min"] / 60, 4)), ("OT eligible (1/0)", 1 if e.get("ot_enabled", 1) else 0),
              ("OT multiplier", float(e.get("ot_multiplier") or 1)), ("PF (1/0)", 1 if e.get("pf_enabled") else 0),
              ("ESI (1/0)", 1 if e.get("esi_enabled") else 0), ("Weekly off paid (1/0)", wo_paid),
              ("Paid lunch break (1/0)", 1 if e.get("paid_break") else 0)]
    P = {}
    for i, (label, val) in enumerate(params, 3):
        ws.cell(row=i, column=1, value=label)
        c = ws.cell(row=i, column=2, value=val)
        c.fill = INPUT_FILL
        P[label] = f"$B${i}"
    T, BA, BR = P["Salary type"], P["Rate"], P["Month divisor (days)"]
    STD, OTE, OTM, PFE, ESE, WOP = (P["Standard shift hours"], P["OT eligible (1/0)"], P["OT multiplier"], P["PF (1/0)"],
                                    P["ESI (1/0)"], P["Weekly off paid (1/0)"])
    PB = P["Paid lunch break (1/0)"]
    paid_break = bool(e.get("paid_break"))

    head = ["Date", "Day", "Shift", "Status", "In", "Out", "Worked min", "Late min", "Early min", "OT min", "Late penalty min",
            "Work credit", "Extra credit", "Weekly-off credit", "Holiday credit", "Leave credit", "Notes",
            "OT starts after (min)", "Break min", "Break after min", "OT after min", "OT block min", "Lunch from", "Lunch to",
            "Early-leave short min"]
    H = 14
    _hdr(ws, H, head)
    ws.row_dimensions[H].height = 30
    first = H + 1
    double = settings.get("long_duty_mode") == "double"
    ot_mode = settings.get("wo_work_mode") == "ot"
    for i, d in enumerate(days):
        r = first + i
        sh = d.get("shift") or row["shift"]
        ws.cell(row=r, column=1, value=d["date"]).number_format = "dd-mmm-yy"
        ws.cell(row=r, column=2, value=d["date"].strftime("%a"))
        ws.cell(row=r, column=3, value=sh["name"])
        ws.cell(row=r, column=4, value=d["status"])
        if d["in"]:
            ws.cell(row=r, column=5, value=d["in"]).number_format = "dd-mmm hh:mm"
        if d["out"]:
            ws.cell(row=r, column=6, value=d["out"]).number_format = "dd-mmm hh:mm"
        ws.cell(row=r, column=18, value=engine.ot_base_minutes(sh, paid_break))
        if sh.get("break_start") and d["in"]:
            lw = engine.dt.datetime.combine(d["in"].date(), engine.parse_hm(sh["break_start"]))
            ws.cell(row=r, column=23, value=lw).number_format = "dd-mmm hh:mm"
            ws.cell(row=r, column=24, value=lw + engine.dt.timedelta(minutes=int(sh["break_min"] or 0))).number_format = "dd-mmm hh:mm"
        ws.cell(row=r, column=19, value=int(sh["break_min"] or 0))
        ws.cell(row=r, column=20, value=int(sh["break_after_min"] or 0))
        ws.cell(row=r, column=21, value=int(sh["ot_after_min"] or 0))
        ws.cell(row=r, column=22, value=int(sh["ot_round_min"] or 1) or 1)
        mode_pairs = settings.get("calc_mode") == "pairs"
        if d["worked_min"] and _simple_session(d) and not d["override"] and not mode_pairs                 and _one_window(d, sh, paid_break):
            ded = (f"IF({PB}=1,0,IF(ISNUMBER(W{r}),MAX(0,MIN(F{r},X{r})-MAX(E{r},W{r}))*1440,"
                   f"IF((F{r}-E{r})*1440>T{r},S{r},0)))")
            ws.cell(row=r, column=7, value=f"=IF(AND(ISNUMBER(E{r}),ISNUMBER(F{r})),MAX(0,ROUNDDOWN((F{r}-E{r})*1440+0.000001"
                                           f"-{ded},0)),0)")
        else:
            ws.cell(row=r, column=7, value=d["worked_min"])
        ws.cell(row=r, column=8, value=d["late_min"] or 0)
        ws.cell(row=r, column=9, value=d["early_min"] or 0)
        bonus = d.get("extra_credit", 0) - (d["work_credit"] if d["status"] in ("WOP", "HP") else 0)
        formula_ot = (d["status"] in ("P", "HD") or (d["status"] in ("WOP", "HP") and not ot_mode)) \
            and not d["override"] and not (double and bonus > 0)
        if formula_ot:
            ws.cell(row=r, column=10, value=f"=IF(AND(G{r}-R{r}>=U{r},G{r}-R{r}>0),FLOOR(G{r}-R{r},V{r}),0)")
        else:
            ws.cell(row=r, column=10, value=d["ot_min"])
        pen = ws.cell(row=r, column=11, value=d.get("late_penalty_min", 0))
        pen.fill = INPUT_FILL
        ws.cell(row=r, column=12, value=d["work_credit"])
        ws.cell(row=r, column=13, value=d.get("extra_credit", 0))
        ws.cell(row=r, column=14, value=d["off_credit"] if d["off_kind"] == "wo" else 0)
        ws.cell(row=r, column=15, value=d["off_credit"] if d["off_kind"] == "holiday" else 0)
        ws.cell(row=r, column=16, value=d["off_credit"] if d["off_kind"] == "leave" else 0)
        notes = " ".join(filter(None, [d.get("holiday") or "", d.get("leave_type") or ""] + d["flags"]))
        ws.cell(row=r, column=17, value=notes)
        ws.cell(row=r, column=25, value=0 if p["salary_type"] == "hourly" else d.get("early_short_min", 0))
        for c in range(1, 26):
            ws.cell(row=r, column=c).border = BOX
    last = first + len(days) - 1
    rng = lambda col: f"{col}{first}:{col}{last}"  # noqa: E731
    tr = last + 1
    ws.cell(row=tr, column=1, value="Totals").font = BOLD
    for col in "GHIJKLMNOP":
        c = ws.cell(row=tr, column="ABCDEFGHIJKLMNOPQ".index(col) + 1, value=f"=SUM({rng(col)})")
        c.font, c.fill = BOLD, SUB_FILL

    # ---- summary + salary, each a formula
    r0 = tr + 2
    cells = {}
    plan = [
        ("Present (P)", f'=COUNTIF({rng("D")},"P")', None),
        ("Half days (HD)", f'=COUNTIF({rng("D")},"HD")', None),
        ("Absent (A)", f'=COUNTIF({rng("D")},"A")', None),
        ("Missing punch (MP)", f'=COUNTIF({rng("D")},"MP")', None),
        ("Weekly off (WO)", f'=COUNTIF({rng("D")},"WO")', None),
        ("Worked on weekly off / holiday", f'=COUNTIF({rng("D")},"WOP")+COUNTIF({rng("D")},"HP")', None),
        ("Holidays (H)", f'=COUNTIF({rng("D")},"H")', None),
        ("Leave (L / LWP)", f'=COUNTIF({rng("D")},"L")+COUNTIF({rng("D")},"LWP")', None),
        ("Late marks", f'=COUNTIF({rng("H")},">0")', "late"),
        ("Late-mark deduction (days)", "=IF({late_half}>0,INT({late}/{late_half})*0.5,0)", "late_ded"),
        ("Worked credit (days)", f"=SUM({rng('L')})", "work"),
        ("Extra credit (days)", f"=SUM({rng('M')})", "extra"),
        ("Paid off-days (WO×paid + holiday + leave)", f"=SUM({rng('N')})*{WOP}+SUM({rng('O')})+SUM({rng('P')})", "paidoff"),
        ("PAYABLE DAYS", "=MAX(0,{work}+{extra}+{paidoff}-{late_ded})", "payable"),
        ("Worked hours", f"=SUM({rng('G')})/60", "worked_h"),
        ("OT earned (min)", f"=SUM({rng('J')})", "ot_gross"),
        ("Late penalty (min)", f"=SUM({rng('K')})", "lp"),
        ("Early-leave short (min)", f"=SUM({rng('Y')})", "short"),
        ("OT hours (net, never below 0)", "=MAX(0,{ot_gross}-{lp}-{short})/60", "ot_h"),
        ("Salary type", f"={T}", "type"),
        ("Rate", f"={BA}", "rate"),
        ("Per-day rate", f'=IF({T}="monthly",{BA}/{BR},IF({T}="daily",{BA},{BA}*{STD}))', "per_day"),
        ("Hourly rate", f'=IF({T}="hourly",{BA},{{per_day}}/{STD})', "hour_rate"),
        ("Basic earned", f'=IF({T}="hourly",{BA}*(MAX(0,(SUM({rng("G")})-SUM({rng("J")}))/60)+{{paidoff}}*{STD}),{{per_day}}*{{payable}})', "basic"),
        ("OT pay", f"=IF({OTE}=1,{{ot_h}}*{{hour_rate}}*{OTM},0)", "ot_pay"),
        ("Allowance", p["allowance"], "allow"),
        ("Bonus / incentive", p["bonus"], "bonus"),
        ("GROSS", "={basic}+{ot_pay}+{allow}+{bonus}", "gross"),
        ("PF", f"=IF({PFE}=1,MIN({{basic}},{R['pf_ceil']})*{R['pf_rate']}/100,0)", "pf"),
        ("ESI", f"=IF(AND({ESE}=1,{{gross}}<={R['esi_ceil']}),{{gross}}*{R['esi_rate']}/100,0)", "esi"),
        ("Advance recovery", p["advance"], "adv"),
        ("TDS", p["tds"], "tds"),
        ("Other deduction", p["other_deduction"], "oth"),
        ("TOTAL DEDUCTIONS", "={pf}+{esi}+{adv}+{tds}+{oth}", "ded"),
        ("NET PAY", "=ROUND({gross}-{ded},0)", "net"),
    ]
    ws.cell(row=r0 - 1, column=1, value="Summary & salary calculation (formulas)").font = Font(bold=True, size=12)
    for i, (label, _, key) in enumerate(plan):
        if key:
            cells[key] = f"B{r0 + i}"
    cells["late_half"] = R["late_half"]
    for i, (label, formula, key) in enumerate(plan):
        r = r0 + i
        ws.cell(row=r, column=1, value=label)
        val = formula.format(**cells) if isinstance(formula, str) else formula
        c = ws.cell(row=r, column=2, value=val)
        c.border = BOX
        if isinstance(formula, (int, float)):
            c.fill = INPUT_FILL
        if label.isupper():
            ws.cell(row=r, column=1).font = BOLD
            c.font = BOLD
        if key in ("per_day", "hour_rate", "basic", "ot_pay", "allow", "bonus", "gross", "pf", "esi", "adv", "tds", "oth", "ded", "net"):
            c.number_format = "#,##0.00"
    widths = {"A": 36, "B": 14, "C": 9, "D": 7, "E": 14, "F": 14, "Q": 30}
    for i in range(1, 26):
        L = get_column_letter(i)
        ws.column_dimensions[L].width = widths.get(L, 9)
    ws.freeze_panes = f"E{first}"
    return title, cells
