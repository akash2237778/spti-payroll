"""Attendance engine: turns raw punches into work sessions, per-day attendance and monthly summaries.

Punches are NOT split by calendar day. Each employee's punches form one continuous stream which is
paired into work sessions (in -> out) by choosing the globally most plausible pairing (dynamic
programming). A session belongs to the work day on which it started, so a 24-hour duty from
08:56 to 08:30 next morning is one session on the first day.

Because pairing is global, a single correction (add a missing punch, mark a punch IN / OUT /
ignore) automatically re-pairs every following punch - nothing else has to be fixed by hand.

The device's own in/out state flag is ignored: on this machine it is unreliable.
"""
import calendar
import math
import datetime as dt
import statistics
from collections import defaultdict

STATUS_LABELS = {
    "P": "Present", "HD": "Half Day", "A": "Absent", "MP": "Missing Punch",
    "WO": "Weekly Off", "WOP": "Worked on Weekly Off", "H": "Holiday", "HP": "Worked on Holiday",
    "L": "Paid Leave", "LWP": "Leave Without Pay", "-": "Not applicable",
}
OVERRIDE_STATUSES = ["P", "HD", "A", "WO", "H", "L", "LWP"]

# Pairing cost model (units ~ "hours of implausibility")
ORPHAN_COST = 6.0          # leaving a punch unpaired (= a missing punch)
IN_DEV_WEIGHT = 0.5        # per hour the IN differs from the usual start time
OUT_DEV_WEIGHT = 0.25      # per hour the OUT differs from the usual end time
EXCESS_WEIGHT = 0.15       # per hour the session is longer than the standard shift
ABSORB_COST = 0.3          # per inside punch that forms a short break pair (lunch out -> lunch in)
STRAY_COST = 3.0           # per inside punch that does not look like a break pair
BREAK_PAIR_MAX_H = 3.0     # a lunch/tea break is at most this long
MULTI_GAP_MAX_H = 10.0     # employees allowed several in/out a day: a gap between pairs may be this long
SHORT_SESSION_COST = 3.0   # sessions under an hour are unlikely to be real
MAX_INSIDE = 8             # max punches inside one session
STREAM_MARGIN_DAYS = 3     # extra days loaded each side so cross-day sessions pair correctly


def parse_hm(s):
    h, m = str(s).split(":")[:2]
    return dt.time(int(h), int(m))


def shift_bounds(work_date, shift):
    start = dt.datetime.combine(work_date, parse_hm(shift["start_time"]))
    end = dt.datetime.combine(work_date, parse_hm(shift["end_time"]))
    if end <= start:
        end += dt.timedelta(days=1)  # night shift
    return start, end


def standard_minutes(shift):
    s, e = shift_bounds(dt.date(2000, 1, 3), shift)
    return int((e - s).total_seconds() // 60) - int(shift["break_min"] or 0)


def work_date_of(ts, shift):
    """Punches before day_start_hour are attributed to the previous work day."""
    return (ts - dt.timedelta(hours=int(shift["day_start_hour"] or 0))).date()


def _tod(t):
    return t.hour + t.minute / 60.0


def _circ(a, b):
    d = abs(a - b) % 24
    return min(d, 24 - d)


def dedup(times, seconds):
    out = []
    for t in sorted(times):
        if out and (t - out[-1]).total_seconds() <= seconds:
            continue
        out.append(t)
    return out


def dedup_punches(punches, seconds):
    """punches: list of dicts {ts, role, id}. Repeat taps are dropped; a forced role survives the merge."""
    out = []
    for p in sorted(punches, key=lambda p: p["ts"]):
        if out and (p["ts"] - out[-1]["ts"]).total_seconds() <= seconds:
            if p.get("role") and not out[-1].get("role"):
                out[-1] = dict(out[-1], role=p["role"], id=p.get("id"))
            continue
        out.append(p)
    return out


# ------------------------------------------------------------------------------------------------
# Session pairing
# ------------------------------------------------------------------------------------------------

def pair_sessions(punches, shift, settings, typical=None, expect=None):
    """Pair a chronologically sorted punch stream into sessions.

    punches: list of datetimes or dicts {ts, role ('in'|'out'|None), id}.
    typical: optional (usual_in_hour, usual_out_hour) for this employee.
    expect: optional fn(in_ts) -> (usual_in_hour, usual_out_hour, standard_hours); lets the expected
            times follow a shift roster that changes during the month (day / night rotation).
    Returns list of {start, end, punches, ids, orphan}.
    """
    ps = [p if isinstance(p, dict) else {"ts": p, "role": None, "id": None} for p in punches]
    n = len(ps)
    if n == 0:
        return []
    if expect is None:
        s_start, s_end = shift_bounds(dt.date(2000, 1, 3), shift)
        fixed = (typical or (_tod(s_start), _tod(s_end))) + (standard_minutes(shift) / 60.0,)

        def expect(ts):
            return fixed
    max_h = float(settings.get("max_duty_hours") or 26)

    gap_max = float(settings.get("break_pair_max_h") or BREAK_PAIR_MAX_H)

    def inside_cost(i, j):
        inside = [p["ts"] for p in ps[i + 1:j]]
        if not inside:
            return 0.0
        if len(inside) % 2 == 0 and all((inside[k + 1] - inside[k]).total_seconds() / 3600 <= gap_max
                                        for k in range(0, len(inside), 2)):
            return ABSORB_COST * len(inside)
        return STRAY_COST * len(inside)

    def cost(i, j):
        a, b = ps[i]["ts"], ps[j]["ts"]
        dur = (b - a).total_seconds() / 3600
        t_in, t_out, std_h = expect(a)
        c = IN_DEV_WEIGHT * _circ(_tod(a), t_in) + OUT_DEV_WEIGHT * _circ(_tod(b), t_out)
        c += EXCESS_WEIGHT * max(0.0, dur - std_h) + inside_cost(i, j)
        if dur < 1:
            c += SHORT_SESSION_COST
        if ps[i]["role"] == "in" and ps[j]["role"] == "out":
            c = 0.0  # confirmed by a person
        return c

    f = [0.0] * (n + 1)
    nxt = [None] * n
    for i in range(n - 1, -1, -1):
        best, choice = ORPHAN_COST + f[i + 1], i
        if ps[i]["role"] != "out":
            for j in range(i + 1, min(n, i + MAX_INSIDE + 2)):
                if j - 1 > i and ps[j - 1]["role"] in ("in", "out"):
                    break  # a confirmed punch cannot be swallowed inside a session
                if (ps[j]["ts"] - ps[i]["ts"]).total_seconds() / 3600 > max_h:
                    break
                if ps[j]["role"] == "in":
                    continue
                c = cost(i, j) + f[j + 1]
                if c < best:
                    best, choice = c, j
        f[i], nxt[i] = best, choice

    sessions, i = [], 0
    while i < n:
        j = nxt[i]
        seg = ps[i:j + 1]
        sessions.append({"start": seg[0]["ts"], "end": seg[-1]["ts"] if j > i else None,
                         "punches": [p["ts"] for p in seg], "ids": [p["id"] for p in seg],
                         "roles": [p["role"] for p in seg], "sources": [p.get("source") for p in seg], "orphan": j == i})
        i = j + 1
    return sessions


def break_minutes(start, end, shift, paid_break=False):
    """Unpaid break to deduct from one continuous in->out span.

    With a fixed lunch window (shift.break_start, e.g. 13:00 for 30 min) only the part of the window the
    person was actually present for is deducted: 09:00-17:30 loses 30 min, 14:00-20:00 loses nothing.
    Without a window, the break is deducted once the span exceeds break_after_min.
    Employees with a paid break never lose it."""
    brk = int(shift.get("break_min") or 0)
    if paid_break or brk <= 0:
        return 0.0
    if shift.get("break_start"):
        total, d = 0.0, start.date() - dt.timedelta(days=1)
        while d <= end.date():
            ws = dt.datetime.combine(d, parse_hm(shift["break_start"]))
            we = ws + dt.timedelta(minutes=brk)
            total += max(0.0, (min(end, we) - max(start, ws)).total_seconds() / 60)
            d += dt.timedelta(days=1)
        return total
    span = (end - start).total_seconds() / 60
    return float(brk) if span > int(shift.get("break_after_min") or 0) else 0.0


def session_minutes(sess, shift, mode, paid_break=False):
    p = sess["punches"]
    if sess["orphan"] or len(p) < 2:
        return 0
    if mode == "pairs" and len(p) % 2 == 0 and len(p) > 2:
        return int(sum((p[k + 1] - p[k]).total_seconds() for k in range(0, len(p), 2)) // 60)
    span = (p[-1] - p[0]).total_seconds() / 60
    return max(0, int(span - break_minutes(p[0], p[-1], shift, paid_break)))


def worked_minutes(times, shift, mode, paid_break=False):
    """Worked minutes for a single in/out sequence (kept for callers that have plain punches)."""
    return session_minutes({"punches": list(times), "orphan": len(times) < 2}, shift, mode, paid_break)


def paid_day_minutes(shift):
    """Standard paid working minutes of a day on this shift: the shift's 'standard paid hours' when set
    (e.g. 8 h on a 21:00-09:00 night duty; the rest is overtime), else shift length minus lunch."""
    v = shift["std_min"] if "std_min" in shift.keys() else None
    return int(v) if v else standard_minutes(shift)


def ot_base_minutes(shift, paid_break=False):
    """Paid minutes in a normal day before overtime starts (lunch included when it is paid)."""
    return paid_day_minutes(shift) + (int(shift.get("break_min") or 0) if paid_break else 0)


def _override_session(work_date, ov):
    tin = dt.datetime.combine(work_date, parse_hm(ov["in_time"]))
    tout = dt.datetime.combine(work_date, parse_hm(ov["out_time"]))
    if tout <= tin:
        tout += dt.timedelta(days=1)
    return {"start": tin, "end": tout, "punches": [tin, tout], "ids": [None, None], "roles": ["in", "out"], "orphan": False}


# ------------------------------------------------------------------------------------------------
# Day evaluation
# ------------------------------------------------------------------------------------------------

def ot_grace(shift):
    return max(0, int(shift.get("ot_grace_min") if shift.get("ot_grace_min") is not None else 5))


def round_ot(minutes, shift):
    """Overtime in whole blocks (e.g. 30 min); a block still counts when at most `ot_grace_min` short.
    E.g. block 30, grace 5: 1:59 -> 2:00, 1:55 -> 2:00, 1:54 -> 1:30."""
    block = int(shift["ot_round_min"] or 1) or 1
    return int((minutes + ot_grace(shift)) // block * block)


def _clock(work_date, hm, after=None):
    t = dt.datetime.combine(work_date, parse_hm(hm))
    if after is not None and t <= after:
        t += dt.timedelta(days=1)       # night shift: time after midnight
    return t


def _overlap(a1, a2, b1, b2):
    return max(0.0, (min(a2, b2) - max(a1, b1)).total_seconds() / 60)


def apply_gate_passes(work_date, complete, gate_passes, shift, paid_break=False):
    """(personal minutes to deduct, official minutes to add) for one day.
    Personal: time out of the premises inside the punched work spans (lunch already deducted is not counted twice).
    Official: time on official duty that is NOT already inside a punched span (e.g. left on duty, no return punch)."""
    spans = [(s["start"], s["end"]) for s in complete]
    first = spans[0][0] if spans else None
    last = spans[-1][1] if spans else None
    s_start, s_end = shift_bounds(work_date, shift)
    lunch = None
    if shift.get("break_start") and not paid_break:
        ls = dt.datetime.combine(work_date, parse_hm(shift["break_start"]))
        lunch = (ls, ls + dt.timedelta(minutes=int(shift.get("break_min") or 0)))
    personal = official = 0.0
    for gp in gate_passes:
        out = _clock(work_date, gp["out_time"], after=(first - dt.timedelta(hours=1)) if first else None)
        back = _clock(work_date, gp["in_time"], after=out) if gp.get("in_time") else max(last or s_end, s_end if gp["kind"] == "official" else out)
        if back <= out:
            continue
        inside = sum(_overlap(out, back, a, b) for a, b in spans)
        if lunch:
            inside -= sum(_overlap(max(out, a), min(back, b), lunch[0], lunch[1]) for a, b in spans if min(back, b) > max(out, a))
        inside = max(0.0, inside)
        if gp["kind"] == "official":
            official += max(0.0, (back - out).total_seconds() / 60 - sum(_overlap(out, back, a, b) for a, b in spans))
        else:
            personal += inside
    return int(round(personal)), int(round(official))


def evaluate_day(work_date, punches, shift, settings, *, sessions=None, weekly_off=False, holiday=None, leave=None,
                 override=None, active=True, today=None, now=None, paid_break=False, multi=False, gate_passes=()):
    """Return a dict describing one employee-day.

    Either pass `sessions` (already paired from the whole stream) or plain `punches` for this day,
    which are then paired locally.
    """
    if now is None:
        now = dt.datetime.combine(today, dt.time(23, 59)) if today else dt.datetime.now()
    today = now.date()
    if sessions is None:
        sessions = pair_sessions(sorted(punches), shift, settings)
    rec = {"date": work_date, "status": "-", "in": None, "out": None, "punches": [],
           "sessions": sessions, "worked_min": 0, "late_min": 0, "early_min": 0, "ot_min": 0,
           "work_credit": 0.0, "extra_credit": 0.0, "off_credit": 0.0, "off_kind": None, "flags": [], "override": False,
           "leave_type": None, "holiday": holiday["name"] if holiday else None, "suggest": None,
           "early_short_min": 0}
    if not active:
        return rec
    forced_status = None
    if override:
        rec["override"] = True
        if override.get("in_time") and override.get("out_time"):
            sessions = [_override_session(work_date, override)]
            rec["sessions"] = sessions
            rec["flags"].append("manual times")
        if override.get("status"):
            forced_status = override["status"]

    complete = [s for s in sessions if not s["orphan"]]
    orphans = [s for s in sessions if s["orphan"]]
    times = sorted(t for s in sessions for t in s["punches"])
    rec["punches"] = times
    if work_date > today and not times and not forced_status and not leave:
        return rec

    mode = "pairs" if multi else settings.get("calc_mode", "first_last")
    mins = sum(session_minutes(s, shift, mode, paid_break) for s in complete)
    if multi:
        rec["pairs"] = [(s["punches"][k], s["punches"][k + 1]) for s in complete for k in range(0, len(s["punches"]) - 1, 2)
                        if len(s["punches"]) % 2 == 0]
        if any(len(s["punches"]) % 2 for s in complete):
            rec["flags"].append("odd in/out punches")
    if gate_passes and (complete or any(gp["kind"] == "official" for gp in gate_passes)):
        personal, official = apply_gate_passes(work_date, complete, gate_passes, shift, paid_break)
        mins = max(0, mins - personal) + official
        rec["gate_personal_min"], rec["gate_official_min"] = personal, official
        rec["flags"].append("gate pass")
    rec["worked_min"] = mins
    std = ot_base_minutes(shift, paid_break)
    s_start, s_end = shift_bounds(work_date, shift)
    if complete:
        rec["in"], rec["out"] = complete[0]["start"], complete[-1]["end"]
        late = (complete[0]["start"] - s_start).total_seconds() / 60
        if late > int(shift["grace_in_min"] or 0):
            rec["late_min"] = int(late)
        early = (s_end - complete[-1]["end"]).total_seconds() / 60
        if early > int(shift["grace_out_min"] or 0):
            rec["early_min"] = int(early)
    elif orphans:
        rec["in"] = orphans[0]["start"]
    if orphans and complete:
        rec["flags"].append("unpaired punch")

    long_h = float(settings.get("long_duty_flag_hours") or 16)
    is_long = any((s["end"] - s["start"]).total_seconds() / 3600 >= long_h for s in complete)
    if is_long:
        confirmed = all(s["roles"][0] == "in" and s["roles"][-1] == "out" for s in complete
                        if (s["end"] - s["start"]).total_seconds() / 3600 >= long_h)
        rec["flags"].append("long duty" + (" (confirmed)" if confirmed else ""))
    # A long duty can be paid as two days instead of one day + overtime.
    double = settings.get("long_duty_mode") == "double" and mins >= std + int(shift["full_day_min"])

    if mins >= int(shift["full_day_min"]):
        work_credit = 1.0
    elif mins >= int(shift["half_day_min"]):
        work_credit = 0.5
    else:
        work_credit = 0.0
    bonus_credit = 1.0 if double else 0.0
    ot_base = 2 * std if double else std
    is_off_day = weekly_off or (holiday is not None)

    def set_ot(base_minutes):
        extra = mins - base_minutes
        if extra > 0 and extra + ot_grace(shift) >= int(shift["ot_after_min"] or 0):
            rec["ot_min"] = round_ot(extra, shift)

    # --- Manual status override wins -------------------------------------------------------
    if forced_status:
        rec["status"] = forced_status
        if forced_status == "P":
            rec["work_credit"] = 1.0
        elif forced_status == "HD":
            rec["work_credit"] = 0.5
        elif forced_status in ("WO", "H"):
            rec["off_credit"], rec["off_kind"] = 1.0, ("wo" if forced_status == "WO" else "holiday")
        elif forced_status == "L":
            rec["off_credit"], rec["off_kind"], rec["leave_type"] = 1.0, "leave", "Manual"
        if forced_status in ("P", "HD") and mins:
            set_ot(std)
        return rec

    # --- Leave --------------------------------------------------------------------------------
    if leave and not times:
        rec["leave_type"] = leave["leave_type"]
        portion = 0.5 if leave["half_day"] else 1.0
        if leave["paid"]:
            rec["status"], rec["off_credit"], rec["off_kind"] = "L", portion, "leave"
        else:
            rec["status"] = "LWP"
        return rec

    # --- No punches ---------------------------------------------------------------------------
    if not times:
        if holiday is not None:
            rec["status"], rec["off_kind"] = "H", "holiday"
            rec["off_credit"] = 1.0 if holiday["paid"] else 0.0
        elif weekly_off:
            rec["status"], rec["off_credit"], rec["off_kind"] = "WO", 1.0, "wo"
        elif work_date == today:
            rec["status"] = "-"
        else:
            rec["status"] = "A"
        return rec

    # --- Only unpaired punches ----------------------------------------------------------------
    if not complete:
        rec["flags"].append("missing punch")
        if (now - orphans[-1]["start"]).total_seconds() / 3600 < long_h:
            rec["status"] = "P" if not is_off_day else ("WOP" if holiday is None else "HP")
            rec["flags"].append("in progress")
            return rec
        policy = settings.get("missing_punch_policy", "half")
        credit = {"absent": 0.0, "half": 0.5, "full": 1.0}.get(policy, 0.5)
        rec["status"] = "MP"
        if is_off_day:
            # The day is paid as the weekly off / holiday; the lone punch adds nothing until the other punch is fixed.
            rec["status"] = "H" if holiday is not None else "WO"
            rec["mp_on_off"] = True
            rec["off_kind"] = "holiday" if holiday is not None else "wo"
            rec["off_credit"] = 1.0 if (holiday is None or holiday["paid"]) else 0.0
            # Paid as an extra day only in "extra_day" mode; in "ot" mode the hours become OT once the punch is fixed.
            rec["extra_credit"] = credit if settings.get("wo_work_mode") != "ot" else 0.0
        else:
            rec["work_credit"] = credit
        return rec

    # --- Normal worked day --------------------------------------------------------------------
    if leave and leave["half_day"] and leave["paid"] and work_credit == 0.5:
        rec["off_credit"], rec["off_kind"], rec["leave_type"] = 0.5, "leave", leave["leave_type"]

    if is_off_day:
        rec["status"] = "HP" if holiday is not None else "WOP"
        rec["off_kind"] = "holiday" if holiday is not None else "wo"
        rec["off_credit"] = 1.0 if (holiday is None or holiday["paid"]) else 0.0
        rec["extra_credit"] = work_credit + bonus_credit
        if settings.get("wo_work_mode") == "ot":
            rec["extra_credit"] = 0.0
            rec["ot_min"] = round_ot(mins, shift)
        else:
            set_ot(ot_base)
            rec["ot_base_min"] = ot_base
        rec["late_min"] = rec["early_min"] = 0
        return rec

    # Working-hours rule (company rule), applies whenever the shift is not fully worked:
    #   full-day hours (e.g. 6 h) up to the standard day (e.g. 8 h) -> full day; the minutes short of the standard
    #   day are taken off the month's overtime (see summarize).
    #   half-day hours (e.g. 4 h) up to full-day hours -> half day; time worked beyond the half day is overtime.
    if settings.get("short_day_rule", "1") == "1" and complete and not double:
        if int(shift["full_day_min"]) <= mins < std:
            rec["early_short_min"] = std - mins
            rec["flags"].append(f"short day: full day, {fmt_min(std - mins)} from OT")
        elif work_credit == 0.5:
            ot_base = int(shift["half_day_min"])

    rec["work_credit"] = work_credit
    rec["extra_credit"] = bonus_credit
    rec["status"] = "P" if work_credit == 1.0 else ("HD" if work_credit == 0.5 else "A")
    if work_credit == 0.0:
        rec["flags"].append("too short")
    set_ot(ot_base)
    rec["ot_base_min"] = ot_base
    if work_credit == 0.5 and ot_base == int(shift["half_day_min"]) and rec["ot_min"]:
        rec["flags"].append(f"half day: {fmt_min(rec['ot_min'])} as OT")
    return rec


# ------------------------------------------------------------------------------------------------
# Loading from DB
# ------------------------------------------------------------------------------------------------

def month_range(month):
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def load_context(conn, start, end):
    shifts = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM shifts")}
    holidays = {r["hdate"]: dict(r) for r in conn.execute(
        "SELECT * FROM holidays WHERE hdate BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))}
    leaves = defaultdict(dict)
    for r in conn.execute("SELECT * FROM leaves WHERE leave_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
        leaves[r["employee_id"]][r["leave_date"]] = dict(r)
    overrides = defaultdict(dict)
    for r in conn.execute("SELECT * FROM day_overrides WHERE work_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
        overrides[r["employee_id"]][r["work_date"]] = dict(r)
    assignments = defaultdict(list)
    for r in conn.execute("SELECT * FROM employee_shifts WHERE from_date <= ? AND (to_date IS NULL OR to_date = '' OR to_date >= ?)",
                          ((end + dt.timedelta(days=STREAM_MARGIN_DAYS + 1)).isoformat(),
                           (start - dt.timedelta(days=STREAM_MARGIN_DAYS + 1)).isoformat())):
        assignments[r["employee_id"]].append(dict(r))
    penalties = defaultdict(dict)
    for r in conn.execute("SELECT * FROM late_penalties WHERE work_date BETWEEN ? AND ?", (start.isoformat(), end.isoformat())):
        penalties[r["employee_id"]][r["work_date"]] = int(r["minutes"])
    gates = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT * FROM gate_passes WHERE work_date BETWEEN ? AND ? ORDER BY out_time", (start.isoformat(), end.isoformat())):
        gates[r["employee_id"]][r["work_date"]].append(dict(r))
    roster = defaultdict(dict)
    try:
        for r in conn.execute("SELECT * FROM roster WHERE work_date BETWEEN ? AND ?",
                              ((start - dt.timedelta(days=STREAM_MARGIN_DAYS + 1)).isoformat(),
                               (end + dt.timedelta(days=STREAM_MARGIN_DAYS + 1)).isoformat())):
            roster[r["employee_id"]][r["work_date"]] = dict(r)
    except Exception:   # older database without the roster table
        pass
    return {"shifts": shifts, "holidays": holidays, "leaves": leaves, "overrides": overrides, "assignments": assignments,
            "penalties": penalties, "gates": gates, "roster": roster}


def suggest_penalty(late_min, settings):
    """Pre-filled OT minutes for a late day: fixed + factor x minutes late (Excel-style rounding)."""
    per_mark = float(settings.get("late_penalty_per_mark_min") or 0)
    factor = float(settings.get("late_penalty_factor") or 0)
    return int(math.floor(per_mark + factor * late_min + 0.5))


def load_streams(conn, device_user_ids, start, end, dedup_seconds, machine_only=False):
    """All usable punches per employee from a few days before `start` to a few days after `end`.
    machine_only: exactly what the device recorded - no manual punches, no IN/OUT/ignore decisions."""
    lo = dt.datetime.combine(start - dt.timedelta(days=STREAM_MARGIN_DAYS), dt.time())
    hi = dt.datetime.combine(end + dt.timedelta(days=STREAM_MARGIN_DAYS + 1), dt.time())
    q = ("SELECT id, device_user_id, ts, role, source FROM punches WHERE deleted = 0 AND COALESCE(role, '') != 'ignore' "
         "AND ts BETWEEN ? AND ?")
    if machine_only:
        q = "SELECT id, device_user_id, ts, NULL AS role, source FROM punches WHERE source = 'device' AND ts BETWEEN ? AND ?"
    args = [lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")]
    if device_user_ids is not None:
        q += " AND device_user_id IN (%s)" % ",".join("?" * len(device_user_ids))
        args += list(device_user_ids)
    raw = defaultdict(list)
    for r in conn.execute(q, args):
        raw[r["device_user_id"]].append({"ts": dt.datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S"),
                                         "role": r["role"] or None, "id": r["id"], "source": r["source"]})
    return {uid: dedup_punches(v, dedup_seconds) for uid, v in raw.items()}


def typical_times(sessions):
    """Median in / out hour of this employee's ordinary sessions (4-16 h), or None."""
    ins, outs = [], []
    for s in sessions:
        if s["orphan"]:
            continue
        h = (s["end"] - s["start"]).total_seconds() / 3600
        if 4 <= h <= 16:
            ins.append(_tod(s["start"]))
            outs.append(_tod(s["end"]) + (24 if s["end"].date() > s["start"].date() else 0))
    if len(ins) < 5:
        return None
    return statistics.median(ins), statistics.median(outs) % 24


def _suggest(orphan, prev_ts, next_ts, expected):
    """Propose the missing partner punch for an unpaired punch. expected = (usual_in_hour, usual_out_hour)."""
    t = orphan["start"]
    t_in, t_out = expected
    as_in = _circ(_tod(t), t_in) <= _circ(_tod(t), t_out)

    def at(base_date, hour):
        return dt.datetime.combine(base_date, dt.time(int(hour), int(round((hour % 1) * 60)) % 60))
    if as_in:
        cand = at(t.date(), t_out)
        if cand <= t:
            cand += dt.timedelta(days=1)
        if next_ts and cand >= next_ts:
            return None
        return {"of": t, "ts": cand, "role": "out", "orphan_role": "in"}
    cand = at(t.date(), t_in)
    if cand >= t:
        cand -= dt.timedelta(days=1)
    if prev_ts and cand <= prev_ts:
        return None
    return {"of": t, "ts": cand, "role": "in", "orphan_role": "out"}


def _shift_hours(shift):
    s, e = shift_bounds(dt.date(2000, 1, 3), shift)
    return _tod(s), _tod(e)


def employee_sessions(stream, shift, settings, shift_on=None):
    """Pair a stream twice: first with the rostered shift times, then with this employee's learned
    usual times for each shift. Returns (sessions, {shift key: (usual_in, usual_out)})."""
    shift_on = shift_on or (lambda d: shift)

    def key(s):
        return s.get("id") or s["name"]

    def base(ts):
        s = shift_on(ts.date())
        return _shift_hours(s) + (standard_minutes(s) / 60.0,)
    first = pair_sessions(stream, shift, settings, expect=base)
    groups = defaultdict(list)
    for s in first:
        groups[key(shift_on(s["start"].date()))].append(s)
    typ = {k: t for k, v in groups.items() if (t := typical_times(v))}
    if not typ:
        return first, typ

    def learned(ts):
        s = shift_on(ts.date())
        return typ.get(key(s), _shift_hours(s)) + (standard_minutes(s) / 60.0,)
    return pair_sessions(stream, shift, settings, expect=learned), typ


def make_shift_on(assignments, default_shift, shifts, roster=None):
    """assignments: rows {shift_id, from_date, to_date} for one employee; the latest-created covering row wins.
    roster: {date: row} planned shifts per day - a roster day with a shift wins over everything else."""
    rows = sorted(assignments, key=lambda a: a["id"], reverse=True)
    roster = roster or {}
    cache = {}

    def shift_on(d):
        if d not in cache:
            ds = d.isoformat()
            ro = roster.get(ds)
            if ro and ro.get("shift_id") and ro["shift_id"] in shifts:
                cache[d] = shifts[ro["shift_id"]]
            else:
                hit = next((a for a in rows if a["from_date"] <= ds and (not a["to_date"] or ds <= a["to_date"])), None)
                cache[d] = shifts.get(hit["shift_id"], default_shift) if hit else default_shift
        return cache[d]
    return shift_on


def with_work_hours(shifts, emp):
    """The employee's own working hours a day (e.g. 9 h) replace each shift's standard hours for that employee."""
    wm = emp.get("work_min") if hasattr(emp, "get") else (emp["work_min"] if "work_min" in emp.keys() else None)
    if not wm:
        return shifts
    return {k: dict(v, std_min=int(wm)) for k, v in shifts.items()}


def is_weekly_off(d, offs, roster_row=None):
    """Roster decides when it says so (1 = off, 0 = working day); otherwise the employee's weekly-off weekdays."""
    if roster_row is not None and roster_row.get("off") is not None:
        return bool(roster_row["off"])
    return d.weekday() in offs


NEIGHBOUR_DAYS = 7   # days evaluated either side of the range so the sandwich rule can see across month ends


def employee_days(conn, employees, start, end, settings, today=None, machine_only=False):
    """Compute day records for each employee between start and end (inclusive).
    machine_only: attendance exactly as the machine data gives it - ignores manual punches, punch decisions,
    forced statuses, leaves, late penalties and shift-roster changes (holidays still apply)."""
    outer_start, outer_end = start - dt.timedelta(days=NEIGHBOUR_DAYS), end + dt.timedelta(days=NEIGHBOUR_DAYS)
    ctx = load_context(conn, outer_start, outer_end)
    if machine_only:
        for k in ("leaves", "overrides", "assignments", "penalties", "roster"):
            ctx[k] = defaultdict(dict) if k != "assignments" else defaultdict(list)
        ctx["gates"] = defaultdict(lambda: defaultdict(list))
    default_id = next(iter(ctx["shifts"]))
    streams = load_streams(conn, [e["device_user_id"] for e in employees], outer_start, outer_end,
                           int(settings.get("dedup_seconds") or 0), machine_only)
    result = {}
    for e in employees:
        shifts_e = with_work_hours(ctx["shifts"], e)
        base_shift = shifts_e.get(e["shift_id"], shifts_e[default_id])
        roster_e = ctx["roster"].get(e["id"], {})
        shift_on = make_shift_on(ctx["assignments"].get(e["id"], []), base_shift, shifts_e, roster_e)
        stream = streams.get(e["device_user_id"], [])
        multi = bool(e.get("multi_punch"))
        emp_settings = dict(settings, break_pair_max_h=MULTI_GAP_MAX_H) if multi else settings
        sessions, typ = employee_sessions(stream, base_shift, emp_settings, shift_on)
        by_day = defaultdict(list)
        all_ts = [p["ts"] for p in stream]
        for s in sessions:
            by_day[work_date_of(s["start"], shift_on(s["start"].date()))].append(s)
        offs = {int(x) for x in (e["weekly_off"] or "").split(",") if x.strip() != ""}
        days, used = [], defaultdict(int)
        d = outer_start
        while d <= outer_end:
            ds = d.isoformat()
            shift = shift_on(d)
            active = (not e["join_date"] or ds >= e["join_date"][:10]) and (not e["exit_date"] or ds <= e["exit_date"][:10])
            rec = evaluate_day(
                d, None, shift, settings, sessions=by_day.get(d, []),
                weekly_off=is_weekly_off(d, offs, roster_e.get(ds)), holiday=ctx["holidays"].get(ds),
                leave=ctx["leaves"][e["id"]].get(ds), override=ctx["overrides"][e["id"]].get(ds),
                active=active, today=today, paid_break=bool(e.get("paid_break")), multi=multi,
                gate_passes=ctx["gates"][e["id"]].get(ds, ()))
            rec["shift"] = shift
            rec["late_penalty_min"] = ctx["penalties"][e["id"]].get(ds, 0)
            rec["late_penalty_set"] = ds in ctx["penalties"][e["id"]]
            rec["late_penalty_suggest"] = suggest_penalty(rec["late_min"], settings) if rec["late_min"] else 0
            rec["shift_changed"] = shift is not base_shift
            rec["roster"] = roster_e.get(ds)
            if rec["sessions"] and start <= d <= end:
                used[shift.get("id") or shift["name"]] += 1
            if not rec["override"] and "in progress" not in rec["flags"]:
                for s in rec["sessions"]:
                    if s["orphan"]:
                        k = all_ts.index(s["start"])
                        expected = typ.get(shift.get("id") or shift["name"], _shift_hours(shift))
                        rec["suggest"] = _suggest(s, all_ts[k - 1] if k else None,
                                                  all_ts[k + 1] if k + 1 < len(all_ts) else None, expected)
                        break
            days.append(rec)
            d += dt.timedelta(days=1)
        before = [r for r in days if r["date"] < start]
        after = [r for r in days if r["date"] > end]
        days = [r for r in days if start <= r["date"] <= end]
        main = max(used, key=used.get) if used else None
        main_shift = next((r["shift"] for r in days if (r["shift"].get("id") or r["shift"]["name"]) == main), base_shift)
        first_shift = days[0]["shift"] if days else base_shift
        result[e["id"]] = {"shift": main_shift, "days": days, "before": before, "after": after,
                           "typical": typ.get(first_shift.get("id") or first_shift["name"])}
    return result


def apply_sandwich(days, settings, before=(), after=()):
    """Sandwich rule: a weekly off / holiday whose nearest working days on BOTH sides are absences
    (absent or leave without pay) is not paid - e.g. Saturday absent + Monday absent => Sunday absent.
    `before` / `after` are the days just outside the range so a Sunday on the 1st or 31st is judged correctly."""
    if settings.get("sandwich_rule") != "1":
        return
    off_days = {"A", "LWP"} | ({"L"} if settings.get("sandwich_paid_leave") == "1" else set())
    seq = list(before) + list(days) + list(after)
    first = len(before)
    for i in range(first, first + len(days)):
        r = seq[i]
        if r["status"] in ("WO", "H") and not r["override"] and not r.get("mp_on_off"):   # came in: not sandwiched
            prev = next((x for x in reversed(seq[:i]) if x["status"] not in ("WO", "H")), None)
            nxt = next((x for x in seq[i + 1:] if x["status"] not in ("WO", "H")), None)
            if prev and nxt and prev["status"] in off_days and nxt["status"] in off_days:
                r["status"], r["off_credit"] = "A", 0.0
                r["flags"].append(f"sandwich rule ({prev['date']:%a} & {nxt['date']:%a} off)")


def summarize(days, shift, settings, salary_type="monthly", before=(), after=()):
    """Monthly totals + payable days for one employee."""
    apply_sandwich(days, settings, before, after)

    s = defaultdict(float)
    for r in days:
        s["cnt_" + r["status"]] += 1
        s["worked_min"] += r["worked_min"]
        s["ot_min"] += r["ot_min"]
        s["work_credit"] += r["work_credit"]
        s["extra_credit"] += r.get("extra_credit", 0.0)
        if r["late_min"]:
            s["late_count"] += 1
            s["late_min"] += r["late_min"]
        if r["early_min"]:
            s["early_count"] += 1
            s["early_min"] += r["early_min"]
        if r["off_kind"] == "wo":
            s["wo_credit"] += r["off_credit"]
        elif r["off_kind"] == "holiday":
            s["holiday_credit"] += r["off_credit"]
        elif r["off_kind"] == "leave":
            s["leave_credit"] += r["off_credit"]
        if "missing punch" in r["flags"] and "in progress" not in r["flags"] and not r["override"]:
            s["missing_punch"] += 1
        if any(f.startswith("long duty") for f in r["flags"]):
            s["long_duty"] += 1

    # Late-comer penalty: taken out of the month's overtime (never below zero, never from salary).
    s["ot_min_gross"] = s["ot_min"]
    # Penalty minutes are chosen day by day on the employee's sheet (late_penalties table).
    s["late_penalty_min"] = float(sum(r.get("late_penalty_min", 0) for r in days))
    s["late_penalty_days"] = float(sum(1 for r in days if r.get("late_penalty_min")))
    # Early-leave shortfall also comes out of overtime (not for hourly staff: they are only paid hours worked).
    s["early_short_min"] = 0.0 if salary_type == "hourly" else float(sum(r.get("early_short_min", 0) for r in days))
    s["early_short_days"] = 0.0 if salary_type == "hourly" else float(sum(1 for r in days if r.get("early_short_min")))
    taken = s["late_penalty_min"] + s["early_short_min"]
    s["ot_min"] = max(0.0, s["ot_min_gross"] - taken)
    s["late_penalty_unrecovered_min"] = max(0.0, taken - s["ot_min_gross"])

    wo_paid = settings.get("weekly_off_paid_daily" if salary_type == "daily" else "weekly_off_paid_monthly", "1") == "1"
    s["wo_paid"] = 1.0 if wo_paid else 0.0
    per = int(settings.get("late_marks_per_halfday") or 0)
    s["late_deduction_days"] = (int(s["late_count"]) // per) * 0.5 if per > 0 else 0.0
    s["paid_off_days"] = (s["wo_credit"] if wo_paid else 0.0) + s["holiday_credit"] + s["leave_credit"]
    s["payable_days"] = max(0.0, s["work_credit"] + s["extra_credit"] + s["paid_off_days"] - s["late_deduction_days"])
    s["std_min"] = paid_day_minutes(shift)   # paid working hours of the shift (rate basis), lunch excluded
    s["days_in_period"] = len(days)
    return dict(s)


def fmt_min(m):
    m = int(m or 0)
    return f"{m // 60}:{m % 60:02d}"
