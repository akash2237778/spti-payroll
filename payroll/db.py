"""SQLite storage: schema, connection helpers, settings."""
import os
import sqlite3
import json
from contextlib import contextmanager

DB_PATH = os.environ.get("PAYROLL_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "payroll.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS app_users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin', created_at TEXT DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, ip TEXT NOT NULL, port INTEGER DEFAULT 4370,
    password INTEGER DEFAULT 0, active INTEGER DEFAULT 1, serial TEXT, model TEXT,
    last_sync TEXT, last_status TEXT);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    break_min INTEGER DEFAULT 0,          -- unpaid break deducted from worked time
    break_after_min INTEGER DEFAULT 300,  -- only deduct break if worked span exceeds this (when no break_start)
    break_start TEXT,                     -- fixed lunch window start (e.g. 13:00); break deducted only if worked through it
    grace_in_min INTEGER DEFAULT 10, grace_out_min INTEGER DEFAULT 10,
    full_day_min INTEGER DEFAULT 480,     -- worked minutes needed for a full day
    half_day_min INTEGER DEFAULT 240,     -- worked minutes needed for a half day
    ot_after_min INTEGER DEFAULT 30,      -- minimum extra minutes before OT counts
    ot_round_min INTEGER DEFAULT 30,      -- OT rounded down to this block
    day_start_hour INTEGER DEFAULT 5);    -- punches before this hour belong to the previous work day

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY, device_user_id TEXT UNIQUE NOT NULL, emp_code TEXT, name TEXT NOT NULL,
    department TEXT DEFAULT '', designation TEXT DEFAULT '', phone TEXT DEFAULT '',
    join_date TEXT, exit_date TEXT, active INTEGER DEFAULT 1,
    shift_id INTEGER REFERENCES shifts(id),
    salary_type TEXT DEFAULT 'monthly',    -- monthly | daily | hourly
    salary_rate REAL DEFAULT 0,
    ot_enabled INTEGER DEFAULT 1, ot_multiplier REAL DEFAULT 1.0,
    category TEXT DEFAULT 'worker',        -- worker: OT paid | staff: no OT, salary by payable days only
    pay_mode TEXT DEFAULT 'bank',          -- bank | cash | fixed (bank_value to bank, rest cash) | percent
    bank_value REAL DEFAULT 0,
    paid_break INTEGER DEFAULT 0,          -- 1 = lunch break is paid time (not deducted)
    disabled_person INTEGER DEFAULT 0,     -- person with disability: ESI ceiling 25,000
    comp_hra REAL DEFAULT 0, comp_conveyance REAL DEFAULT 0, comp_other REAL DEFAULT 0,  -- parts of salary_rate
    weekly_off TEXT DEFAULT '6',           -- python weekday numbers, Mon=0 .. Sun=6
    pf_enabled INTEGER DEFAULT 0, esi_enabled INTEGER DEFAULT 0,
    bank_name TEXT DEFAULT '', bank_account TEXT DEFAULT '', ifsc TEXT DEFAULT '',
    uan TEXT DEFAULT '', esi_no TEXT DEFAULT '', notes TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS employee_shifts (   -- dated shift roster; latest-created row wins on overlap
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    shift_id INTEGER NOT NULL REFERENCES shifts(id), from_date TEXT NOT NULL, to_date TEXT,
    reason TEXT DEFAULT '', created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_empshift ON employee_shifts(employee_id, from_date);

CREATE TABLE IF NOT EXISTS punches (
    id INTEGER PRIMARY KEY, device_user_id TEXT NOT NULL, ts TEXT NOT NULL,
    status INTEGER, punch INTEGER, device_id INTEGER, source TEXT DEFAULT 'device',
    deleted INTEGER DEFAULT 0, note TEXT DEFAULT '',
    role TEXT,                         -- NULL = auto, 'in' / 'out' = confirmed by a person, 'ignore'
    UNIQUE(device_user_id, ts));
CREATE INDEX IF NOT EXISTS ix_punch_user_ts ON punches(device_user_id, ts);

CREATE TABLE IF NOT EXISTS day_overrides (
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
    work_date TEXT NOT NULL, status TEXT, in_time TEXT, out_time TEXT, reason TEXT DEFAULT '',
    created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(employee_id, work_date));

CREATE TABLE IF NOT EXISTS pay_splits (   -- this month's bank amount set by hand (rest of net pay in cash)
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    month TEXT NOT NULL, bank_amount REAL NOT NULL, note TEXT DEFAULT '', created_by TEXT, created_at TEXT,
    UNIQUE(employee_id, month));

CREATE TABLE IF NOT EXISTS cash_payments (  -- cash actually handed over (may differ from the calculated cash)
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    month TEXT NOT NULL, paid_amount REAL NOT NULL, note TEXT DEFAULT '', carried_month TEXT,
    created_by TEXT, created_at TEXT, UNIQUE(employee_id, month));

CREATE TABLE IF NOT EXISTS advances (      -- money advanced to an employee; recovered through 'advance' adjustments
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    given_date TEXT NOT NULL, amount REAL NOT NULL, installment REAL, note TEXT DEFAULT '',
    created_by TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS month_overrides (   -- final totals edited by a person (calculated values kept alongside)
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    month TEXT NOT NULL, payable_days REAL, ot_min INTEGER, calc_payable_days REAL, calc_ot_min INTEGER,
    reason TEXT DEFAULT '', created_by TEXT, created_at TEXT, UNIQUE(employee_id, month));

CREATE TABLE IF NOT EXISTS late_penalties (    -- chosen day by day on the employee sheet
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date TEXT NOT NULL, minutes INTEGER NOT NULL, note TEXT DEFAULT '', created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(employee_id, work_date));

CREATE TABLE IF NOT EXISTS leaves (
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
    leave_date TEXT NOT NULL, leave_type TEXT DEFAULT 'CL', paid INTEGER DEFAULT 1,
    half_day INTEGER DEFAULT 0, reason TEXT DEFAULT '',
    UNIQUE(employee_id, leave_date));

CREATE TABLE IF NOT EXISTS holidays (
    id INTEGER PRIMARY KEY, hdate TEXT UNIQUE NOT NULL, name TEXT NOT NULL, paid INTEGER DEFAULT 1);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
    month TEXT NOT NULL, kind TEXT NOT NULL,   -- allowance | bonus | deduction | advance | tds
    amount REAL NOT NULL, note TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS payroll_runs (
    id INTEGER PRIMARY KEY, month TEXT UNIQUE NOT NULL, status TEXT DEFAULT 'draft',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, finalized_at TEXT, finalized_by TEXT);

CREATE TABLE IF NOT EXISTS payroll_items (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    gross REAL, total_deductions REAL, net REAL, details TEXT,
    UNIQUE(run_id, employee_id));

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, username TEXT, action TEXT, detail TEXT);
"""

DEFAULT_SETTINGS = {
    "company_name": "SPTI",
    "company_address": "",
    "currency": "₹",
    "month_days_mode": "calendar",      # calendar | fixed  (divisor for monthly salary)
    "fixed_month_days": "30",
    "weekly_off_paid_monthly": "1",     # monthly staff get paid weekly offs
    "weekly_off_paid_daily": "0",       # daily wagers get paid weekly offs
    "sandwich_rule": "1",               # weekly off/holiday between two absences is not paid (company rule)
    "sandwich_paid_leave": "0",         # 1 = paid leave on both sides also makes the weekly off unpaid
    "wo_work_mode": "ot",               # ot | extra_day : work on week-off/holiday paid as overtime hours (company rule)
    "missing_punch_policy": "half",     # absent | half | full : single-punch days until corrected
    "late_marks_per_halfday": "0",      # e.g. 3 => every 3 late marks deduct 0.5 day (0 = off)
    "calc_mode": "first_last",          # first_last | pairs
    "dedup_seconds": "120",
    "pf_rate": "12", "pf_wage_ceiling": "15000",
    "esi_rate": "0.75", "esi_gross_ceiling": "21000",
    # ESIC (Code on Social Security 2020, wages u/s 2(88), ESIC circular 11.12.2025):
    "esi_mode": "all",                  # all = every employee whose ESI wages are within the ceiling | ticked = only ticked
    "esi_employer_rate": "3.25",
    "esi_disabled_ceiling": "25000",    # ceiling for a person with disability
    "esi_low_wage_daily": "176",        # average daily wage at or below this: no employee share (employer still pays)
    "salary_components": "0",           # 1 = salary can be split into Basic+DA / HRA / Conveyance / Other allowance
    "auto_sync_minutes": "30",
    "max_duty_hours": "26",             # longest continuous duty that may be paired as one session
    "long_duty_flag_hours": "16",       # sessions at least this long are flagged for a quick review
    "long_duty_mode": "ot",             # ot = 1 day + overtime | double = 2 days (+OT beyond 2 shifts)
    # Late penalty is chosen day by day on each employee's sheet; these only pre-fill the suggested minutes.
    "late_penalty_per_mark_min": "0",   # fixed OT minutes suggested per late day
    "late_penalty_factor": "1",         # plus this multiple of the minutes late
    # Company rule, by hours worked: full-day hours..standard day -> full day, shortfall taken off OT;
    # half-day hours..full-day hours -> half day, time beyond the half day paid as OT. "0" = off.
    "short_day_rule": "1",
}


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with tx() as c:
        c.executescript(SCHEMA)
        for table, col, ddl in (("punches", "role", "TEXT"), ("shifts", "break_start", "TEXT"),
                                ("employees", "paid_break", "INTEGER DEFAULT 0"),
                                # who/when a punch was added or re-roled by a person (machine punches stay NULL)
                                ("punches", "changed_by", "TEXT"), ("punches", "changed_at", "TEXT"),
                                ("leaves", "created_by", "TEXT"), ("leaves", "created_at", "TEXT"),
                                ("employees", "category", "TEXT DEFAULT 'worker'"),
                                ("employees", "pay_mode", "TEXT DEFAULT 'bank'"), ("employees", "bank_value", "REAL DEFAULT 0"),
                                ("employees", "disabled_person", "INTEGER DEFAULT 0"),
                                ("employees", "comp_hra", "REAL DEFAULT 0"), ("employees", "comp_conveyance", "REAL DEFAULT 0"),
                                ("employees", "comp_other", "REAL DEFAULT 0")):
            if col not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        c.execute("DELETE FROM settings WHERE key = 'late_penalty_mode'")  # replaced by day-by-day choice
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if not c.execute("SELECT 1 FROM shifts").fetchone():
            # Defaults derived from the real punch data: median in 09:02, median out ~20:20.
            # 8 working hours: 09:00-17:30 with unpaid lunch 13:00-13:30; work beyond that is overtime.
            # Full day from 6 h worked; 6-8 h shortfall is taken off overtime (short_day_rule).
            c.execute("""INSERT INTO shifts(name, start_time, end_time, break_min, break_after_min, break_start, grace_in_min,
                         grace_out_min, full_day_min, half_day_min, ot_after_min, ot_round_min, day_start_hour)
                         VALUES ('General', '09:00', '17:30', 30, 300, '13:00', 15, 15, 360, 240, 30, 30, 5)""")
        if not c.execute("SELECT 1 FROM settings WHERE key = 'migrated_sandwich_on'").fetchone():
            # Company rule (owner, 2026-11-02): Sunday is not paid when Saturday and Monday are taken off.
            c.execute("UPDATE settings SET value='1' WHERE key='sandwich_rule'")
            c.execute("INSERT INTO settings(key, value) VALUES ('migrated_sandwich_on', '1')")
        if not c.execute("SELECT 1 FROM settings WHERE key = 'migrated_hours_rule'").fetchone():
            # Owner, 2026-11-06: < 6 h worked = half day (rest as OT); 6-8 h = full day (shortfall from OT).
            # Replaces the "left after 14:00" rule. Only an untouched General shift (full day 7h45) is moved.
            c.execute("DELETE FROM settings WHERE key = 'early_leave_full_day_after'")
            c.execute("UPDATE shifts SET full_day_min = 360 WHERE name = 'General' AND full_day_min = 465 AND half_day_min = 240")
            c.execute("INSERT INTO settings(key, value) VALUES ('migrated_hours_rule', '1')")
        if not c.execute("SELECT 1 FROM settings WHERE key = 'migrated_8h_day'").fetchone():
            # One-time move of an untouched old default General shift (09:00-20:30) to the 8-hour day.
            c.execute("""UPDATE shifts SET end_time='17:30', break_start='13:00', full_day_min=360
                         WHERE name='General' AND start_time='09:00' AND end_time='20:30' AND break_min=30
                           AND full_day_min=480 AND half_day_min=240""")
            c.execute("INSERT INTO settings(key, value) VALUES ('migrated_8h_day', '1')")
        if not c.execute("SELECT 1 FROM settings WHERE key = 'seeded_night_shift'").fetchone():
            if not c.execute("SELECT 1 FROM shifts WHERE name = 'Night'").fetchone():
                # Night duty seen on the real data: in ~21:00, out ~09:00 next morning.
                # Day starts at 12:00 so an unpaired morning punch counts for the previous night.
                c.execute("""INSERT INTO shifts(name, start_time, end_time, break_min, break_after_min, grace_in_min,
                             grace_out_min, full_day_min, half_day_min, ot_after_min, ot_round_min, day_start_hour)
                             VALUES ('Night', '21:00', '09:00', 30, 300, 15, 15, 480, 240, 30, 30, 12)""")
            c.execute("INSERT INTO settings(key, value) VALUES ('seeded_night_shift', '1')")
        if not c.execute("SELECT 1 FROM devices").fetchone():
            c.execute("INSERT INTO devices(name, ip, port) VALUES ('Main Gate', '192.168.2.56', 4370)")


def get_settings(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    finally:
        if own:
            conn.close()


def audit(conn, user, action, detail=""):
    if not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    conn.execute("INSERT INTO audit_log(username, action, detail) VALUES (?, ?, ?)", (user, action, detail))
