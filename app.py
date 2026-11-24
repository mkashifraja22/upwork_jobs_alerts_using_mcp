"""
Upwork Job Alerts — polls the Upwork MCP server and emails you every new job that
matches your keywords and filters (title, description, budget, client location,
client spend, payment verification, job link).

Run:  python app.py   then open http://localhost:5050 and click "Connect Upwork".
Keywords, filters, email and schedule are all edited in the dashboard; everything
(including the Upwork login) is stored in jobs.db, so a restart never logs you out.
"""
import asyncio
import html
import json
import logging
import os
import re
import smtplib
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit, urlunsplit

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, redirect, render_template, request
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

import db
import settings as cfg
from db import meta_del, meta_get, meta_set

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("upwork-alerts")


# --------------------------------------------------------------------------- #
# Static config (server-level; everything else lives in the UI-editable settings)
# --------------------------------------------------------------------------- #
MCP_URL = os.getenv("UPWORK_MCP_URL", "https://mcp.upwork.com/mcp")
ORG_UID = os.getenv("UPWORK_ORG_UID", "")  # optional; auto-detected (Freelancer account)

PORT = int(os.getenv("PORT", "5050"))  # not 5000: macOS AirPlay uses it
# Where your browser reaches this app; Upwork redirects back to PUBLIC_URL/oauth/callback
PUBLIC_URL = os.getenv("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
REDIRECT_URI = f"{PUBLIC_URL}/oauth/callback"
HOST = os.getenv("HOST", "127.0.0.1")  # dashboard only reachable from this computer
EMAIL_ON_FIRST_RUN = os.getenv("EMAIL_ON_FIRST_RUN", "false").strip().lower() in ("1", "true", "yes", "on")

app = Flask(__name__)
_poll_lock = threading.Lock()
STATE = {"last_poll": meta_get("last_poll"), "last_error": None,
         "last_found": int(meta_get("last_found", 0) or 0), "paused_until": None,
         "polling": False, "last_result": None}
_org_cache = {"uid": ORG_UID or meta_get("org_uid") or ""}
_backoff = {"until": 0.0, "seconds": 0}
_auth_fail = {"n": 0}
scheduler = BackgroundScheduler(timezone="UTC")


class RateLimited(Exception):
    pass


def is_rate_limit(text):
    t = str(text).lower()
    return "429" in t or "rate limit" in t or "too many requests" in t or "throttl" in t


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
        # A refresh response often omits refresh_token (it is only rotated sometimes).
        # Never overwrite a good stored refresh token with nothing — that is what
        # used to log the app out after the first access-token expiry.
        if not tokens.refresh_token:
            old = await self.get_tokens()
            if old and old.refresh_token:
                tokens = tokens.model_copy(update={"refresh_token": old.refresh_token})
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


# Scheduled polls can't open a browser. If the SDK decides a full login is needed
# (refresh failed), we do NOT delete the stored tokens: the failure may be transient
# (network, Upwork 5xx) and the next poll will refresh fine. We only flag it so the
# dashboard offers "Reconnect" — and the flag clears itself as soon as a poll works.
async def _background_redirect(_url):
    raise NotConnected("Upwork session expired — click Reconnect")


async def _background_callback():
    raise NotConnected("Upwork session expired — click Reconnect")


def is_connected():
    return meta_get("oauth_tokens") is not None


def needs_reauth():
    return is_connected() and meta_get("needs_reauth") == "1"


def contains_exc(exc, cls, _depth=0):
    if isinstance(exc, cls):
        return True
    if _depth > 8:
        return False
    if isinstance(exc, BaseExceptionGroup):
        return any(contains_exc(e, cls, _depth + 1) for e in exc.exceptions)
    cause = exc.__cause__ or exc.__context__
    return bool(cause) and contains_exc(cause, cls, _depth + 1)


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
        _auth_fail["n"] = 0
        meta_del("needs_reauth", "reauth_notified")
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
    meta_set("org_uid", _org_cache["uid"])
    meta_set("account_name", talent.get("name") or "")
    log.info("Using freelancer account %s", talent.get("name"))


async def fetch_jobs(s):
    """Run every search in one MCP session; return {job_id: job}."""
    jobs = {}
    auth = make_auth(_background_redirect, _background_callback)
    async with mcp_session(auth) as (session, names):
        find_jobs = next(n for n in names if n.endswith("find_jobs"))
        await detect_account(session, names)

        for i, q in enumerate(s["searches"]):
            if i:
                await asyncio.sleep(s["search_delay_seconds"])  # spread requests out
            params = {"query": q, "sort": "recency", "limit": 10, "include_full_details": True}
            if s["verified_only"]:
                params["verified_payment_only"] = True
            try:
                data = await _call(session, find_jobs, {
                    "action": "search", "org_uid": _org_cache["uid"], "params": params,
                })
                for j in data.get("jobs", []):
                    if j["id"] not in jobs:
                        jobs[j["id"]] = {**j, "_query": q}
            except Exception as exc:  # one bad query shouldn't kill the cycle...
                if is_rate_limit(err_text(exc)):  # ...but a rate limit stops everything immediately
                    raise RateLimited(err_text(exc)) from exc
                if contains_exc(exc, NotConnected):
                    raise
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


def budget_amount(job):
    """Highest number in the budget text ('500.00', '$30.00-$60.00/hr'); None if unspecified."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", str(job.get("budget") or ""))]
    return max(nums) if nums else None


def parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def keyword_in(text, kw):
    """Whole-word, case-insensitive match ('ai' does not match 'maintain'; 'c++' works)."""
    return re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text, re.I) is not None


def evaluate(job, s):
    """Apply the user's filters. Returns (passes, reason-if-not)."""
    client = job.get("client") or {}
    if s["verified_only"] and client.get("verification_status") != "VERIFIED":
        return False, "Payment method not verified"

    spent = money(client.get("total_spent"))
    if spent < s["min_client_spend"]:
        return False, f"Client spent ${spent:,.0f} (min ${s['min_client_spend']:,.0f})"

    rating = client.get("rating")
    if s["min_client_rating"] and rating and money(rating) < s["min_client_rating"]:
        return False, f"Client rating {rating} (min {s['min_client_rating']:g})"

    pub = parse_dt(job.get("published_date") or job.get("created_date"))
    if pub and datetime.now(timezone.utc) - pub > timedelta(hours=s["max_age_hours"]):
        return False, f"Older than {s['max_age_hours']}h"

    kind = "fixed" if job.get("job_type") == "fixed" else "hourly"
    if s["job_type"] in ("fixed", "hourly") and kind != s["job_type"]:
        return False, f"{kind.title()} job (only {s['job_type']} wanted)"

    amount = budget_amount(job)
    if amount is not None:
        if kind == "fixed" and amount < s["min_fixed_budget"]:
            return False, f"Budget ${amount:,.0f} (min ${s['min_fixed_budget']:,.0f})"
        if kind == "hourly" and amount < s["min_hourly_rate"]:
            return False, f"Rate ${amount:,.0f}/hr (min ${s['min_hourly_rate']:,.0f})"

    level = (job.get("experience_level") or "").lower()
    if s["experience_levels"] and level and not any(lv in level for lv in s["experience_levels"]):
        return False, f"Experience level: {level.replace('_', ' ')}"

    country = (client.get("country") or "").strip().lower()
    if country:
        if s["countries_include"] and country not in [c.lower() for c in s["countries_include"]]:
            return False, f"Client country: {client.get('country')}"
        if country in [c.lower() for c in s["countries_exclude"]]:
            return False, f"Excluded country: {client.get('country')}"

    text = " ".join([job.get("title") or "", job.get("description") or job.get("description_snippet") or "",
                     " ".join(job.get("skills") or [])])
    for kw in s["exclude_keywords"]:
        if keyword_in(text, kw):
            return False, f"Contains excluded word “{kw}”"
    if s["include_keywords"] and not any(keyword_in(text, kw) for kw in s["include_keywords"]):
        return False, "Missing required keyword"
    return True, ""


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
    fields = [
        ("Budget", fmt_budget(job)),
        ("Client location", c.get("country") or "Unknown"),
        ("Client spent", c.get("total_spent") or "$0"),
        ("Payment method", "✅ Verified" if verified else "❌ Not verified"),
        ("Client rating", rating),
        ("Proposals", job.get("proposals_tier") or "n/a"),
        ("Experience", (job.get("experience_level") or "n/a").replace("_", " ").title()),
        ("Skills", ", ".join(job.get("skills") or []) or "n/a"),
    ]
    if job.get("_query"):
        fields.append(("Matched search", job["_query"]))
    return fields


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
def send_email(s, subject, text, html_body):
    if not cfg.smtp_configured(s):
        raise RuntimeError("Email isn't set up — fill in the SMTP fields under Email & schedule")
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, s["mail_to"]
    msg["From"] = s["mail_from"] or s["smtp_user"]
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    if s["smtp_port"] == 465:
        with smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"], timeout=30) as srv:
            srv.login(s["smtp_user"], s["smtp_pass"])
            srv.send_message(msg)
    else:
        with smtplib.SMTP(s["smtp_host"], s["smtp_port"], timeout=30) as srv:
            srv.starttls()
            srv.login(s["smtp_user"], s["smtp_pass"])
            srv.send_message(msg)


def notify_reauth(s):
    """One email per outage when the Upwork session can't be refreshed."""
    if meta_get("reauth_notified") == "1" or not s["email_enabled"] or not cfg.smtp_configured(s):
        return
    url = html.escape(PUBLIC_URL, quote=True)
    try:
        send_email(
            s, "⚠️ Upwork Job Alerts needs you to reconnect",
            f"Your Upwork session couldn't be refreshed, so job checks are paused.\n"
            f"Open {PUBLIC_URL} and click Reconnect.\n",
            f'<p>Your Upwork session couldn\'t be refreshed, so job checks are paused.</p>'
            f'<p><a href="{url}">Open the dashboard</a> and click <b>Reconnect</b>.</p>')
        meta_set("reauth_notified", "1")
    except Exception as exc:
        log.error("Reconnect notice failed: %s", exc)


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
    STATE["polling"] = True
    s = cfg.load()
    try:
        jobs = asyncio.run(fetch_jobs(s))
        result = process_jobs(jobs, s)
        _backoff.update(until=0.0, seconds=0)
        _auth_fail["n"] = 0
        meta_del("needs_reauth", "reauth_notified")  # a successful poll proves the session is fine
        STATE.update(paused_until=None, last_found=len(jobs),
                     last_poll=db.now_iso(), last_result=result)
        meta_set("last_poll", STATE["last_poll"])
        meta_set("last_found", len(jobs))
        STATE["last_error"] = result.get("email_error")  # None when this cycle was clean
        return result
    except Exception as exc:
        msg = err_text(exc)
        if contains_exc(exc, NotConnected):
            _auth_fail["n"] += 1
            meta_set("needs_reauth", "1")
            msg = "Upwork session expired — click Reconnect (the app keeps retrying automatically)"
            if _auth_fail["n"] >= 2:  # one failure can be a blip; two in a row deserves an email
                notify_reauth(s)
        elif isinstance(exc, RateLimited) or is_rate_limit(msg):
            # Back off hard: 15 min, then 30, 60, max 2 h. Never hammer Upwork.
            _backoff["seconds"] = min(max(_backoff["seconds"] * 2, 900), 7200)
            _backoff["until"] = time.time() + _backoff["seconds"]
            STATE["paused_until"] = datetime.fromtimestamp(_backoff["until"], timezone.utc).isoformat()
            msg = f"Upwork rate limit hit — pausing {_backoff['seconds'] // 60} min ({msg})"
        log.error("Poll failed: %s", msg)
        STATE["last_error"] = msg
        STATE["last_result"] = {"error": msg}
        return {"error": msg}
    finally:
        STATE["polling"] = False
        _poll_lock.release()


def process_jobs(jobs, s):
    """Store every fetched job, evaluate the new ones, email the matches."""
    db.prune_jobs()
    sending = s["email_enabled"]
    with db.conn() as con:
        first_run = con.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone() is None
        known = db.job_statuses(con, jobs.keys())

        if first_run and not EMAIL_ON_FIRST_RUN:
            for j in jobs.values():
                db.upsert_job(con, j, "seeded", "Already online at first run", j.get("_query", ""))
            con.execute("INSERT OR IGNORE INTO meta VALUES ('seeded','1')")
            log.info("First run: saved %d existing jobs (no emails).", len(jobs))
            return {"seeded": len(jobs), "found": len(jobs), "new_emailed": 0}

        matches, filtered = [], 0
        for j in jobs.values():
            if known.get(j["id"]) in db.FINAL_STATUSES:
                continue
            ok, reason = evaluate(j, s)
            if ok:
                matches.append(j)
            else:
                filtered += 1
                db.upsert_job(con, j, "filtered", reason, j.get("_query", ""))
        con.execute("INSERT OR IGNORE INTO meta VALUES ('seeded','1')")

    matches.sort(key=lambda j: j.get("published_date") or "")
    sent, email_error = 0, None
    for j in matches:
        status, reason = "matched", "Email alerts are off"
        if sending:
            try:
                send_email(s, *render_email(j))
                status, reason, sent = "emailed", "", sent + 1
                log.info("Sent: %s", j.get("title"))
            except Exception as exc:  # stays retryable: status 'failed' is re-evaluated next cycle
                status, reason = "failed", f"Email failed: {exc}"[:300]
                email_error = f"email: {exc}"
                log.error("Email failed for %s: %s", j.get("title"), exc)
        with db.conn() as con:
            db.upsert_job(con, j, status, reason, j.get("_query", ""))
    return {"found": len(jobs), "new_emailed": sent, "matched": len(matches), "filtered": filtered,
            "email_error": email_error}


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def schedule_poll(seconds, run_now=False):
    kwargs = {"next_run_time": datetime.now(timezone.utc)} if run_now else {}
    scheduler.add_job(poll, "interval", seconds=seconds, id="poll", max_instances=1,
                      coalesce=True, replace_existing=True, **kwargs)


# --------------------------------------------------------------------------- #
# Flask: request guard (the dashboard can change SMTP settings, so protect it)
# --------------------------------------------------------------------------- #
def _allowed_hosts():
    hosts = {"localhost", "127.0.0.1", "::1", urlsplit(PUBLIC_URL).hostname or ""}
    if HOST not in ("0.0.0.0", "::"):
        hosts.add(HOST)
    return hosts


@app.before_request
def guard():
    # DNS-rebinding protection: only answer for our own host names.
    host = urlsplit("//" + request.host).hostname
    if host not in _allowed_hosts():
        abort(403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # CSRF protection: JSON bodies can't be sent cross-site without a CORS preflight
        # (which we never allow), and a foreign Origin is rejected outright.
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host and origin.rstrip("/") != PUBLIC_URL:
            abort(403)
        if not request.is_json:
            abort(415)


# --------------------------------------------------------------------------- #
# Flask routes
# --------------------------------------------------------------------------- #
def state_data():
    s = cfg.load()
    counts = db.job_counts()
    nxt = scheduler.get_job("poll")
    return {
        **STATE,
        "connected": is_connected(), "needs_reauth": needs_reauth(),
        "account": meta_get("account_name"),
        "connecting": AUTH["running"], "connect_error": AUTH["error"],
        "poll_seconds": s["poll_seconds"],
        "requests_per_hour": len(s["searches"]) * 3600 // s["poll_seconds"],
        "next_poll": nxt.next_run_time.isoformat() if nxt and nxt.next_run_time else None,
        "smtp_configured": cfg.smtp_configured(s), "email_enabled": s["email_enabled"],
        "mail_to": s["mail_to"], "counts": counts, "emails_sent": counts.get("emailed", 0),
        "total_jobs": sum(counts.values()),
    }


@app.get("/")
def dashboard():
    return render_template("index.html")


@app.get("/status")
@app.get("/api/state")
def api_state():
    return jsonify(state_data())


@app.get("/api/settings")
def api_settings_get():
    return jsonify(cfg.public(cfg.load()))


@app.post("/api/settings")
def api_settings_save():
    current = cfg.load()
    new, to_store, errors = cfg.validate(request.get_json(silent=True) or {}, current)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    db.settings_save(to_store)
    if new["poll_seconds"] != current["poll_seconds"] and scheduler.running:
        schedule_poll(new["poll_seconds"])
    log.info("Settings saved from the dashboard")
    return jsonify(cfg.public(new))


def _job_row(r):
    j = json.loads(r["data"] or "{}")
    c = j.get("client") or {}
    return {
        "id": r["job_id"], "title": r["title"] or "(untitled)", "status": r["status"],
        "reason": r["reason"], "query": r["query"], "found_at": r["found_at"],
        "published_at": r["published_at"],
        "url": clean_url(r["url"]) if r["url"] else "",
        "budget": fmt_budget(j) if j else "", "country": c.get("country") or "",
        "spent": c.get("total_spent") or "", "verified": c.get("verification_status") == "VERIFIED",
        "skills": (j.get("skills") or [])[:6],
    }


@app.get("/api/jobs")
def api_jobs():
    status = request.args.get("status") or None
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    total, rows = db.jobs_list(status, limit, offset)
    return jsonify({"total": total, "jobs": [_job_row(r) for r in rows]})


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


@app.post("/api/disconnect")
def api_disconnect():
    meta_del("oauth_tokens", "oauth_expires_at", "account_name", "org_uid", "needs_reauth", "reauth_notified")
    _org_cache["uid"] = ORG_UID
    return jsonify({"ok": True})


@app.post("/api/poll")
def api_poll():
    """Start a check in the background; answer immediately if it was refused, or after 2 s."""
    box = {}
    t = threading.Thread(target=lambda: box.update(poll()), daemon=True)
    t.start()
    t.join(2)
    return jsonify(box or {"started": True})


@app.post("/api/test-email")
def api_test_email():
    s = cfg.load()
    sample = {
        "id": "test", "title": "Test job: Senior Django/FastAPI developer", "job_type": "fixed",
        "budget": "500.00", "url": "https://www.upwork.com/jobs/~0123456789?utm_source=x",
        "description": "This is a test alert from your Upwork Job Alerts app.",
        "client": {"country": "United States", "total_spent": "$12,400.00", "rating": 4.8,
                   "total_reviews": 14, "verification_status": "VERIFIED"},
        "proposals_tier": "5 to 10", "experience_level": "expert", "skills": ["Python", "Django"],
        "_query": "Django",
    }
    try:
        send_email(s, *render_email(sample))
        return jsonify({"ok": True, "sent_to": s["mail_to"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def main():
    s = cfg.load()
    if not cfg.smtp_configured(s):
        log.warning("Email isn't configured yet — set it up in the dashboard (Email & schedule)")
    schedule_poll(s["poll_seconds"], run_now=True)
    scheduler.start()
    log.info("Polling %d searches every %ss (~%d Upwork requests/hour) — database: %s",
             len(s["searches"]), s["poll_seconds"], len(s["searches"]) * 3600 // s["poll_seconds"], db.DB_PATH)
    log.info("Open %s %s", PUBLIC_URL, "(connected)" if is_connected() else "and click 'Connect Upwork'")
    app.run(host=HOST, port=PORT, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
