# SPTI Payroll

Web-based attendance and payroll for the eSSL/ZKTeco biometric machine (192.168.2.56:4370).

## Start

Double-click `start_payroll.bat` (or `python run.py`), then open http://localhost:8000.
Other PCs on the LAN use `http://<this-pc-ip>:8000`. On first open you create the admin account.

## Monthly workflow

1. **Devices → Download punches** (also runs automatically every 30 min).
2. **Employees → Bulk edit salaries**: set salary type (monthly / daily / hourly), rate, OT, PF/ESI, department.
3. **Holidays**: add the year's paid holidays.
4. **Exceptions**: fix missing punches / short days (add a punch, override in/out times, or force a status).
5. **Leaves** and **Advances & Allowances** for the month.
6. **Payroll** → check the register → **Finalize & lock** → Excel / bank sheet / payslips.

## How punches become shifts (minimal manual correction)

Punches are **not** split by calendar day. Each employee's punches are one continuous stream, paired into
shifts (in → out) by picking the most plausible pairing of the whole stream. The pairing weighs how close
the in/out times are to that employee's usual times, extra length beyond the standard shift, and a penalty
for leaving a punch unpaired. A shift may run past midnight, up to 26 h. It belongs to the day it **started**.

Example (real data, one employee): 1 Oct 08:56 → 2 Oct 08:30 is one 23 h duty on 1 Oct, 3 Oct 08:54 → 4 Oct 05:00
is one shift on 3 Oct, and 4 Oct 09:15 → 17:36 is Sunday work. No manual edits are needed.

Corrections are made on **punches**, not days. Pairing is recomputed every time, so one fix re-pairs every following day:
- **IN / OUT / ignore** buttons on any punch (day screen).
- **Correct this shift**: enter in and out date-times (out may be the next day).
- **Exceptions → Apply selected suggestions**: for each unpaired punch the system proposes the missing one from the
  employee's usual times; tick all and apply in one click.
- **Long duty** (16 h or more) is listed for a one-click *Confirm* or *Not long – split*.

## Quick edits, holidays, late penalty, reports

- **Employees list**: ✎ on any row edits name, department, shift, pay type, rate, OT and active in place.
- **Employee month view**: ✎ on any day corrects In/Out or forces a status in place; `+OUT 20:30` buttons apply the suggested missing punch in one click.
- **Paid holidays**: Holidays → *Add national holidays* (26 Jan, 15 Aug, 2 Oct), *Mark as paid holiday* on the daily screen, or *Add paid holiday* on the muster roll. Recalculates everyone instantly.
- **Late penalty** (Settings): each late mark removes *fixed minutes + factor × minutes late* from the month's overtime. OT never goes below 0; salary is never cut by it.
- **Attendance statements**: printable per employee (one page each), with every day and the salary calculation written out. From the employee month view, Muster roll or Payroll.
- **Excel with formulas**: one sheet per employee with the day table; worked minutes, OT, late penalty, payable days, basic, OT pay, PF, ESI and net pay are live Excel formulas. The Payroll sheet links to each employee sheet and the Rules sheet holds editable rates. The test suite recalculates the workbook and checks it matches the app to the rupee.

## How hours and pay are calculated

| Rule | Default (editable) |
|---|---|
| Worked time | last punch − first punch − 30 min break (break only if span > 5 h). The device's in/out flag is ignored because it is unreliable on this machine. |
| Day boundary | a shift belongs to the day it started; an unpaired punch before 05:00 counts for the previous day |
| Duplicate punches | ignored within 120 s |
| Shift "General" | 09:00–20:30, 11 h standard; late after 15 min grace |
| Full / half day | ≥ 8 h worked = P, ≥ 4 h = HD, less = A |
| Overtime | worked beyond 11 h, counted when ≥ 30 min, rounded down to 30 min blocks; paid at per-hour rate × OT multiplier |
| Weekly off | Sunday; paid for monthly staff, unpaid for daily wage |
| Work on weekly off / holiday | paid as an extra day (or all hours as OT; see Settings) |
| Missing punch | counts as half day until corrected |
| Monthly salary | rate ÷ days in month × payable days |
| PF / ESI | 12% of basic (capped at ₹15,000) / 0.75% of gross if gross ≤ ₹21,000, only for employees with PF/ESI ticked |

Payable days = full days + ½ half days + paid weekly offs + paid holidays + paid leave + extra days worked on offs − late-mark deductions (off by default).

## Tests

```
python -m pytest tests -s
```

`tests/test_engine.py` covers the calculation rules; `tests/test_integration_device.py` syncs the real machine into a throw-away database and exercises every page, export, correction, finalize/lock and the viewer role. When the machine is unreachable it replays real data captured from it (`tests/data/device_snapshot.json`).

## Data

Everything is stored in `data/payroll.db` (SQLite). Settings → *Download database backup* makes a copy.
