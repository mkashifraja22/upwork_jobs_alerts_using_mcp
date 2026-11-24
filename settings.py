"""
User-editable settings (keywords, filters, email, schedule).

Resolution order for every setting:  value saved from the UI (database)  >  .env  >  built-in default.
So an existing .env keeps working as the starting point; once you press Save in the UI,
the database is the source of truth.
"""
import os
import re

import db

MIN_POLL_SECONDS = 300  # account safety: never poll Upwork faster than every 5 min
MAX_SEARCHES = 30
EXPERIENCE_LEVELS = ("entry", "intermediate", "expert")
JOB_TYPES = ("any", "fixed", "hourly")

DEFAULT_SEARCHES = "Django,FastAPI,Python backend,REST API,HubSpot,Salesforce integration,Zoho,RAG,LangChain"


def _bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _split(v):
    return [x.strip() for x in re.split(r"[,\n]", str(v or "")) if x.strip()]


def defaults():
    """Starting values, taken from .env (read at call time) with built-in fallbacks."""
    e = os.getenv
    smtp_user = e("SMTP_USER", "")
    return {
        # keywords
        "searches": _split(e("JOB_SEARCHES", DEFAULT_SEARCHES)),
        "include_keywords": [],
        "exclude_keywords": [],
        # filters
        "verified_only": _bool(e("VERIFIED_PAYMENT_ONLY", "false")),
        "min_client_spend": float(e("MIN_CLIENT_SPEND", "0") or 0),
        "min_client_rating": 0.0,
        "min_fixed_budget": 0.0,
        "min_hourly_rate": 0.0,
        "job_type": "any",
        "experience_levels": [],
        "countries_include": [],
        "countries_exclude": [],
        "max_age_hours": int(e("MAX_AGE_HOURS", "24") or 24),
        # schedule
        "poll_seconds": int(e("POLL_SECONDS", "600") or 600),
        "search_delay_seconds": float(e("SEARCH_DELAY_SECONDS", "3") or 3),
        # email
        "email_enabled": True,
        "mail_to": e("MAIL_TO", "") or smtp_user,
        "mail_from": e("MAIL_FROM", "") or smtp_user,
        "smtp_host": e("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(e("SMTP_PORT", "587") or 587),
        "smtp_user": smtp_user,
        "smtp_pass": e("SMTP_PASS", ""),
    }


def load():
    s = defaults()
    s.update({k: v for k, v in db.settings_all().items() if k in s})
    s["poll_seconds"] = max(int(s["poll_seconds"]), MIN_POLL_SECONDS)
    return s


def public(s):
    """Settings as sent to the browser: the SMTP password never leaves the server."""
    out = {k: v for k, v in s.items() if k != "smtp_pass"}
    out["smtp_pass_set"] = bool(s.get("smtp_pass"))
    return out


def smtp_configured(s):
    return bool(s["smtp_host"] and s["smtp_user"] and s["smtp_pass"] and s["mail_to"])


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _list(v, limit=100, item_len=100):
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    out, seen = [], set()
    for x in v or []:
        x = str(x).strip()[:item_len]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:limit]


def _num(v, lo, hi, name, errors, integer=False):
    try:
        n = float(v)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number")
        return lo
    if n != n or n < lo or n > hi:  # NaN or out of range
        errors.append(f"{name} must be between {lo:g} and {hi:g}")
        return lo
    return int(n) if integer else n


def _emails(v, name, errors):
    v = str(v or "").strip()
    addrs = _split(v)
    if any(not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", a) for a in addrs):
        errors.append(f"{name} must be a valid email address")
    return ", ".join(addrs)


def validate(raw, current):
    """Merge `raw` (from the browser) onto `current`. Returns (new_settings, to_store, errors)."""
    errors = []
    s = dict(current)

    def has(k):
        return k in raw

    if has("searches"):
        s["searches"] = _list(raw["searches"], limit=MAX_SEARCHES)
        if not s["searches"]:
            errors.append("Add at least one search keyword")
    for k in ("include_keywords", "exclude_keywords", "countries_include", "countries_exclude"):
        if has(k):
            s[k] = _list(raw[k])
    for k in ("verified_only", "email_enabled"):
        if has(k):
            s[k] = bool(raw[k])
    for k, name, hi in (("min_client_spend", "Min client spend", 1e9),
                        ("min_fixed_budget", "Min fixed budget", 1e9),
                        ("min_hourly_rate", "Min hourly rate", 1e5)):
        if has(k):
            s[k] = _num(raw[k] or 0, 0, hi, name, errors)
    if has("min_client_rating"):
        s["min_client_rating"] = _num(raw["min_client_rating"] or 0, 0, 5, "Min client rating", errors)
    if has("job_type"):
        s["job_type"] = raw["job_type"] if raw["job_type"] in JOB_TYPES else "any"
    if has("experience_levels"):
        s["experience_levels"] = [x for x in _list(raw["experience_levels"]) if x in EXPERIENCE_LEVELS]
    if has("max_age_hours"):
        s["max_age_hours"] = _num(raw["max_age_hours"], 1, 720, "Max job age (hours)", errors, integer=True)
    if has("poll_seconds"):
        s["poll_seconds"] = _num(raw["poll_seconds"], MIN_POLL_SECONDS, 86400,
                                 "Check interval (seconds)", errors, integer=True)
    if has("search_delay_seconds"):
        s["search_delay_seconds"] = _num(raw["search_delay_seconds"], 1, 60, "Delay between searches", errors)

    if has("mail_to"):
        s["mail_to"] = _emails(raw["mail_to"], "Send alerts to", errors)
    if has("mail_from"):
        s["mail_from"] = _emails(raw["mail_from"], "From address", errors)
    if has("smtp_host"):
        s["smtp_host"] = str(raw["smtp_host"]).strip()[:255]
    if has("smtp_user"):
        s["smtp_user"] = str(raw["smtp_user"]).strip()[:255]
    if has("smtp_port"):
        s["smtp_port"] = _num(raw["smtp_port"], 1, 65535, "SMTP port", errors, integer=True)

    # Password: blank = keep the current one. If the mail server or login changed, the old
    # password must not be silently sent there, so it is dropped unless a new one is given.
    new_pass = str(raw.get("smtp_pass") or "")
    if new_pass:
        s["smtp_pass"] = new_pass
    elif (s["smtp_host"], s["smtp_user"]) != (current["smtp_host"], current["smtp_user"]):
        s["smtp_pass"] = ""

    if s["email_enabled"] and not s["mail_to"] and not errors:
        errors.append("Enter an address to send alerts to (or turn email alerts off)")

    to_store = {k: v for k, v in s.items() if k != "smtp_pass"}
    if new_pass or s["smtp_pass"] != current["smtp_pass"]:
        to_store["smtp_pass"] = s["smtp_pass"]
    return s, to_store, errors
