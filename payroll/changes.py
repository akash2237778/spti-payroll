"""Everything people changed on top of the attendance machine's data, with the machine's own view
of each affected day for comparison."""
import datetime as dt
from collections import defaultdict

from . import engine

KINDS = {
    "punch_added": "Punch added",
    "punch_marked": "Punch marked IN/OUT",
    "punch_ignored": "Punch ignored",
    "status": "Day status forced",
    "leave": "Leave",
    "penalty": "Late penalty",
    "roster": "Shift changed",
    "holiday": "Holiday",
    "month_total": "Final totals edited",
}
TS = "%Y-%m-%d %H:%M:%S"


def _shift_index(conn):
    shifts = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM shifts")}
    return shifts, next(iter(shifts.values()))


def collect(conn, start, end, emp_id=None):
    """List of change dicts between start and end (by work date), newest change first."""
    shifts, default_shift = _shift_index(conn)
    emps = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM employees")}
    by_uid = {e["device_user_id"]: e for e in emps.values()}
    s, e = start.isoformat(), end.isoformat()
    out = []

    def add(kind, emp, work_date, machine, changed, reason, by, at, undo, ref=None):
        if emp_id and (not emp or emp["id"] != emp_id):
            return
        out.append({"kind": kind, "label": KINDS[kind], "emp": emp, "date": work_date, "machine": machine,
                    "changed": changed, "reason": reason or "", "by": by or "", "at": (at or "")[:16],
                    "undo": undo, "ref": ref})

    # punches: anything a person added or re-classified (margins so night/early punches are not missed)
    lo = (dt.datetime.combine(start, dt.time()) - dt.timedelta(days=1)).strftime(TS)
    hi = (dt.datetime.combine(end + dt.timedelta(days=1), dt.time(12))).strftime(TS)
    for p in conn.execute("""SELECT * FROM punches WHERE ts BETWEEN ? AND ?
                             AND (source = 'manual' OR role IS NOT NULL OR deleted = 1)""", (lo, hi)):
        emp = by_uid.get(p["device_user_id"])
        if not emp:
            continue
        ts = dt.datetime.strptime(p["ts"], TS)
        wd = engine.work_date_of(ts, shifts.get(emp["shift_id"], default_shift))
        if not (start <= wd <= end):
            continue
        when = ts.strftime("%d %b %H:%M")
        reason = (p["note"] or "").split(": ", 1)[-1] if p["note"] else ""
        if p["source"] == "manual":
            role = (p["role"] or "auto").upper()
            add("punch_added", emp, wd, "no punch", f"+{role} {when}", reason, p["changed_by"], p["changed_at"],
                {"kind": "punch_added", "id": p["id"]}, p["id"])
        elif p["role"] == "ignore" or p["deleted"]:
            add("punch_ignored", emp, wd, f"punch {when}", "ignored", reason, p["changed_by"], p["changed_at"],
                {"kind": "punch_role", "id": p["id"]}, p["id"])
        elif p["role"] in ("in", "out"):
            add("punch_marked", emp, wd, f"punch {when} (auto)", f"{p['role'].upper()} {when}", reason, p["changed_by"],
                p["changed_at"], {"kind": "punch_role", "id": p["id"]}, p["id"])

    for o in conn.execute("SELECT * FROM day_overrides WHERE work_date BETWEEN ? AND ?", (s, e)):
        changed = f"status {o['status']}" if o["status"] else ""
        if o["in_time"]:
            changed = (changed + " " if changed else "") + f"times {o['in_time']}-{o['out_time']}"
        add("status", emps.get(o["employee_id"]), dt.date.fromisoformat(o["work_date"]), "from punches", changed,
            o["reason"], o["created_by"], o["created_at"], {"kind": "status", "id": o["id"]})

    for lv in conn.execute("SELECT * FROM leaves WHERE leave_date BETWEEN ? AND ?", (s, e)):
        changed = f"{lv['leave_type']} {'paid' if lv['paid'] else 'unpaid'}{' ½ day' if lv['half_day'] else ''}"
        add("leave", emps.get(lv["employee_id"]), dt.date.fromisoformat(lv["leave_date"]), "", changed, lv["reason"],
            lv["created_by"], lv["created_at"], {"kind": "leave", "id": lv["id"]})

    for lp in conn.execute("SELECT * FROM late_penalties WHERE work_date BETWEEN ? AND ?", (s, e)):
        add("penalty", emps.get(lp["employee_id"]), dt.date.fromisoformat(lp["work_date"]), "full OT",
            f"−{lp['minutes']} min OT", lp["note"], lp["created_by"], lp["created_at"], {"kind": "penalty", "id": lp["id"]})

    for a in conn.execute("""SELECT a.*, s.name AS shift_name FROM employee_shifts a JOIN shifts s ON s.id = a.shift_id
                             WHERE a.from_date <= ? AND (a.to_date IS NULL OR a.to_date = '' OR a.to_date >= ?)""", (e, s)):
        emp = emps.get(a["employee_id"])
        base = shifts.get(emp["shift_id"], default_shift)["name"] if emp else ""
        span = f"{a['from_date']} → {a['to_date'] or 'onwards'}"
        add("roster", emp, dt.date.fromisoformat(max(a["from_date"], s)), f"{base} shift",
            f"{a['shift_name']} shift ({span})", a["reason"], a["created_by"], a["created_at"], {"kind": "roster", "id": a["id"]})

    for m in conn.execute("SELECT * FROM month_overrides WHERE month BETWEEN ? AND ?", (s[:7], e[:7])):
        was, now = [], []
        if m["payable_days"] is not None:
            was.append(f"{m['calc_payable_days']:g} payable days" if m["calc_payable_days"] is not None else "calculated days")
            now.append(f"{m['payable_days']:g} payable days")
        if m["ot_min"] is not None:
            was.append(f"OT {engine.fmt_min(m['calc_ot_min'])}" if m["calc_ot_min"] is not None else "calculated OT")
            now.append(f"OT {engine.fmt_min(m['ot_min'])}")
        add("month_total", emps.get(m["employee_id"]), max(dt.date.fromisoformat(m["month"] + "-01"), start),
            ", ".join(was), ", ".join(now), m["reason"], m["created_by"], m["created_at"], {"kind": "month_total", "id": m["id"]})

    if not emp_id:
        for h in conn.execute("SELECT * FROM holidays WHERE hdate BETWEEN ? AND ?", (s, e)):
            add("holiday", None, dt.date.fromisoformat(h["hdate"]), "working day",
                f"{'paid' if h['paid'] else 'unpaid'} holiday: {h['name']}", "", "", "", {"kind": "holiday", "id": h["id"]})

    out.sort(key=lambda c: (c["at"] or "", c["date"].isoformat()), reverse=True)
    return out


def edited_days(changes):
    """{(employee_id, date)} that differ from the machine because of a person; None employee = everyone (holiday)."""
    days = defaultdict(list)
    for c in changes:
        days[(c["emp"]["id"] if c["emp"] else None, c["date"])].append(c["kind"])
    return days


def day_text(d):
    if not d or d["status"] == "-":
        return "-"
    t = d["status"]
    if d["in"]:
        t += f" {d['in']:%H:%M}–{d['out']:%H:%M}" if d["out"] else f" {d['in']:%H:%M}–?"
    if d["worked_min"]:
        t += f" ({engine.fmt_min(d['worked_min'])})"
    if d.get("ot_min"):
        t += f" OT {engine.fmt_min(d['ot_min'])}"
    return t


def compare(conn, changes, start, end, settings):
    """For every edited employee-day: what the machine alone says vs what is used now."""
    keys = {(c["emp"]["id"], c["date"]) for c in changes if c["emp"] and c["kind"] not in ("roster", "month_total")}
    roster = {c["emp"]["id"] for c in changes if c["emp"] and c["kind"] == "roster"}
    ids = {k[0] for k in keys} | roster
    if not ids:
        return {}
    emps = [dict(r) for r in conn.execute(f"SELECT * FROM employees WHERE id IN ({','.join('?' * len(ids))})", list(ids))]
    now = engine.employee_days(conn, emps, start, end, settings)
    raw = engine.employee_days(conn, emps, start, end, settings, machine_only=True)
    res = {}
    for eid in ids:
        for dn, dr in zip(now[eid]["days"], raw[eid]["days"]):
            k = (eid, dn["date"])
            # roster changes affect every day in range: only list days where the result actually differs
            if k in keys or (eid in roster and day_text(dn) != day_text(dr)):
                res[k] = {"machine": day_text(dr), "now": day_text(dn), "differs": day_text(dn) != day_text(dr)}
    return res
