"""Attendance-only PDF (no salary): one page per employee with every day, or a muster sheet for many employees."""
import datetime as dt

from . import engine
from .pdf import A4, A4_LANDSCAPE, PDF, fit, text_width

INK, MUTED, LINE, HEAD_BG, BRAND = "#1f2a37", "#6b7785", "#d5dbe3", "#eef2f6", "#1f4e79"
STATUS_BG = {"P": "#e3f4e8", "WOP": "#d9eefc", "HP": "#d9eefc", "HD": "#fff4cc", "MP": "#ffe8c2", "A": "#fde1de",
             "LWP": "#fde1de", "WO": "#eceff3", "H": "#fbe7d6", "L": "#e8e2f7", "-": "#ffffff"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def hm(minutes):
    return engine.fmt_min(int(minutes or 0)) if minutes else ""


def days_fmt(v):
    return f"{float(v or 0):g}"


def _clock(ts, day):
    if not ts:
        return ""
    s = ts.strftime("%H:%M")
    return s + (" +1" if ts.date() > day else "")


def _remarks(d):
    out = []
    if d.get("holiday"):
        h = d["holiday"]
        out.append((h.get("name") if isinstance(h, dict) else str(h)) or "Holiday")
    if d.get("leave_type"):
        out.append(f"Leave {d['leave_type']}")
    if d.get("mp_on_off"):
        out.append("one punch only")
    elif d["status"] == "MP":
        out.append("missing punch")
    if d.get("early_min"):
        out.append(f"left {hm(d['early_min'])} early")
    if d.get("gate_personal_min") or d.get("gate_official_min"):
        out.append("gate pass")
    if d.get("override"):
        out.append("set by HR")
    if d.get("roster"):
        out.append("roster")
    return ", ".join(out)


def _footer(pdf, company, printed, page_no, total):
    pdf.line(28, pdf.h - 30, pdf.w - 28, pdf.h - 30, LINE)
    pdf.text(28, pdf.h - 18, f"{company} - attendance - printed {printed}", 7, color=MUTED)
    pdf.text(pdf.w - 128, pdf.h - 18, f"Page {page_no} of {total}", 7, color=MUTED, align="right", width=100)


def _employee_page(pdf, r, month_title, company, shift_names):
    e, s, days = r["emp"], r["summary"], r["days"]
    pdf.add_page(A4)
    x0, x1 = 28, pdf.w - 28
    pdf.rect(0, 0, pdf.w, 6, fill=BRAND)
    pdf.text(x0, 30, company, 13, bold=True, color=BRAND)
    pdf.text(x1 - 260, 30, f"Attendance - {month_title}", 11, bold=True, align="right", width=260)
    # employee block
    y = 44
    pdf.rect(x0, y, x1 - x0, 40, fill="#f7f9fb", stroke=LINE)
    pdf.text(x0 + 10, y + 16, e["name"], 12, bold=True, width=230)
    pdf.text(x0 + 10, y + 31, f"ID {e['device_user_id']}" + (f"  -  Code {e['emp_code']}" if e.get("emp_code") else ""), 8, color=MUTED)
    info = [("Department", e.get("department") or "-"), ("Designation", e.get("designation") or "-"),
            ("Shift", shift_names), ("Weekly off", ", ".join(WEEKDAYS[int(w)] for w in (e.get("weekly_off") or "").split(",") if w.strip()) or "-")]
    cx = x0 + 250
    for i, (k, v) in enumerate(info):
        xx = cx + (i % 2) * 145
        yy = y + 15 + (i // 2) * 15
        pdf.text(xx, yy, k, 7, color=MUTED)
        pdf.text(xx + 50, yy, v, 8, width=92)

    # day table
    cols = [("Date", 46, "left"), ("Day", 26, "left"), ("Shift", 44, "left"), ("In", 44, "center"), ("Out", 44, "center"),
            ("Hours", 38, "right"), ("OT", 34, "right"), ("Late", 32, "right"), ("Status", 100, "left"), ("Remarks", 0, "left")]
    used = sum(c[1] for c in cols)
    cols[-1] = ("Remarks", (x1 - x0) - used, "left")
    y = 96
    rh = 17.2
    pdf.rect(x0, y, x1 - x0, rh, fill=HEAD_BG)
    xx = x0
    for name, w, al in cols:
        pdf.text(xx + 3, y + rh - 6, name, 7.5, bold=True, color="#3b4652", align=al, width=w - 6)
        xx += w
    y += rh
    for d in days:
        st = d["status"]
        if d["date"].weekday() == 6:
            pdf.rect(x0, y, x1 - x0, rh, fill="#fafbfc")
        vals = [d["date"].strftime("%d %b"), WEEKDAYS[d["date"].weekday()], d["shift"]["name"] if d.get("shift") else "",
                _clock(d.get("in"), d["date"]), _clock(d.get("out"), d["date"]), hm(d.get("worked_min")), hm(d.get("ot_min")),
                hm(d.get("late_min")), None, _remarks(d)]
        xx = x0
        for (name, w, al), v in zip(cols, vals):
            if name == "Status":
                label = engine.STATUS_LABELS.get(st, st) + (" + MP" if d.get("mp_on_off") else "")
                pdf.rect(xx + 2, y + 3, w - 4, rh - 6, fill=STATUS_BG.get(st, "#ffffff"))
                pdf.text(xx + 5, y + rh - 6.2, label, 7.5, bold=st in ("A", "MP", "LWP"), width=w - 8)
            else:
                pdf.text(xx + 3, y + rh - 6.2, v, 8 if name != "Remarks" else 7, align=al, width=w - 6,
                         color=MUTED if name in ("Day", "Remarks") else INK)
            xx += w
        pdf.line(x0, y + rh, x1, y + rh, "#e6eaef")
        y += rh

    # summary
    y += 12
    box = [("Present", days_fmt(s.get("cnt_P"))), ("Half days", days_fmt(sum(1 for d in days if d["status"] == "HD"))),
           ("Absent", days_fmt(s.get("cnt_A"))), ("Missing punch", days_fmt(s.get("cnt_MP"))),
           ("Weekly offs", days_fmt(s.get("cnt_WO"))), ("Holidays", days_fmt(s.get("cnt_H"))),
           ("Leaves", days_fmt(sum(1 for d in days if d["status"] in ("L", "LWP")))),
           ("Worked on off days", days_fmt(sum(1 for d in days if d["status"] in ("WOP", "HP")))),
           ("Late marks", days_fmt(s.get("late_count"))), ("Hours worked", hm(s.get("worked_min")) or "0:00"),
           ("Overtime", hm(s.get("ot_min")) or "0:00"), ("Payable days", days_fmt(s.get("payable_days")))]
    bw = (x1 - x0) / 6
    for i, (k, v) in enumerate(box):
        bx, by = x0 + (i % 6) * bw, y + (i // 6) * 34
        strong = k in ("Payable days", "Overtime")
        pdf.rect(bx + 1, by, bw - 2, 30, fill="#eef5fb" if strong else "#f7f9fb", stroke=LINE)
        pdf.text(bx + 7, by + 11, k, 6.8, color=MUTED, width=bw - 12)
        pdf.text(bx + 7, by + 25, v, 11, bold=True, color=BRAND if strong else INK, width=bw - 12)
    y += 74
    if s.get("edited"):
        pdf.text(x0, y, "Final totals edited by HR: payable days / overtime above are the edited values.", 7, color=MUTED)
        y += 10
    # signatures
    sy = min(max(y + 30, pdf.h - 66), pdf.h - 46)
    for i, lab in enumerate(("Employee signature", "Checked by", "Authorised signatory")):
        sx = x0 + i * (x1 - x0) / 3
        pdf.line(sx + 10, sy, sx + (x1 - x0) / 3 - 20, sy, "#9aa5b1")
        pdf.text(sx + 10, sy + 10, lab, 7.5, color=MUTED)


def _muster_pages(pdf, rows, month_days, month_title, company, holidays):
    x0 = 22
    name_w, tot_cols = 120, [("P", 24), ("HD", 22), ("A", 22), ("WO", 24), ("H", 20), ("L", 20), ("Pay", 30), ("OT", 34)]
    page_w = A4_LANDSCAPE[0]
    day_w = (page_w - 2 * x0 - name_w - sum(w for _, w in tot_cols)) / len(month_days)
    rh, top = 15.5, 70
    per_page = int((A4_LANDSCAPE[1] - top - 70) // rh)
    chunks = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    for ci, chunk in enumerate(chunks):
        pdf.add_page(A4_LANDSCAPE)
        pdf.rect(0, 0, pdf.w, 6, fill=BRAND)
        pdf.text(x0, 28, company, 13, bold=True, color=BRAND)
        pdf.text(pdf.w - x0 - 300, 28, f"Attendance muster - {month_title}", 11, bold=True, align="right", width=300)
        pdf.text(x0, 42, f"{len(rows)} employee(s)" + (f" - sheet {ci + 1} of {len(chunks)}" if len(chunks) > 1 else ""), 7.5, color=MUTED)
        y = top - 22
        pdf.rect(x0, y, pdf.w - 2 * x0, 22, fill=HEAD_BG)
        pdf.text(x0 + 3, y + 14, "Employee", 7.5, bold=True, color="#3b4652")
        xx = x0 + name_w
        for d in month_days:
            if d.weekday() == 6 or d.isoformat() in holidays:
                pdf.rect(xx, y, day_w, 22 + rh * len(chunk), fill="#f3f5f8" if d.weekday() == 6 else "#fdf1e7")
            pdf.text(xx, y + 9, str(d.day), 6.8, bold=True, align="center", width=day_w)
            pdf.text(xx, y + 18, WEEKDAYS[d.weekday()][:2], 5.5, color=MUTED, align="center", width=day_w)
            xx += day_w
        for k, w in tot_cols:
            pdf.text(xx, y + 14, k, 7, bold=True, align="center", width=w)
            xx += w
        y += 22
        for r in chunk:
            s, by_date = r["summary"], {d["date"]: d for d in r["days"]}
            pdf.text(x0 + 3, y + rh - 4.8, f"{r['emp']['name']}", 7.5, width=name_w - 30)
            pdf.text(x0 + name_w - 28, y + rh - 4.8, str(r["emp"]["device_user_id"]), 6.5, color=MUTED, align="right", width=24)
            xx = x0 + name_w
            for day in month_days:
                d = by_date.get(day)
                st = d["status"] if d else ""
                if st and st not in ("-",):
                    pdf.rect(xx + 0.6, y + 2, day_w - 1.2, rh - 4, fill=STATUS_BG.get(st, "#ffffff"))
                    pdf.text(xx, y + rh - 5, st, 5.6 if len(st) > 2 else 6.3, bold=st in ("A", "MP"), align="center", width=day_w)
                xx += day_w
            hd = sum(1 for d in r["days"] if d["status"] == "HD")
            lv = sum(1 for d in r["days"] if d["status"] in ("L", "LWP"))
            for (k, w), v in zip(tot_cols, [s.get("cnt_P"), hd, s.get("cnt_A"), s.get("cnt_WO"), s.get("cnt_H"), lv,
                                             s.get("payable_days"), None]):
                txt = hm(s.get("ot_min")) if k == "OT" else days_fmt(v)
                pdf.text(xx, y + rh - 4.8, txt, 7, bold=k == "Pay", align="center", width=w)
                xx += w
            pdf.line(x0, y + rh, pdf.w - x0, y + rh, "#e6eaef")
            y += rh
        # legend
        y += 10
        lx = x0
        for st in ("P", "HD", "A", "MP", "WO", "WOP", "H", "HP", "L", "LWP"):
            label = f"{st} {engine.STATUS_LABELS.get(st, st)}"
            pdf.rect(lx, y - 7, 14, 9, fill=STATUS_BG.get(st, "#fff"))
            pdf.text(lx + 17, y, label, 6.5, color=MUTED)
            lx += 22 + text_width(label, 6.5)
        pdf.text(x0, y + 12, "Pay = payable days (present + paid offs, after half days and edits); OT in hours:minutes.", 6.5, color=MUTED)


def build(rows, month, company, layout="employee", holidays=None):
    """rows: [{'emp', 'summary', 'days', 'shift'}] from payroll.compute_month. Returns PDF bytes (attendance only)."""
    start, end = engine.month_range(month)
    month_title = start.strftime("%B %Y")
    printed = dt.datetime.now().strftime("%d %b %Y %H:%M")
    pdf = PDF(title=f"Attendance {month_title}", author=company)
    if layout == "muster":
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        _muster_pages(pdf, rows, days, month_title, company, holidays or {})
    else:
        for r in rows:
            names = []
            for d in r["days"]:
                n = d["shift"]["name"] if d.get("shift") else ""
                if n and n not in names:
                    names.append(n)
            _employee_page(pdf, r, month_title, company, " / ".join(names) or "-")
    total = len(pdf.pages)
    for i, (size, ops) in enumerate(pdf.pages, 1):
        pdf.size, pdf.ops = size, ops
        _footer(pdf, company, printed, i, total)
    return pdf.output()
