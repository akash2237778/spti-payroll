"""Biometric device (ZKTeco / eSSL, port 4370) communication."""
import datetime as dt
import threading
import time
import logging

from zk import ZK

from . import db

log = logging.getLogger(__name__)
_sync_lock = threading.Lock()


def _open(device, timeout=20):
    zk = ZK(device["ip"], port=int(device["port"] or 4370), timeout=timeout,
            password=int(device["password"] or 0), force_udp=False, ommit_ping=True)
    return zk.connect()


def test_connection(device):
    conn = _open(device, timeout=10)
    try:
        return {
            "firmware": conn.get_firmware_version(),
            "serial": conn.get_serialnumber(),
            "platform": conn.get_platform(),
            "device_time": str(conn.get_time()),
            "users": len(conn.get_users()),
        }
    finally:
        conn.disconnect()


def set_device_time(device, when=None):
    conn = _open(device)
    try:
        conn.set_time(when or dt.datetime.now())
    finally:
        conn.disconnect()


def sync_device(device_id, username="system"):
    """Pull users + attendance logs from one device. Idempotent: duplicates are ignored."""
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "A sync is already running"}
    try:
        with db.tx() as c:
            device = c.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if not device:
            return {"ok": False, "error": "Device not found"}
        started = time.time()
        try:
            conn = _open(device)
        except Exception as e:
            with db.tx() as c:
                c.execute("UPDATE devices SET last_status = ? WHERE id = ?", (f"FAILED: {e}", device_id))
            return {"ok": False, "error": f"Cannot connect to {device['ip']}:{device['port']} - {e}"}
        try:
            conn.disable_device()
            serial = conn.get_serialnumber()
            users = conn.get_users()
            records = conn.get_attendance()
        finally:
            try:
                conn.enable_device()
            finally:
                conn.disconnect()

        new_punches = new_emps = 0
        with db.tx() as c:
            last_seen = {}
            for r in records:
                ts = r.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                cur = c.execute("INSERT OR IGNORE INTO punches(device_user_id, ts, status, punch, device_id) VALUES (?,?,?,?,?)",
                                (str(r.user_id), ts, r.status, r.punch, device_id))
                new_punches += cur.rowcount
                if ts > last_seen.get(str(r.user_id), ""):
                    last_seen[str(r.user_id)] = ts
            cutoff = (dt.datetime.now() - dt.timedelta(days=60)).strftime("%Y-%m-%d")
            default_shift = c.execute("SELECT id FROM shifts ORDER BY id LIMIT 1").fetchone()
            for u in users:
                uid = str(u.user_id)
                if u.privilege == 14 and not last_seen.get(uid):
                    continue  # device admin account with no punches (e.g. vendor login)
                exists = c.execute("SELECT id FROM employees WHERE device_user_id = ?", (uid,)).fetchone()
                if exists:
                    continue
                first = c.execute("SELECT MIN(ts) FROM punches WHERE device_user_id = ?", (uid,)).fetchone()[0]
                active = 1 if last_seen.get(uid, "") >= cutoff else 0
                c.execute("""INSERT INTO employees(device_user_id, emp_code, name, join_date, active, shift_id)
                             VALUES (?,?,?,?,?,?)""",
                          (uid, uid, (u.name or f"User {uid}").strip(), first[:10] if first else None, active,
                           default_shift["id"] if default_shift else None))
                new_emps += 1
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status = f"OK: {len(records)} logs on device, {new_punches} new, {new_emps} new employees ({time.time() - started:.1f}s)"
            c.execute("UPDATE devices SET last_sync = ?, last_status = ?, serial = ? WHERE id = ?",
                      (now, status, serial, device_id))
            db.audit(c, username, "device_sync", status)
        return {"ok": True, "device_logs": len(records), "new_punches": new_punches,
                "new_employees": new_emps, "users": len(users), "message": status}
    finally:
        _sync_lock.release()


def sync_all(username="system"):
    with db.tx() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM devices WHERE active = 1")]
    return [sync_device(i, username) for i in ids]


def start_auto_sync():
    """Background thread pulling logs every N minutes (settings.auto_sync_minutes, 0 = off)."""
    def loop():
        time.sleep(15)
        while True:
            minutes = 0
            try:
                minutes = int(db.get_settings().get("auto_sync_minutes") or 0)
                if minutes > 0:
                    for res in sync_all("auto-sync"):
                        log.info("auto-sync: %s", res.get("message") or res.get("error"))
            except Exception:
                log.exception("auto-sync failed")
            time.sleep(max(minutes, 5) * 60)
    t = threading.Thread(target=loop, daemon=True, name="auto-sync")
    t.start()
    return t
