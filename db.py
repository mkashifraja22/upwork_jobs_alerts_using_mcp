"""
SQLite storage for Upwork Job Alerts.

Everything that must survive a restart lives in one database file next to the app
(never relative to the current working directory):

  meta      key/value: OAuth tokens + client registration, account, flags
  settings  key/value (JSON): keywords, filters, email + schedule — edited from the UI
  jobs      every job the app has seen, with its status (emailed / filtered / ...)
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Relative DB_PATH values are resolved against the app folder, not the shell's cwd —
# otherwise starting the app from another folder silently creates an empty DB (= logged out).
DB_PATH = os.path.abspath(os.path.join(BASE_DIR, os.getenv("DB_PATH", "jobs.db")))

# A job in one of these states is "done": never evaluated or emailed again.
FINAL_STATUSES = ("emailed", "seeded", "seen", "matched")
KEEP_JOB_DAYS = 30


def now_iso():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id       TEXT PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT '',
            url          TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL,
            reason       TEXT NOT NULL DEFAULT '',
            query        TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            found_at     TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            emailed_at   TEXT,
            data         TEXT NOT NULL DEFAULT '{}')""")
        con.execute("CREATE INDEX IF NOT EXISTS jobs_found ON jobs(found_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
        # one-time migration from the old "seen" table so old jobs are never re-sent
        has_seen = con.execute("SELECT 1 FROM sqlite_master WHERE name='seen'").fetchone()
        done = con.execute("SELECT 1 FROM meta WHERE k='seen_migrated'").fetchone()
        if has_seen and not done:
            con.execute("""INSERT OR IGNORE INTO jobs(job_id, status, found_at, updated_at)
                           SELECT job_id, 'seen', seen_at, seen_at FROM seen""")
            con.execute("INSERT INTO meta VALUES ('seen_migrated','1')")


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #
def meta_get(key, default=None):
    with conn() as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def meta_set(key, value):
    with conn() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))


def meta_del(*keys):
    with conn() as con:
        con.executemany("DELETE FROM meta WHERE k=?", [(k,) for k in keys])


# --------------------------------------------------------------------------- #
# settings (JSON values)
# --------------------------------------------------------------------------- #
def settings_all():
    with conn() as con:
        rows = con.execute("SELECT k, v FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["k"]] = json.loads(r["v"])
        except ValueError:
            pass
    return out


def settings_save(values):
    with conn() as con:
        con.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                        [(k, json.dumps(v)) for k, v in values.items()])


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def job_statuses(con, ids):
    ids = list(ids)
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in con.execute(f"SELECT job_id, status FROM jobs WHERE job_id IN ({q})", chunk):
            out[r["job_id"]] = r["status"]
    return out


def upsert_job(con, job, status, reason="", query=""):
    now = now_iso()
    con.execute(
        """INSERT INTO jobs(job_id, title, url, status, reason, query, published_at,
                            found_at, updated_at, emailed_at, data)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             title=excluded.title, url=excluded.url, status=excluded.status,
             reason=excluded.reason, updated_at=excluded.updated_at,
             emailed_at=COALESCE(excluded.emailed_at, jobs.emailed_at),
             data=excluded.data""",
        (job["id"], job.get("title") or "", job.get("url") or "", status, reason, query,
         job.get("published_date") or job.get("created_date"), now, now,
         now if status == "emailed" else None, json.dumps(job)),
    )


def jobs_list(status=None, limit=50, offset=0):
    where, args = "", []
    if status:
        where, args = "WHERE status=?", [status]
    with conn() as con:
        total = con.execute(f"SELECT COUNT(*) FROM jobs {where}", args).fetchone()[0]
        rows = con.execute(
            f"""SELECT job_id, title, url, status, reason, query, published_at, found_at, data
                FROM jobs {where}
                ORDER BY COALESCE(published_at, found_at) DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()
    return total, [dict(r) for r in rows]


def job_counts():
    with conn() as con:
        return {r["status"]: r["n"] for r in
                con.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


def prune_jobs():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_JOB_DAYS)).isoformat()
    with conn() as con:
        con.execute("DELETE FROM jobs WHERE found_at < ?", (cutoff,))


init_db()
