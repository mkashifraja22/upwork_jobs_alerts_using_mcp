"""
Upwork Job Alerts — polls the Upwork MCP server and emails you every new job
that matches your tech stack (title, description, budget, client location,
client spend, payment verification, job link).

Run:  python app.py   then open http://localhost:5050 and click "Connect Upwork".
No API token needed — you sign in to Upwork in your browser (OAuth), and the
app stores + auto-refreshes the session in jobs.db.
"""
import asyncio
import html
import json
import logging
import os
import re
import smtplib
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit, urlunsplit

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template_string, request
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("upwork-alerts")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


MCP_URL = os.getenv("UPWORK_MCP_URL", "https://mcp.upwork.com/mcp")
ORG_UID = os.getenv("UPWORK_ORG_UID", "")  # optional; auto-detected (Freelancer account)

PORT = int(os.getenv("PORT", "5050"))  # not 5000: macOS AirPlay uses it
# Where your browser reaches this app; Upwork redirects back to PUBLIC_URL/oauth/callback
PUBLIC_URL = os.getenv("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
REDIRECT_URI = f"{PUBLIC_URL}/oauth/callback"

SEARCHES = [s.strip() for s in os.getenv(
    "JOB_SEARCHES",
    "Django,FastAPI,Python backend,REST API,HubSpot,Salesforce integration,Zoho,RAG,LangChain",
).split(",") if s.strip()]

# Account safety: be a light, human-like API user. Never poll faster than MIN_POLL_SECONDS.
MIN_POLL_SECONDS = 300
POLL_SECONDS = max(int(os.getenv("POLL_SECONDS", "600")), MIN_POLL_SECONDS)
SEARCH_DELAY_SECONDS = max(float(os.getenv("SEARCH_DELAY_SECONDS", "3")), 1.0)  # pause between searches
HOST = os.getenv("HOST", "127.0.0.1")  # dashboard only reachable from this computer
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))
VERIFIED_ONLY = _bool("VERIFIED_PAYMENT_ONLY", False)
MIN_CLIENT_SPEND = float(os.getenv("MIN_CLIENT_SPEND", "0"))
EMAIL_ON_FIRST_RUN = _bool("EMAIL_ON_FIRST_RUN", False)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", "") or SMTP_USER
MAIL_TO = os.getenv("MAIL_TO", "") or SMTP_USER

DB_PATH = os.getenv("DB_PATH", "jobs.db")

app = Flask(__name__)
_poll_lock = threading.Lock()
STATE = {"last_poll": None, "last_error": None, "last_found": 0, "emails_sent": 0,
         "paused_until": None}
_org_cache = {"uid": ORG_UID}
_backoff = {"until": 0.0, "seconds": 0}


class RateLimited(Exception):
    pass


def is_rate_limit(text):
    t = str(text).lower()
    return "429" in t or "rate limit" in t or "too many requests" in t or "throttl" in t


# --------------------------------------------------------------------------- #
# Seen-jobs store (SQLite)
# --------------------------------------------------------------------------- #
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (job_id TEXT PRIMARY KEY, title TEXT, seen_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


def meta_get(key):
    with db() as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def meta_del(*keys):
    with db() as con:
        con.executemany("DELETE FROM meta WHERE k=?", [(k,) for k in keys])


def is_seen(con, job_id):
    return con.execute("SELECT 1 FROM seen WHERE job_id=?", (job_id,)).fetchone() is not None


def mark_seen(con, job):
    # Only the job ID is kept (for de-duplication) — no job content is stored locally.
    con.execute(
        "INSERT OR IGNORE INTO seen VALUES (?,?,?)",
        (job["id"], "", datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def prune_seen(con):
    # Jobs older than MAX_AGE_HOURS are filtered out anyway, so their IDs aren't needed.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS + 6)
    con.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff.isoformat(),))
    con.execute("UPDATE seen SET title='' WHERE title<>''")  # scrub titles stored by older versions
    con.commit()


# --------------------------------------------------------------------------- #
# Upwork OAuth ("Connect with Upwork" in the browser — no manual token)
# --------------------------------------------------------------------------- #
class NotConnected(Exception):
    pass


class DBTokenStorage:
    """Persists OAuth tokens + the dynamically-registered client in jobs.db."""

    async def get_tokens(self):
        raw = meta_get("oauth_tokens")
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens):
        meta_set("oauth_tokens", tokens.model_dump_json())
        meta_set("oauth_expires_at", str(time.time() + tokens.expires_in) if tokens.expires_in else "")

    async def get_client_info(self):
        raw = meta_get("oauth_client")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, info):
        meta_set("oauth_client", info.model_dump_json())


class UpworkOAuth(OAuthClientProvider):
    async def _initialize(self):
        await super()._initialize()
        # restore expiry so stored tokens get refreshed (instead of 401 -> re-login)
        exp = meta_get("oauth_expires_at")
        if self.context.current_tokens and exp:
            self.context.token_expiry_time = float(exp)


def make_auth(redirect_handler, callback_handler):
    return UpworkOAuth(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Upwork Job Alerts",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=DBTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


async def _background_redirect(_url):
    # Scheduled polls can't open a browser: session is gone, user must reconnect.
    meta_del("oauth_tokens", "oauth_expires_at")
    raise NotConnected("Upwork session expired — click Connect again")


async def _background_callback():
    raise NotConnected("Upwork session expired — click Connect again")


def is_connected():
    return meta_get("oauth_tokens") is not None


def err_text(exc):
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return str(exc) or type(exc).__name__


# Shared state between the /connect request, the OAuth thread and /oauth/callback
AUTH = {"lock": threading.Lock(), "running": False, "url": None, "code": None, "state": None,
        "error": None, "url_ready": threading.Event(), "callback": threading.Event(),
        "done": threading.Event()}


async def _connect():
    async def on_redirect(url):
        AUTH["url"] = url
        AUTH["url_ready"].set()

    async def on_callback():
        if not await asyncio.to_thread(AUTH["callback"].wait, 300):
            raise TimeoutError("Timed out waiting for Upwork sign-in")
        if not AUTH["code"]:
            raise RuntimeError(AUTH["error"] or "Upwork sign-in was cancelled")
        return AUTH["code"], AUTH["state"]

    async with mcp_session(make_auth(on_redirect, on_callback)) as (session, names):
        await detect_account(session, names, force=True)


def _connect_thread():
    try:
        asyncio.run(_connect())
        AUTH["error"] = None
        STATE["last_error"] = None
        log.info("Connected to Upwork")
        threading.Thread(target=poll, daemon=True).start()
    except Exception as exc:
        AUTH["error"] = err_text(exc)
        log.error("Upwork connect failed: %s", AUTH["error"])
    finally:
        AUTH["running"] = False
        AUTH["done"].set()


def start_connect():
    with AUTH["lock"]:
        if AUTH["running"]:
            return
        AUTH.update(running=True, url=None, code=None, state=None, error=None)
        for ev in ("url_ready", "callback", "done"):
            AUTH[ev].clear()
        threading.Thread(target=_connect_thread, daemon=True).start()


# --------------------------------------------------------------------------- #
# Upwork MCP
# --------------------------------------------------------------------------- #
async def _call(session, tool_name, args):
    res = await session.call_tool(tool_name, args)
    text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    if res.isError:
        raise RuntimeError(f"{tool_name} failed: {text[:300]}")
    return json.loads(text)


@asynccontextmanager
async def mcp_session(auth):
    async with streamablehttp_client(MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            yield session, names


async def detect_account(session, names, force=False):
    if _org_cache["uid"] and not force:
        return
    list_accounts = next(n for n in names if n.endswith("list_accounts"))
    accounts = (await _call(session, list_accounts, {}))["accounts"]
    talent = next((a for a in accounts if a.get("role") == "TALENT"), None)
    if not talent:
        raise RuntimeError("No Freelancer (talent) account found on this Upwork login")
    _org_cache["uid"] = ORG_UID or talent["org_uid"]
    meta_set("account_name", talent.get("name") or "")
    log.info("Using freelancer account %s", talent.get("name"))


async def fetch_jobs():
    """Run every search in one MCP session; return {job_id: job}."""
    jobs = {}
    auth = make_auth(_background_redirect, _background_callback)
    async with mcp_session(auth) as (session, names):
        find_jobs = next(n for n in names if n.endswith("find_jobs"))
        await detect_account(session, names)

        for i, q in enumerate(SEARCHES):
            if i:
                await asyncio.sleep(SEARCH_DELAY_SECONDS)  # spread requests out
            params = {"query": q, "sort": "recency", "limit": 10, "include_full_details": True}
            if VERIFIED_ONLY:
                params["verified_payment_only"] = True
            try:
                data = await _call(session, find_jobs, {
                    "action": "search", "org_uid": _org_cache["uid"], "params": params,
                })
                for j in data.get("jobs", []):
                    jobs.setdefault(j["id"], j)
            except Exception as exc:  # one bad query shouldn't kill the cycle...
                if is_rate_limit(err_text(exc)):  # ...but a rate limit stops everything immediately
                    raise RateLimited(err_text(exc)) from exc
                log.warning("Search %r failed: %s", q, exc)
    return jobs


# --------------------------------------------------------------------------- #
# Filtering + formatting
# --------------------------------------------------------------------------- #
def money(value):
    try:
        return float(re.sub(r"[^\d.]", "", str(value or "")) or 0)
    except ValueError:
        return 0.0


def parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def passes_filters(job):
    client = job.get("client") or {}
    if VERIFIED_ONLY and client.get("verification_status") != "VERIFIED":
        return False
    if money(client.get("total_spent")) < MIN_CLIENT_SPEND:
        return False
    pub = parse_dt(job.get("published_date") or job.get("created_date"))
    if pub and datetime.now(timezone.utc) - pub > timedelta(hours=MAX_AGE_HOURS):
        return False
    return True


def clean_description(job):
    text = job.get("description") or job.get("description_snippet") or ""
    text = re.sub(r"</?untrusted_participant_content>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= 1800 else text[:1800].rstrip() + "…"


def clean_url(url):
    p = urlsplit(url or "")
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))  # drop utm tracking


def fmt_budget(job):
    raw = str(job.get("budget") or "").strip()
    kind = "Fixed" if job.get("job_type") == "fixed" else "Hourly"
    if not raw:
        return f"Not specified ({kind})"
    if re.fullmatch(r"[\d,.]+", raw):
        raw = f"${float(raw.replace(',', '')):,.0f}"
    return f"{raw} ({kind})"


def job_fields(job):
    c = job.get("client") or {}
    verified = c.get("verification_status") == "VERIFIED"
    rating = f"{c['rating']} ({c.get('total_reviews', 0)} reviews)" if c.get("rating") else "No reviews yet"
    return [
        ("Budget", fmt_budget(job)),
        ("Client location", c.get("country") or "Unknown"),
        ("Client spent", c.get("total_spent") or "$0"),
        ("Payment method", "✅ Verified" if verified else "❌ Not verified"),
        ("Client rating", rating),
        ("Proposals", job.get("proposals_tier") or "n/a"),
        ("Experience", (job.get("experience_level") or "n/a").replace("_", " ").title()),
        ("Skills", ", ".join(job.get("skills") or []) or "n/a"),
    ]


def render_email(job):
    title = job.get("title", "(no title)")
    url = clean_url(job.get("url"))
    desc = clean_description(job)
    fields = job_fields(job)

    text = f"{title}\n\n" + "\n".join(f"{k}: {v}" for k, v in fields)
    text += f"\n\n{desc}\n\nApply: {url}\n"

    e = html.escape
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#6b7280;white-space:nowrap">{e(k)}</td>'
        f'<td style="padding:6px 0;font-weight:600">{e(str(v))}</td></tr>'
        for k, v in fields
    )
    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;margin:auto;color:#111827">
  <h2 style="margin:0 0 12px">{e(title)}</h2>
  <table style="border-collapse:collapse;font-size:14px">{rows}</table>
  <div style="margin:16px 0;padding:12px;background:#f9fafb;border-radius:8px;font-size:14px;line-height:1.5;white-space:pre-wrap">{e(desc)}</div>
  <a href="{e(url, quote=True)}" style="display:inline-block;background:#14a800;color:#fff;text-decoration:none;
     padding:10px 18px;border-radius:6px;font-weight:600">Open job on Upwork</a>
</div>"""
    subject = f"🔔 {title} — {fmt_budget(job)}"
    return subject, text, body


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(subject, text, html_body):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, MAIL_FROM, MAIL_TO
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


# --------------------------------------------------------------------------- #
# Poll cycle
# --------------------------------------------------------------------------- #
MIN_GAP_SECONDS = 120  # even manual "Check now" clicks can't hit Upwork more often than this
_last_poll_start = {"t": 0.0}


def poll():
    if not is_connected():
        return {"skipped": "Upwork not connected — open the dashboard and click Connect"}
    now = time.time()
    if now < _backoff["until"]:
        return {"skipped": f"Paused after an Upwork rate-limit — resumes in {int(_backoff['until'] - now) // 60 + 1} min"}
    if now - _last_poll_start["t"] < MIN_GAP_SECONDS:
        return {"skipped": f"Checked recently — try again in {int(MIN_GAP_SECONDS - (now - _last_poll_start['t']))}s"}
    if not _poll_lock.acquire(blocking=False):
        return {"skipped": "previous poll still running"}
    _last_poll_start["t"] = now
    try:
        jobs = asyncio.run(fetch_jobs())
        _backoff.update(until=0.0, seconds=0)
        STATE["paused_until"] = None
        STATE["last_found"] = len(jobs)
        con = db()
        prune_seen(con)
        first_run = con.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone() is None

        if first_run and not EMAIL_ON_FIRST_RUN:
            for j in jobs.values():
                mark_seen(con, j)
            con.execute("INSERT INTO meta VALUES ('seeded','1')")
            con.commit()
            log.info("First run: marked %d existing jobs as seen (no emails).", len(jobs))
            STATE.update(last_poll=datetime.now(timezone.utc).isoformat(), last_error=None)
            return {"seeded": len(jobs)}

        fresh = [j for j in jobs.values() if not is_seen(con, j["id"]) and passes_filters(j)]
        fresh.sort(key=lambda j: j.get("published_date") or "")
        sent = 0
        for j in fresh:
            try:
                send_email(*render_email(j))
            except Exception as exc:  # leave unseen so it retries next cycle
                log.error("Email failed for %s: %s", j.get("title"), exc)
                STATE["last_error"] = f"email: {exc}"
                continue
            mark_seen(con, j)
            sent += 1
            log.info("Sent: %s", j.get("title"))

        if first_run:
            con.execute("INSERT OR IGNORE INTO meta VALUES ('seeded','1')")
            con.commit()
        STATE["emails_sent"] += sent
        STATE["last_poll"] = datetime.now(timezone.utc).isoformat()
        if sent or not STATE["last_error"] or not STATE["last_error"].startswith("email"):
            STATE["last_error"] = None
        return {"found": len(jobs), "new_emailed": sent}
    except Exception as exc:
        msg = err_text(exc)
        if isinstance(exc, RateLimited) or is_rate_limit(msg):
            # Back off hard: 15 min, then 30, 60, max 2 h. Never hammer Upwork.
            _backoff["seconds"] = min(max(_backoff["seconds"] * 2, 900), 7200)
            _backoff["until"] = time.time() + _backoff["seconds"]
            STATE["paused_until"] = datetime.fromtimestamp(_backoff["until"], timezone.utc).isoformat()
            msg = f"Upwork rate limit hit — pausing {_backoff['seconds'] // 60} min ({msg})"
        log.error("Poll failed: %s", msg)
        STATE["last_error"] = msg
        return {"error": msg}
    finally:
        _poll_lock.release()


# --------------------------------------------------------------------------- #
# Flask routes
# --------------------------------------------------------------------------- #
def status_data():
    return {**STATE, "connected": is_connected(), "account": meta_get("account_name"),
            "connecting": AUTH["running"], "connect_error": AUTH["error"],
            "searches": SEARCHES, "poll_seconds": POLL_SECONDS, "mail_to": MAIL_TO,
            "smtp_configured": bool(SMTP_USER and SMTP_PASS)}


@app.get("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML, s=status_data())


@app.get("/status")
def status():
    return jsonify(status_data())


@app.get("/connect")
def connect():
    start_connect()
    deadline = time.time() + 45
    while time.time() < deadline:
        if AUTH["url_ready"].is_set():
            return redirect(AUTH["url"])   # -> Upwork sign-in / consent page
        if AUTH["done"].is_set():
            break                          # already authorized (or failed)
        time.sleep(0.1)
    return redirect("/")


@app.get("/oauth/callback")
def oauth_callback():
    if request.args.get("error"):
        AUTH["error"] = request.args.get("error_description") or request.args["error"]
    AUTH["code"], AUTH["state"] = request.args.get("code"), request.args.get("state")
    AUTH["callback"].set()
    AUTH["done"].wait(45)  # let token exchange + account detection finish
    return redirect("/")


@app.post("/disconnect")
def disconnect():
    meta_del("oauth_tokens", "oauth_expires_at", "account_name")
    _org_cache["uid"] = ORG_UID
    return redirect("/")


@app.get("/poll")
def poll_now():
    return jsonify(poll())


@app.get("/test-email")
def test_email():
    sample = {
        "id": "test", "title": "Test job: Senior Django/FastAPI developer", "job_type": "fixed",
        "budget": "500.00", "url": "https://www.upwork.com/jobs/~0123456789?utm_source=x",
        "description": "This is a test alert from your Upwork Job Alerts app.",
        "client": {"country": "United States", "total_spent": "$12,400.00", "rating": 4.8,
                   "total_reviews": 14, "verification_status": "VERIFIED"},
        "proposals_tier": "5 to 10", "experience_level": "expert", "skills": ["Python", "Django"],
    }
    try:
        send_email(*render_email(sample))
        return jsonify({"ok": True, "sent_to": MAIL_TO})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upwork Job Alerts — Dashboard</title>
<meta name="description" content="Connect your Upwork account and get instant email alerts for new matching jobs.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0b0f0c;--card:rgba(255,255,255,.04);--line:rgba(255,255,255,.08);--txt:#e8f0ea;--mut:#8a9a8f;
 --g:#14a800;--g2:#3ddc84;--red:#ff6b6b;--amb:#f5b942}
*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:Inter,system-ui,sans-serif;color:var(--txt);
 background:radial-gradient(900px 500px at 10% -10%,rgba(20,168,0,.25),transparent 60%),
 radial-gradient(700px 400px at 110% 10%,rgba(61,220,132,.12),transparent 60%),var(--bg)}
main{max-width:880px;margin:0 auto;padding:48px 20px 64px}
header{display:flex;align-items:center;gap:14px;margin-bottom:32px}
.logo{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;font-weight:800;font-size:20px;
 background:linear-gradient(135deg,var(--g),var(--g2));box-shadow:0 8px 30px rgba(20,168,0,.4)}
h1{font-size:24px;margin:0;letter-spacing:-.02em}header p{margin:2px 0 0;color:var(--mut);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;backdrop-filter:blur(12px);
 margin-bottom:18px;animation:up .5s ease both}
@keyframes up{from{opacity:0;transform:translateY(8px)}}
.conn{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px;background:var(--red)}
.on .dot{background:var(--g2);box-shadow:0 0 0 0 rgba(61,220,132,.6);animation:pulse 2s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 10px rgba(61,220,132,0)}100%{box-shadow:0 0 0 0 rgba(61,220,132,0)}}
h2{font-size:18px;margin:0 0 4px}.mut{color:var(--mut);font-size:14px;margin:0}
.btn{appearance:none;border:0;cursor:pointer;font:600 15px Inter,sans-serif;color:#fff;text-decoration:none;
 padding:12px 22px;border-radius:12px;display:inline-flex;align-items:center;gap:8px;transition:transform .15s,box-shadow .2s,background .2s}
.btn:hover{transform:translateY(-1px)}.btn:active{transform:none}
.primary{background:linear-gradient(135deg,var(--g),#1fbf3a);box-shadow:0 8px 24px rgba(20,168,0,.35)}
.primary:hover{box-shadow:0 12px 32px rgba(20,168,0,.5)}
.ghost{background:rgba(255,255,255,.06);border:1px solid var(--line)}.ghost:hover{background:rgba(255,255,255,.1)}
.danger{background:transparent;color:var(--red);border:1px solid rgba(255,107,107,.3)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.stat b{display:block;font-size:26px;letter-spacing:-.02em;margin-top:6px}.stat span{color:var(--mut);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.chip{padding:6px 12px;border-radius:999px;font-size:13px;background:rgba(20,168,0,.12);border:1px solid rgba(20,168,0,.3);color:#b6f5c4}
.alert{padding:12px 16px;border-radius:12px;font-size:14px;margin-bottom:18px;border:1px solid}
.err{background:rgba(255,107,107,.08);border-color:rgba(255,107,107,.3);color:#ffb3b3}
.warn{background:rgba(245,185,66,.08);border-color:rgba(245,185,66,.3);color:#ffd98a}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);background:#16201a;
 border:1px solid var(--line);padding:12px 18px;border-radius:12px;font-size:14px;transition:transform .3s;max-width:90vw}
#toast.show{transform:translateX(-50%) translateY(0)}
</style></head><body><main>
<header><div class="logo">U</div><div><h1>Upwork Job Alerts</h1>
<p>New jobs matching your stack, straight to your inbox.</p></div></header>

{% if s.connect_error %}<div class="alert err">Connection failed: {{ s.connect_error }}</div>{% endif %}
{% if s.last_error %}<div class="alert err">Last error: {{ s.last_error }}</div>{% endif %}
{% if not s.smtp_configured %}<div class="alert warn">Email isn't set up yet — add SMTP_USER / SMTP_PASS / MAIL_TO to .env so alerts can be sent.</div>{% endif %}

<section class="card conn {{ 'on' if s.connected }}">
  <div>
    <h2><span class="dot"></span>{{ 'Connected to Upwork' if s.connected else 'Not connected' }}</h2>
    <p class="mut">{% if s.connected %}{{ s.account or 'Freelancer account' }} · checking every {{ s.poll_seconds }}s
    {% else %}Sign in with Upwork in your browser — no API keys or tokens needed.{% endif %}</p>
  </div>
  {% if s.connected %}
  <form method="post" action="/disconnect"><button id="disconnect-btn" class="btn danger">Disconnect</button></form>
  {% else %}
  <a id="connect-btn" class="btn primary" href="/connect">{{ 'Connecting…' if s.connecting else 'Connect Upwork' }} →</a>
  {% endif %}
</section>

<section class="grid">
  <div class="card stat"><span>Jobs found (last check)</span><b>{{ s.last_found }}</b></div>
  <div class="card stat"><span>Alerts emailed</span><b>{{ s.emails_sent }}</b></div>
  <div class="card stat"><span>Last check</span><b id="last-poll" data-ts="{{ s.last_poll or '' }}">—</b></div>
</section>

<section class="card">
  <h2>Watching</h2><p class="mut">Alerts go to {{ s.mail_to or '(MAIL_TO not set)' }}</p>
  <div class="chips">{% for q in s.searches %}<span class="chip">{{ q }}</span>{% endfor %}</div>
  <div class="row">
    <button id="poll-btn" class="btn ghost" data-url="/poll" {{ 'disabled' if not s.connected }}>Check now</button>
    <button id="test-email-btn" class="btn ghost" data-url="/test-email">Send test email</button>
  </div>
</section>
</main><div id="toast" role="status"></div>
<script>
const ts=document.getElementById('last-poll');
if(ts.dataset.ts){const d=new Date(ts.dataset.ts);ts.textContent=d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
const toast=m=>{const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),3500)};
document.querySelectorAll('[data-url]').forEach(b=>b.addEventListener('click',async()=>{
  const label=b.textContent;b.disabled=true;b.textContent='Working…';
  try{const r=await (await fetch(b.dataset.url)).json();
    toast(r.error?'Error: '+r.error:r.skipped?r.skipped:r.ok===false?'Error':
      r.sent_to?'Test email sent to '+r.sent_to:r.seeded!==undefined?'Saved '+r.seeded+' existing jobs':
      'Found '+r.found+' jobs, emailed '+r.new_emailed);
    if(b.id==='poll-btn')setTimeout(()=>location.reload(),1500);
  }catch(e){toast('Request failed')}finally{b.disabled=false;b.textContent=label}}));
{% if s.connecting %}setTimeout(()=>location.reload(),3000);{% endif %}
</script></body></html>"""


if __name__ == "__main__":
    if not (SMTP_USER and SMTP_PASS):
        log.warning("SMTP_USER / SMTP_PASS not set — alerts can't be emailed until you add them to .env")
    if int(os.getenv("POLL_SECONDS", "600")) < MIN_POLL_SECONDS:
        log.warning("POLL_SECONDS raised to %ss (safety minimum)", MIN_POLL_SECONDS)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(poll, "interval", seconds=POLL_SECONDS, max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc))
    scheduler.start()
    log.info("Polling %d searches every %ss (~%d Upwork requests/hour)",
             len(SEARCHES), POLL_SECONDS, len(SEARCHES) * 3600 // POLL_SECONDS)
    log.info("Open %s and click 'Connect Upwork'", PUBLIC_URL)
    app.run(host=HOST, port=PORT, use_reloader=False, threaded=True)
