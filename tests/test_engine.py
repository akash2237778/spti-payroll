import datetime as dt

import pytest

from payroll import engine, payroll
from payroll.db import DEFAULT_SETTINGS

SHIFT = {"name": "General", "start_time": "09:00", "end_time": "20:30", "break_min": 30, "break_after_min": 300,
         "grace_in_min": 15, "grace_out_min": 15, "full_day_min": 480, "half_day_min": 240,
         "ot_after_min": 30, "ot_round_min": 30, "day_start_hour": 5}
S = dict(DEFAULT_SETTINGS)
D = dt.date(2026, 9, 15)  # a Tuesday
TODAY = dt.date(2026, 10, 28)


def t(h, m=0, day=D):
    return dt.datetime.combine(day, dt.time(h, m))


def ev(punches, **kw):
    kw.setdefault("today", TODAY)
    return engine.evaluate_day(D, punches, SHIFT, S, **kw)


def test_standard_minutes():
    assert engine.standard_minutes(SHIFT) == 660  # 11.5h span - 30m break


def test_full_day_on_time():
    r = ev([t(9, 0), t(20, 30)])
    assert r["status"] == "P" and r["worked_min"] == 660 and r["work_credit"] == 1
    assert r["late_min"] == 0 and r["early_min"] == 0 and r["ot_min"] == 0


def test_late_and_early_out():
    r = ev([t(9, 40), t(19, 0)])
    assert r["late_min"] == 40 and r["early_min"] == 90
    assert r["status"] == "P"  # 9h20 - 30m break = 8h50 >= 8h


def test_grace_not_late():
    assert ev([t(9, 14), t(20, 30)])["late_min"] == 0


def test_half_day_and_too_short():
    r = ev([t(9), t(14)])
    assert r["status"] == "HD" and r["work_credit"] == 0.5 and r["worked_min"] == 300 - 0  # no break: span not > 300
    r = ev([t(9), t(11)])
    assert r["status"] == "A" and "too short" in r["flags"]


def test_ot_rounding():
    r = ev([t(9), t(22, 10)])  # 13h10 - 30 = 12h40, std 11h -> 100 extra -> 90 OT
    assert r["ot_min"] == 90
    r = ev([t(9), t(20, 50)])  # 20 extra < 30 threshold
    assert r["ot_min"] == 0


def test_ot_rounding_grace():
    # std 11h (worked = span - 30 lunch)
    assert ev([t(9), t(22, 29)])["ot_min"] == 120                 # 119 extra (1:59) -> 2:00
    assert ev([t(9), t(22, 59)])["ot_min"] == 120 + 30            # 149 extra -> 150 with grace
    assert ev([t(9), t(22, 54)])["ot_min"] == 120                 # 144 extra (6 short) -> 120
    sh = dict(SHIFT, ot_grace_min=0)
    assert engine.evaluate_day(D, [t(9), t(22, 59)], sh, S, today=TODAY)["ot_min"] == 120
    sh = dict(SHIFT, ot_grace_min=10)
    assert engine.evaluate_day(D, [t(9), t(22, 54)], sh, S, today=TODAY)["ot_min"] == 150
    # threshold also forgives the grace: 25 extra + 5 -> one 30-min block
    assert ev([t(9), t(20, 55)])["ot_min"] == 30
    assert ev([t(9), t(20, 54)])["ot_min"] == 0


def test_absent_and_future():
    assert ev([])["status"] == "A"
    assert engine.evaluate_day(dt.date(2026, 11, 3), [], SHIFT, S, today=TODAY)["status"] == "-"


def test_missing_punch_policy():
    r = ev([t(9)])
    assert r["status"] == "MP" and r["work_credit"] == 0.5
    r = engine.evaluate_day(D, [t(9)], SHIFT, dict(S, missing_punch_policy="absent"), today=TODAY)
    assert r["work_credit"] == 0


def test_weekly_off_and_work_on_it():
    r = ev([], weekly_off=True)
    assert r["status"] == "WO" and r["off_credit"] == 1
    # Company rule (default): Sunday stays a paid weekly off; hours worked on it are overtime, no extra day.
    r = ev([t(9), t(17)], weekly_off=True)
    assert r["status"] == "WOP" and r["off_credit"] == 1 and r["extra_credit"] == 0 and r["ot_min"] == 450
    r = ev([t(9)], weekly_off=True)   # forgot to punch out on Sunday: paid as the weekly off, no extra day
    assert r["status"] == "WO" and r["mp_on_off"] and r["off_credit"] == 1 and r["extra_credit"] == 0
    assert "missing punch" in r["flags"] and r["work_credit"] == 0
    # Optional alternative: an extra day's wage
    r = engine.evaluate_day(D, [t(9), t(17)], SHIFT, dict(S, wo_work_mode="extra_day"), weekly_off=True, today=TODAY)
    assert r["extra_credit"] == 0.5


def test_payable_days_never_exceed_month_when_sunday_work_is_ot():
    # someone who works every single day of a 30-day month, Sundays included
    days = []
    for i in range(30):
        d = dt.date(2026, 9, 1) + dt.timedelta(days=i)
        days.append(engine.evaluate_day(d, [t(9, day=d), t(20, 30, day=d)], SHIFT, S, weekly_off=d.weekday() == 6, today=TODAY))
    s = engine.summarize(days, SHIFT, S, "monthly")
    assert s["payable_days"] == 30 and s["cnt_WOP"] == 4
    assert s["ot_min"] == 4 * 660   # each Sunday: 11h worked, all overtime


def test_holiday_and_leave():
    hol = {"name": "Diwali", "paid": 1}
    assert ev([], holiday=hol)["status"] == "H"
    assert ev([], holiday=dict(hol, paid=0))["off_credit"] == 0
    lv = {"leave_type": "CL", "paid": 1, "half_day": 0}
    r = ev([], leave=lv)
    assert r["status"] == "L" and r["off_credit"] == 1
    assert ev([], leave=dict(lv, paid=0))["status"] == "LWP"


def test_override_times_and_status():
    r = ev([t(9)], override={"status": None, "in_time": "09:00", "out_time": "20:30"})
    assert r["status"] == "P" and r["worked_min"] == 660 and r["override"]
    r = ev([], override={"status": "P", "in_time": None, "out_time": None})
    assert r["status"] == "P" and r["work_credit"] == 1
    r = ev([t(9), t(20)], override={"status": "A", "in_time": None, "out_time": None})
    assert r["status"] == "A" and r["work_credit"] == 0


def test_cross_midnight_attribution():
    p = dt.datetime(2026, 10, 15, 0, 4)
    assert engine.work_date_of(p, SHIFT) == dt.date(2026, 10, 14)
    assert engine.work_date_of(dt.datetime(2026, 10, 15, 6, 0), SHIFT) == dt.date(2026, 10, 15)


def test_dedup():
    ts = [t(9, 0), t(9, 1), t(20, 0)]
    assert engine.dedup(ts, 120) == [t(9, 0), t(20, 0)]


def test_pairs_mode():
    s = dict(S, calc_mode="pairs")
    r = engine.evaluate_day(D, [t(9), t(13), t(14), t(20)], SHIFT, s, today=TODAY)
    assert r["worked_min"] == 600


def _month_days(statuses):
    days = []
    for i, st in enumerate(statuses):
        d = dt.date(2026, 9, 1) + dt.timedelta(days=i)
        if st == "P":
            days.append(engine.evaluate_day(d, [t(9, day=d), t(20, 30, day=d)], SHIFT, S, today=TODAY))
        elif st == "WO":
            days.append(engine.evaluate_day(d, [], SHIFT, S, weekly_off=True, today=TODAY))
        elif st == "A":
            days.append(engine.evaluate_day(d, [], SHIFT, S, today=TODAY))
    return days


def test_summary_and_monthly_salary():
    # 30 days: 26 present, 4 weekly offs
    pattern = (["P"] * 6 + ["WO"]) * 4 + ["P", "P"]
    days = _month_days(pattern)
    s = engine.summarize(days, SHIFT, S, "monthly")
    assert s["payable_days"] == 30
    emp = {"salary_type": "monthly", "salary_rate": 15000, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 0, "esi_enabled": 1}
    p = payroll.calculate(emp, s, "2026-09", S)
    # ESI applies to every employee (covered establishment): 0.75% of 15,000 = 112.50 -> 113
    assert p["basic"] == 15000 and p["esi"] == 113 and p["esi_employer"] == 488 and p["net"] == 15000 - 113


def test_daily_wage_unpaid_weekoff_and_absence():
    pattern = (["P"] * 6 + ["WO"]) * 4 + ["A", "P"]
    s = engine.summarize(_month_days(pattern), SHIFT, S, "daily")
    assert s["payable_days"] == 25  # weekly off not paid for daily wage by default
    emp = {"salary_type": "daily", "salary_rate": 500, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 0, "esi_enabled": 0}
    assert payroll.calculate(emp, s, "2026-09", S)["basic"] == 12500


def test_sandwich_rule():
    days = _month_days(["A", "WO", "A", "P"])
    s = engine.summarize(days, SHIFT, dict(S, sandwich_rule="1"), "monthly")
    assert days[1]["status"] == "A" and s["payable_days"] == 1


def test_ot_pay_pf_esi_adjustments():
    s = {"payable_days": 30, "std_min": 660, "worked_min": 0, "ot_min": 600, "paid_off_days": 4}
    emp = {"salary_type": "monthly", "salary_rate": 16500, "ot_enabled": 1, "ot_multiplier": 2, "pf_enabled": 1, "esi_enabled": 1}
    adj = [{"kind": "advance", "amount": 1000, "note": ""}, {"kind": "allowance", "amount": 500, "note": ""}]
    p = payroll.calculate(emp, s, "2026-09", S, adj)
    # per day 550, hour 50, OT 10h x 50 x 2 = 1000
    assert p["per_day"] == 550 and p["hour_rate"] == 50 and p["ot_pay"] == 1000
    assert p["gross"] == 18000
    assert p["pf"] == 1800  # 12% of min(16500, 15000)
    # ESI wages exclude overtime: 18000 - 1000 = 17000; 0.75% = 127.50 -> rounded up to 128
    assert p["esi_wages"] == 17000 and p["esi"] == 128 and p["esi_employer"] == 553
    assert p["net"] == round(18000 - 1800 - 128 - 1000)


def test_late_mark_deduction():
    days = [engine.evaluate_day(dt.date(2026, 9, i), [t(10, day=dt.date(2026, 9, i)), t(20, 30, day=dt.date(2026, 9, i))], SHIFT, S, today=TODAY)
            for i in range(1, 7)]
    s = engine.summarize(days, SHIFT, dict(S, late_marks_per_halfday="3"), "monthly")
    assert s["late_count"] == 6 and s["late_deduction_days"] == 1.0 and s["payable_days"] == 5


# ---------------------------------------------------------------- cross-day session pairing

def _stream(*items):
    out = []
    for it in items:
        ts, role = (it, None) if isinstance(it, dt.datetime) else it
        out.append({"ts": ts, "role": role, "id": len(out) + 1})
    return out


def _days(stream, start, end, settings=S, offs=(6,)):
    sessions, _ = engine.employee_sessions(stream, SHIFT, settings)
    by = {}
    for s in sessions:
        by.setdefault(engine.work_date_of(s["start"], SHIFT), []).append(s)
    res, d = {}, start
    while d <= end:
        res[d] = engine.evaluate_day(d, None, SHIFT, settings, sessions=by.get(d, []), weekly_off=d.weekday() in offs, today=TODAY)
        d += dt.timedelta(days=1)
    return res


def K(day, h, m):
    return dt.datetime(2026, 10, day, h, m)


LONG_DUTY = [K(1, 8, 56), K(2, 8, 30), K(3, 8, 54), K(4, 5, 0), K(4, 9, 15), K(4, 17, 36)]


def test_long_duties_pair_across_midnight():
    days = _days(_stream(*LONG_DUTY), dt.date(2026, 10, 1), dt.date(2026, 10, 4))
    d1, d2, d3, d4 = (days[dt.date(2026, 10, i)] for i in (1, 2, 3, 4))
    assert d1["status"] == "P" and d1["in"] == K(1, 8, 56) and d1["out"] == K(2, 8, 30)
    assert d1["worked_min"] == 23 * 60 + 34 - 30 and "long duty" in d1["flags"]
    assert d1["ot_min"] == 720  # 23h04 worked - 11h standard = 12h04 -> 12h
    assert d2["status"] == "A"  # rest day after the 24h duty: no new shift started
    assert d3["status"] == "P" and d3["out"] == K(4, 5, 0)
    assert d4["status"] == "WOP" and d4["in"] == K(4, 9, 15) and d4["out"] == K(4, 17, 36)  # Sunday


def test_long_duty_double_day_mode():
    days = _days(_stream(*LONG_DUTY), dt.date(2026, 10, 1), dt.date(2026, 10, 1), dict(S, long_duty_mode="double"))
    d1 = days[dt.date(2026, 10, 1)]
    assert d1["work_credit"] + d1["extra_credit"] == 2 and d1["ot_min"] == 60  # 23h04 - 22h


def test_one_fix_repairs_following_days():
    # forgot to punch out on the 6th; 7th and 8th normal
    base = [K(6, 9, 0), K(7, 9, 2), K(7, 20, 30), K(8, 9, 1), K(8, 20, 35)]
    days = _days(_stream(*base), dt.date(2026, 10, 6), dt.date(2026, 10, 8))
    assert [days[dt.date(2026, 10, i)]["status"] for i in (6, 7, 8)] == ["MP", "P", "P"]
    fixed = _days(_stream(*(base[:1] + [(K(6, 20, 30), "out")] + base[1:])), dt.date(2026, 10, 6), dt.date(2026, 10, 8))
    assert [fixed[dt.date(2026, 10, i)]["status"] for i in (6, 7, 8)] == ["P", "P", "P"]


def test_marking_one_punch_reflows_the_chain():
    # Ambiguous stream: auto pairing treats 08:30 on the 2nd as end of a 24h duty.
    # Saying "08:30 was an arrival" re-pairs everything after it.
    stream = [K(1, 8, 56), K(2, 8, 30), K(2, 20, 40), K(3, 9, 0), K(3, 20, 30)]
    auto = _days(_stream(*stream), dt.date(2026, 10, 1), dt.date(2026, 10, 3))
    assert auto[dt.date(2026, 10, 1)]["status"] == "MP"  # evening punch on the 2nd makes 08:30 an arrival
    assert auto[dt.date(2026, 10, 2)]["in"] == K(2, 8, 30) and auto[dt.date(2026, 10, 3)]["status"] == "P"
    forced = _days(_stream(K(1, 8, 56), (K(2, 8, 30), "out"), K(2, 20, 40), K(3, 9, 0), K(3, 20, 30)),
                   dt.date(2026, 10, 1), dt.date(2026, 10, 3))
    assert forced[dt.date(2026, 10, 1)]["out"] == K(2, 8, 30)
    assert forced[dt.date(2026, 10, 2)]["status"] == "MP"  # 20:40 now unpaired
    assert forced[dt.date(2026, 10, 3)]["status"] == "P"


def test_lunch_punches_stay_in_one_session():
    sessions = engine.pair_sessions([t(9), t(13), t(13, 45), t(20, 30)], SHIFT, S)
    assert len(sessions) == 1 and not sessions[0]["orphan"]


def test_stray_punch_not_swallowed_by_long_session():
    sessions = engine.pair_sessions([dt.datetime(2026, 9, 27, 17, 34), dt.datetime(2026, 9, 28, 12, 24),
                                     dt.datetime(2026, 9, 28, 21, 33)], SHIFT, S, (9.2, 21.4))
    assert sessions[0]["orphan"] and sessions[1]["start"] == dt.datetime(2026, 9, 28, 12, 24)


def test_suggestion_for_missing_out():
    sug = engine._suggest({"start": K(6, 9, 0)}, None, K(7, 9, 2), (9.0, 20.5))
    assert sug["role"] == "out" and sug["ts"] == K(6, 20, 30)
    sug = engine._suggest({"start": K(6, 20, 40)}, K(5, 21, 0), None, (9.0, 20.5))
    assert sug["role"] == "in" and sug["ts"] == K(6, 9, 0)


# ---------------------------------------------------------------- shift roster (day / night rotation)

NIGHT = dict(SHIFT, id=2, name="Night", start_time="21:00", end_time="09:00", day_start_hour=12)
DAY = dict(SHIFT, id=1)
NIGHT_PUNCHES = [dt.datetime(2026, 9, 29, 9, 14), dt.datetime(2026, 9, 29, 21, 2), dt.datetime(2026, 9, 30, 9, 0),
        dt.datetime(2026, 9, 30, 21, 7), K(1, 9, 30), K(1, 21, 3), K(2, 8, 29), K(3, 20, 59), K(4, 9, 9)]


def _roster_days(stream, rows, start, end):
    shift_on = engine.make_shift_on(rows, DAY, {1: DAY, 2: NIGHT})
    sessions, _ = engine.employee_sessions(_stream(*stream), DAY, S, shift_on)
    by = {}
    for s in sessions:
        by.setdefault(engine.work_date_of(s["start"], shift_on(s["start"].date())), []).append(s)
    out, d = {}, start
    while d <= end:
        out[d] = engine.evaluate_day(d, None, shift_on(d), S, sessions=by.get(d, []), weekly_off=d.weekday() == 6, today=TODAY)
        d += dt.timedelta(days=1)
    return out


def test_night_worker_shift_pairs_evening_to_morning():
    days = _roster_days(NIGHT_PUNCHES, [{"id": 1, "shift_id": 2, "from_date": "2026-09-01", "to_date": None}],
                        dt.date(2026, 9, 29), dt.date(2026, 10, 3))
    oct1 = days[dt.date(2026, 10, 1)]
    assert oct1["status"] == "P" and oct1["in"] == K(1, 21, 3) and oct1["out"] == K(2, 8, 29)
    assert oct1["late_min"] == 0 and oct1["early_min"] == 31
    assert days[dt.date(2026, 9, 30)]["in"] == dt.datetime(2026, 9, 30, 21, 7)
    assert days[dt.date(2026, 9, 30)]["out"] == K(1, 9, 30)
    assert days[dt.date(2026, 10, 3)]["out"] == K(4, 9, 9)


def test_same_punches_on_day_roster_pair_differently():
    days = _roster_days(NIGHT_PUNCHES, [], dt.date(2026, 9, 29), dt.date(2026, 9, 30))
    assert days[dt.date(2026, 9, 29)]["in"] == dt.datetime(2026, 9, 29, 9, 14)
    assert days[dt.date(2026, 9, 29)]["out"] == dt.datetime(2026, 9, 29, 21, 2)


def test_multiple_shift_changes_within_month_latest_wins():
    rows = [{"id": 1, "shift_id": 2, "from_date": "2026-10-01", "to_date": "2026-10-10"},
            {"id": 2, "shift_id": 1, "from_date": "2026-10-05", "to_date": "2026-10-06"}]
    on = engine.make_shift_on(rows, DAY, {1: DAY, 2: NIGHT})
    assert [on(dt.date(2026, 10, i))["name"] for i in (1, 4, 5, 6, 7, 11)] == ["Night", "Night", "General", "General", "Night", "General"]


# ---------------------------------------------------------------- late penalty reduces overtime

def test_late_penalty_only_on_days_chosen():
    d1, d2, d3 = dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3)
    days = [engine.evaluate_day(d1, [t(9, 40, d1), t(22, 30, d1)], SHIFT, S, today=TODAY),   # 40 late, 80 extra -> OT 60
            engine.evaluate_day(d2, [t(9, 0, d2), t(22, 30, d2)], SHIFT, S, today=TODAY),    # on time, OT 120
            engine.evaluate_day(d3, [t(9, 30, d3), t(22, 0, d3)], SHIFT, S, today=TODAY)]    # 30 late, OT 60
    assert [d["ot_min"] for d in days] == [60, 120, 60]
    assert engine.summarize([dict(d) for d in days], SHIFT, S)["ot_min"] == 240   # nothing chosen -> no penalty
    days[0]["late_penalty_min"] = 70   # penalized on the sheet; day 3 deliberately forgiven
    s = engine.summarize(days, SHIFT, S)
    assert s["late_penalty_min"] == 70 and s["late_penalty_days"] == 1 and s["ot_min_gross"] == 240 and s["ot_min"] == 170
    assert engine.suggest_penalty(40, dict(S, late_penalty_per_mark_min="30", late_penalty_factor="1")) == 70


def test_late_penalty_never_below_zero_and_not_from_salary():
    d = dt.date(2026, 9, 1)
    days = [engine.evaluate_day(d, [t(11, 0, d), t(20, 30, d)], SHIFT, S, today=TODAY)]  # 120 late, no OT
    days[0]["late_penalty_min"] = 240
    s = engine.summarize(days, SHIFT, S)
    # 240 chosen penalty + 120 short-day shortfall (9 h worked on this 11 h test shift), none of it recoverable
    assert s["ot_min"] == 0 and s["late_penalty_unrecovered_min"] == 360
    emp = {"salary_type": "monthly", "salary_rate": 30000, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 0, "esi_enabled": 0}
    assert payroll.calculate(emp, dict(s, payable_days=1), "2026-09", S)["basic"] == 1000


# ---------------------------------------------------------------- 8-hour day with a fixed lunch window

SHIFT8 = dict(SHIFT, start_time="09:00", end_time="17:30", break_min=30, break_start="13:00", full_day_min=465)


def ev8(a, b, paid_break=False, day=D):
    return engine.evaluate_day(day, [t(*a, day=day), t(*b, day=day)], SHIFT8, S, today=TODAY, paid_break=paid_break)


def test_eight_hour_day_and_overtime():
    assert engine.standard_minutes(SHIFT8) == 480
    r = ev8((9, 0), (17, 30))
    assert r["worked_min"] == 480 and r["status"] == "P" and r["ot_min"] == 0 and r["early_min"] == 0
    r = ev8((9, 0), (20, 30))   # 3 h past 17:30 -> 3 h OT
    assert r["worked_min"] == 660 and r["ot_min"] == 180
    r = ev8((9, 0), (17, 20))   # 10 min early, within grace: still a full day
    assert r["status"] == "P" and r["early_min"] == 0


def test_lunch_deducted_only_if_present_during_lunch():
    assert ev8((14, 0), (20, 0))["worked_min"] == 360          # came after lunch: nothing deducted
    assert ev8((9, 0), (13, 0))["worked_min"] == 240           # left at lunch
    assert ev8((9, 0), (13, 15))["worked_min"] == 240          # 15 of the 30 lunch minutes overlapped
    assert ev8((13, 10), (17, 30))["worked_min"] == 240        # 20 min of lunch overlapped


def test_paid_lunch_break():
    r = ev8((9, 0), (17, 30), paid_break=True)
    assert r["worked_min"] == 510 and r["ot_min"] == 0          # lunch paid, but no OT before 17:30
    r = ev8((9, 0), (19, 30), paid_break=True)
    assert r["worked_min"] == 630 and r["ot_min"] == 120        # OT still counted from 17:30
    assert engine.ot_base_minutes(SHIFT8, True) == 510 and engine.ot_base_minutes(SHIFT8) == 480
    emp = {"salary_type": "hourly", "salary_rate": 100, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 0, "esi_enabled": 0}
    sm = {"payable_days": 1, "std_min": 480, "worked_min": 510, "ot_min": 0, "paid_off_days": 0}
    assert payroll.calculate(emp, sm, "2026-09", S)["basic"] == 850   # 8.5 paid hours x 100


# ---------------------------------------------------------------- sandwich rule (company rule: on by default)

def _day(d, kind):
    if kind == "P":
        return engine.evaluate_day(d, [t(9, day=d), t(20, 30, day=d)], SHIFT, S, today=TODAY)
    if kind == "A":
        return engine.evaluate_day(d, [], SHIFT, S, today=TODAY)
    if kind == "WO":
        return engine.evaluate_day(d, [], SHIFT, S, weekly_off=True, today=TODAY)
    if kind == "H":
        return engine.evaluate_day(d, [], SHIFT, S, holiday={"name": "Holiday", "paid": 1}, today=TODAY)
    if kind == "L":
        return engine.evaluate_day(d, [], SHIFT, S, leave={"leave_type": "CL", "paid": 1, "half_day": 0}, today=TODAY)


def _week(kinds, first=dt.date(2026, 10, 3)):   # 3 Oct 2026 is a Saturday
    return [_day(first + dt.timedelta(days=i), k) for i, k in enumerate(kinds)]


def test_sandwich_is_on_by_default():
    assert S["sandwich_rule"] == "1"
    days = _week(["A", "WO", "A"])            # Sat absent, Sun off, Mon absent
    s = engine.summarize(days, SHIFT, S)
    assert days[1]["status"] == "A" and days[1]["off_credit"] == 0 and "sandwich rule (Sat & Mon off)" in days[1]["flags"]
    assert s["payable_days"] == 0


def test_sandwich_needs_both_sides_off():
    for kinds in (["P", "WO", "A"], ["A", "WO", "P"], ["P", "WO", "P"]):
        days = _week(kinds)
        engine.summarize(days, SHIFT, S)
        assert days[1]["status"] == "WO", kinds


def test_sandwich_across_month_boundary():
    # Sun 1 Mar 2026: Sat 28 Feb is in the previous month, Mon 2 Mar absent
    sat, sun, mon = dt.date(2026, 2, 28), dt.date(2026, 3, 1), dt.date(2026, 3, 2)
    assert sun.weekday() == 6
    before = [_day(sat, "A")]
    nov = [_day(sun, "WO"), _day(mon, "A")]
    engine.summarize(nov, SHIFT, S, before=before)
    assert nov[0]["status"] == "A"
    nov2 = [_day(sun, "WO"), _day(mon, "A")]
    engine.summarize(nov2, SHIFT, S, before=[_day(sat, "P")])     # worked Saturday -> Sunday paid
    assert nov2[0]["status"] == "WO"


def test_sandwich_paid_leave_option_and_holiday_chain():
    days = _week(["L", "WO", "L"])
    engine.summarize(days, SHIFT, S)
    assert days[1]["status"] == "WO"                                     # paid leave: Sunday stays paid
    days = _week(["L", "WO", "L"])
    engine.summarize(days, SHIFT, dict(S, sandwich_paid_leave="1"))
    assert days[1]["status"] == "A"
    days = _week(["A", "WO", "H", "A"])                                  # Sat absent, Sun, Mon holiday, Tue absent
    engine.summarize(days, SHIFT, S)
    assert days[1]["status"] == "A" and days[2]["status"] == "A"


def test_sandwich_can_be_turned_off():
    days = _week(["A", "WO", "A"])
    engine.summarize(days, SHIFT, dict(S, sandwich_rule="0"))
    assert days[1]["status"] == "WO"


# ---------------------------------------------------------------- short days by hours worked (company rule)

SHIFT8H = dict(SHIFT8, full_day_min=360, half_day_min=240)


def ev8h(a, b, settings=S, day=D):
    return engine.evaluate_day(day, [t(*a, day=day), t(*b, day=day)], SHIFT8H, settings, today=TODAY)


def test_six_to_eight_hours_full_day_shortfall_from_ot():
    assert S["short_day_rule"] == "1"
    r = ev8h((9, 0), (16, 0))                 # 7h - 30 lunch = 6:30 -> full day, 1:30 short
    assert r["status"] == "P" and r["work_credit"] == 1 and r["early_short_min"] == 90 and r["ot_min"] == 0
    r = ev8h((9, 0), (15, 30))                # exactly 6:00 -> full day, 2:00 short
    assert r["status"] == "P" and r["early_short_min"] == 120
    r = ev8h((11, 0), (19, 0))                # late arrival, 7:30 worked: still by hours -> full day, 0:30 short
    assert r["status"] == "P" and r["early_short_min"] == 30
    assert ev8h((9, 0), (17, 30))["early_short_min"] == 0


def test_four_to_six_hours_half_day_rest_as_ot():
    r = ev8h((9, 0), (14, 0))                 # 4:30 worked -> half day + 0:30 OT
    assert r["status"] == "HD" and r["work_credit"] == 0.5 and r["ot_min"] == 30 and r["early_short_min"] == 0
    r = ev8h((9, 0), (15, 15))                # 5:45 -> half day + 1:30 OT (30-min blocks)
    assert r["status"] == "HD" and r["ot_min"] == 90
    r = ev8h((9, 0), (13, 0))                 # 4:00 -> half day, nothing extra
    assert r["status"] == "HD" and r["ot_min"] == 0
    r = ev8h((9, 0), (12, 0))                 # under 4 h -> absent
    assert r["status"] == "A"


def test_short_day_rule_off_restores_plain_thresholds():
    off = dict(S, short_day_rule="0")
    r = ev8h((9, 0), (16, 0), off)
    assert r["status"] == "P" and r["early_short_min"] == 0
    r = ev8h((9, 0), (14, 0), off)
    assert r["status"] == "HD" and r["ot_min"] == 0


def test_short_day_month_totals():
    d1, d2, d3 = dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3)
    days = [ev8h((9, 0), (16, 0), day=d1),       # full, 90 short
            ev8h((9, 0), (14, 0), day=d2),       # half, 30 OT
            ev8h((9, 0), (20, 30), day=d3)]      # full, 180 OT
    s = engine.summarize(days, SHIFT8H, S, "monthly")
    assert s["payable_days"] == 2.5 and s["ot_min_gross"] == 210 and s["early_short_min"] == 90 and s["ot_min"] == 120


# ---------------------------------------------------------------- manually edited final totals

def test_month_override_keeps_calculated_values():
    s = {"payable_days": 26.5, "ot_min": 600.0}
    payroll.apply_month_override(s, None)
    assert s["calc_payable_days"] == 26.5 and s["calc_ot_min"] == 600 and s["edited"] == {}
    s = {"payable_days": 26.5, "ot_min": 600.0}
    payroll.apply_month_override(s, {"payable_days": 27, "ot_min": None, "reason": "approved", "created_by": "hr"})
    assert s["payable_days"] == 27 and s["ot_min"] == 600 and s["calc_payable_days"] == 26.5 and s["edited"] == {"payable_days": True}
    emp = {"salary_type": "monthly", "salary_rate": 31000, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 0, "esi_enabled": 0}
    p = payroll.calculate(emp, dict(s, std_min=480, worked_min=0, paid_off_days=0), "2026-10", S)
    assert p["basic"] == 27000 and p["payable_days"] == 27


def test_parse_hours():
    from payroll.app import parse_hours
    assert parse_hours("95:30") == 5730 and parse_hours("95.5") == 5730 and parse_hours("2") == 120 and parse_hours("") is None


# ---------------------------------------------------------------- staff: payable days only, no overtime pay

def test_staff_paid_on_payable_days_only():
    s = {"payable_days": 26, "std_min": 480, "worked_min": 0, "ot_min": 1200, "paid_off_days": 4}
    worker = {"salary_type": "monthly", "salary_rate": 26000, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 0,
              "esi_enabled": 0, "category": "worker"}
    staff = dict(worker, category="staff")
    pw, ps = payroll.calculate(worker, s, "2026-11", S), payroll.calculate(staff, s, "2026-11", S)   # Nov: 30 days
    assert pw["ot_pay"] > 0 and ps["ot_pay"] == 0 and ps["ot_hours"] == 0
    assert ps["basic"] == pw["basic"] == round(26000 / 30 * 26, 2) and ps["gross"] == ps["basic"] and ps["category"] == "staff"
    assert payroll.calculate(dict(worker, category=None), s, "2026-11", S)["ot_pay"] == pw["ot_pay"]   # default = worker



# ---------------------------------------------------------------- bank / cash split and advances

def test_bank_cash_split_modes():
    sp = payroll.split_bank_cash
    assert sp({"pay_mode": "bank"}, 20000)[:2] == (20000, 0)
    assert sp({}, 20000)[:2] == (20000, 0)                                       # default = all bank
    assert sp({"pay_mode": "cash"}, 20000)[:2] == (0, 20000)
    assert sp({"pay_mode": "fixed", "bank_value": 15000}, 20000)[:2] == (15000, 5000)
    assert sp({"pay_mode": "fixed", "bank_value": 15000}, 9000)[:2] == (9000, 0)   # never more than net
    assert sp({"pay_mode": "percent", "bank_value": 60}, 20001)[:2] == (12001, 8000)
    assert sp({"pay_mode": "cash"}, 20000, bank_override=7000)[:2] == (7000, 13000)  # this month's amount wins
    emp = {"salary_type": "monthly", "salary_rate": 30000, "ot_enabled": 0, "ot_multiplier": 1, "pf_enabled": 0,
           "esi_enabled": 0, "pay_mode": "fixed", "bank_value": 10000}
    pay = payroll.calculate(emp, {"payable_days": 30, "std_min": 480, "worked_min": 0, "ot_min": 0, "paid_off_days": 0}, "2026-11", S)
    assert pay["net"] == 30000 and pay["bank"] == 10000 and pay["cash"] == 20000


def test_advance_ledger(tmp_path):
    from payroll import db as dbm
    old = dbm.DB_PATH
    dbm.DB_PATH = str(tmp_path / "adv.db")
    try:
        dbm.init_db()
        with dbm.tx() as c:
            c.execute("INSERT INTO employees(id, device_user_id, name) VALUES (1, '1', 'A')")
            c.execute("INSERT INTO advances(employee_id, given_date, amount, installment) VALUES (1, '2026-09-10', 6000, 2000)")
            c.execute("INSERT INTO adjustments(employee_id, month, kind, amount) VALUES (1, '2026-09', 'advance', 2000)")
            c.execute("INSERT INTO adjustments(employee_id, month, kind, amount) VALUES (1, '2026-10', 'advance', 1500)")
            oct_ = payroll.advance_ledger(c, "2026-10")[1]
            nov = payroll.advance_ledger(c, "2026-11")[1]
            aug = payroll.advance_ledger(c, "2026-08").get(1)
        assert (oct_["outstanding_before"], oct_["this_month"], oct_["remaining"], oct_["suggest"]) == (4000, 1500, 2500, 2000)
        assert (nov["outstanding_before"], nov["this_month"], nov["suggest"]) == (2500, 0, 2000)
        assert aug is None or aug["given"] == 0                                    # not given yet in August
    finally:
        dbm.DB_PATH = old


# ---------------------------------------------------------------- ESIC (Code on Social Security 2020, s.2(88))

def test_esi_wages_exclude_overtime_with_half_rule():
    assert payroll.esi_wages(18000, 0) == 18000
    assert payroll.esi_wages(18000, 3000) == 18000                 # OT is an excluded head
    assert payroll.esi_wages(4000, 6000) == 5000                   # OT > half of total: excess (6000-5000) added back
    assert payroll.esi_wages(10000, 2000, allowance=1000, bonus=500) == 11500


def test_esi_rates_rounding_ceiling_and_low_wage():
    emp = {"esi_enabled": 1}
    st = dict(S)
    c = payroll.esi_contribution(emp, st, basic=15001, ot_pay=0, payable_days=26)
    assert c["esi_covered"] and c["esi"] == 113 and c["esi_employer"] == 488          # rounded UP to the rupee
    assert payroll.esi_contribution(emp, st, basic=21000, ot_pay=4000, payable_days=26)["esi_covered"]   # OT not counted
    assert not payroll.esi_contribution(emp, st, basic=21001, ot_pay=0, payable_days=26)["esi_covered"]
    assert payroll.esi_contribution(dict(emp, disabled_person=1), st, basic=24000, ot_pay=0, payable_days=26)["esi_covered"]
    low = payroll.esi_contribution(emp, st, basic=4000, ot_pay=0, payable_days=26)          # 153.85 a day
    assert low["esi"] == 0 and low["esi_employer"] == 130 and "no employee share" in low["esi_note"]
    assert payroll.esi_contribution({"esi_enabled": 0}, st, basic=15000, ot_pay=0, payable_days=26)["esi"] == 0   # unticked: no ESI
    assert payroll.esi_contribution({"esi_enabled": 0}, dict(st, esi_mode="all"), basic=15000, ot_pay=0, payable_days=26)["esi"] == 0
    # covered at the start of the period: continues even above the ceiling
    cont = payroll.esi_contribution(emp, st, basic=26000, ot_pay=0, payable_days=26, covered=True)
    assert cont["esi"] == 195 and "start of the contribution period" in cont["esi_note"]


def test_esi_contribution_periods():
    cp = payroll.contribution_period
    assert [cp(m) for m in ("2026-04", "2026-09", "2026-10", "2026-12", "2027-01", "2027-03")] == \
        ["2026-04", "2026-04", "2026-10", "2026-10", "2026-10", "2026-10"]


# ---------------------------------------------------------------- salary components (legal ESI structuring)

def test_salary_components_esi_pf_and_bank():
    on = dict(S, salary_components="1")
    emp = {"salary_type": "monthly", "salary_rate": 30000, "ot_enabled": 1, "ot_multiplier": 1, "pf_enabled": 1, "esi_enabled": 1,
           "comp_hra": 10000, "comp_conveyance": 5000, "comp_other": 0, "pay_mode": "esi"}
    s = {"payable_days": 30, "std_min": 480, "worked_min": 0, "ot_min": 0, "paid_off_days": 0}
    p = payroll.calculate(emp, s, "2026-11", on)                     # November: 30 days
    assert (p["basic_da"], p["hra"], p["conveyance"]) == (15000, 10000, 5000) and p["components"]
    assert p["esi_wages"] == 15000 and p["esi_covered"] and p["esi"] == 113         # legally within the ESI ceiling
    assert p["pf"] == 1800                                                           # PF on Basic + DA
    assert p["bank"] == 15000 and p["cash"] == p["net"] - 15000                     # ESI wages by bank, rest cash
    # with overtime the excluded heads pass one-half: the excess counts back as ESI wages
    p2 = payroll.calculate(emp, dict(s, ot_min=480 * 60 // 60 * 10), "2026-11", on)  # 80 h OT
    total = p2["basic"] + p2["ot_pay"]
    expected = total - (p2["ot_pay"] + 15000) + max(0, p2["ot_pay"] + 15000 - total / 2)
    assert abs(p2["esi_wages"] - expected) < 0.01
    # setting off (or no split entered): whole salary is Basic + DA, as before
    off = payroll.calculate(emp, s, "2026-11", S)
    assert not off["components"] and off["esi_wages"] == 30000 and not off["esi_covered"] and off["pf"] == 1800
    bad = payroll.salary_parts(dict(emp, comp_hra=40000), on, 30000)                # components above the rate: ignored
    assert bad["basic_da"] == 30000


# ---------------------------------------------------------------- multiple in/out per employee; gate passes

def test_multiple_in_out_sums_pairs():
    punches = [t(9), t(10), t(10, 30), t(12), t(12, 30), t(17, 30)]
    one = engine.evaluate_day(D, punches, SHIFT8H, S, today=TODAY)                    # default: first in -> last out
    assert one["worked_min"] == 480
    multi_sessions = engine.pair_sessions(punches, SHIFT8H, dict(S, break_pair_max_h=engine.MULTI_GAP_MAX_H))
    m = engine.evaluate_day(D, None, SHIFT8H, S, sessions=multi_sessions, today=TODAY, multi=True)
    assert m["worked_min"] == 60 + 90 + 300 and len(m["pairs"]) == 3                 # gaps are breaks, lunch not taken twice
    odd = engine.evaluate_day(D, None, SHIFT8H, S, today=TODAY, multi=True,
                              sessions=engine.pair_sessions([t(9), t(10), t(10, 30), t(17, 30), t(17, 40)], SHIFT8H,
                                                            dict(S, break_pair_max_h=10)))
    assert "odd in/out punches" in odd["flags"] or "unpaired punch" in odd["flags"]


def test_gate_pass_personal_and_official():
    day = [t(9), t(17, 30)]
    gp = lambda **k: dict({"kind": "personal", "in_time": None}, **k)              # noqa: E731
    r = engine.evaluate_day(D, day, SHIFT8H, S, today=TODAY, gate_passes=[gp(out_time="11:00", in_time="12:00")])
    assert r["gate_personal_min"] == 60 and r["worked_min"] == 420 and r["status"] == "P" and r["early_short_min"] == 60
    r = engine.evaluate_day(D, day, SHIFT8H, S, today=TODAY, gate_passes=[gp(out_time="12:45", in_time="13:45")])
    assert r["gate_personal_min"] == 30 and r["worked_min"] == 450                  # lunch half-hour not deducted twice
    r = engine.evaluate_day(D, [t(9), t(14)], SHIFT8H, S, today=TODAY,
                            gate_passes=[gp(kind="official", out_time="14:00")])      # left on duty, no return punch
    assert r["gate_official_min"] == 210 and r["worked_min"] == 270 + 210 and r["status"] == "P"
    r = engine.evaluate_day(D, [t(9), t(14)], SHIFT8H, S, today=TODAY,
                            gate_passes=[gp(out_time="14:00")])                       # personal, punched out: nothing extra
    assert r.get("gate_personal_min", 0) == 0 and r["worked_min"] == 270



def test_lone_punch_on_weekly_off_is_not_sandwiched_and_is_flagged():
    sat, sun, mon = dt.date(2026, 10, 24), dt.date(2026, 10, 25), dt.date(2026, 10, 26)   # 25 Oct 2026 = Sunday
    days = [engine.evaluate_day(sat, [], SHIFT, S, today=TODAY),
            engine.evaluate_day(sun, [t(9, 9, sun)], SHIFT, S, weekly_off=True, today=TODAY),
            engine.evaluate_day(mon, [], SHIFT, S, today=TODAY)]
    s = engine.summarize(days, SHIFT, S, "monthly")
    assert days[1]["status"] == "WO" and days[1]["mp_on_off"] and "sandwich" not in " ".join(days[1]["flags"])
    assert s["payable_days"] == 1 and s.get("cnt_MP", 0) == 0 and s["cnt_WO"] == 1 and s["missing_punch"] == 1


def test_default_late_penalty_is_flat_30():
    assert engine.suggest_penalty(5, S) == 30 and engine.suggest_penalty(55, S) == 30



def test_shift_list_shows_where_each_change_applies():
    from payroll.app import shift_effective
    rows = [{"id": 1, "from_date": "2026-10-03", "to_date": "2026-10-28"},
            {"id": 2, "from_date": "2026-10-04", "to_date": "2026-11-11"},
            {"id": 3, "from_date": "2026-10-12", "to_date": None},
            {"id": 4, "from_date": "2026-10-01", "to_date": "2026-10-31"}]
    r = {x["id"]: x for x in shift_effective([dict(x) for x in rows])}
    assert r[1]["in_effect"] == [] and [n["id"] for n in r[1]["replaced_by"]] == [4]      # fully hidden
    assert r[2]["in_effect"] == [] and {n["id"] for n in r[2]["replaced_by"]} == {3, 4}
    assert r[3]["in_effect"] == [("2026-11-01", None)]
    assert r[4]["in_effect"] == [("2026-10-01", "2026-10-31")] and r[4]["replaced_by"] == []


def test_night_duty_standard_8h_rest_is_ot():
    night = dict(SHIFT, name="Night", start_time="21:00", end_time="09:00", break_start=None, full_day_min=360,
                 day_start_hour=12, std_min=480)
    r = engine.evaluate_day(D, [t(21, 3), t(9, 4, D + dt.timedelta(days=1))], night, S, today=TODAY)
    assert r["status"] == "P" and r["worked_min"] == 691 and r["ot_min"] == 210 and not r.get("early_short_min")
    # without the setting the standard day is the whole 11h30 shift: no OT
    plain = dict(night, std_min=None)
    assert engine.evaluate_day(D, [t(21, 3), t(9, 4, D + dt.timedelta(days=1))], plain, S, today=TODAY)["ot_min"] == 0


def test_enable_std_hours_adds_column_and_sets_night(tmp_path):
    import sqlite3
    from payroll import db as dbm
    conn = sqlite3.connect(tmp_path / "x.db"); conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE shifts (id INTEGER PRIMARY KEY, name TEXT, start_time TEXT, end_time TEXT)")
    conn.execute("INSERT INTO shifts(name, start_time, end_time) VALUES ('General', '09:00', '17:30'), ('Night', '21:00', '09:00')")
    assert not dbm.has_column(conn, "shifts", "std_min")
    dbm.enable_std_hours(conn)
    assert dict(conn.execute("SELECT name, std_min FROM shifts").fetchall()) == {"General": None, "Night": 480}
    dbm.enable_std_hours(conn)   # running again changes nothing
    assert dict(conn.execute("SELECT name, std_min FROM shifts").fetchall()) == {"General": None, "Night": 480}
