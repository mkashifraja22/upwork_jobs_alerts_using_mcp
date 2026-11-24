# Upwork Job Alerts

Polls the Upwork MCP server and emails you every new job matching your keywords and filters
(title, description, budget, client location, client spend, payment verification, link).
Everything is configured from the dashboard — no config files to edit.

## Setup
1. `python -m venv venv && source venv/bin/activate`  (Windows: `venv\Scripts\activate`)
2. `pip install -r requirements.txt`
3. `python app.py`
4. Open **http://localhost:5050**
   - **Connect Upwork** (one time) → sign in on Upwork → approve.
   - **Keywords & filters** tab → add your search keywords, exclusions and filters.
   - **Email & schedule** tab → enter your SMTP details (Gmail: use an App Password), press
     **Send test email**, then **Save changes**.

`.env` is optional now: its values (see `.env.example`) are only the starting defaults.
Once you press **Save changes** in the dashboard, the database is the source of truth.

## What you can change from the UI
- **Keywords:** search keywords (each is a separate Upwork search), "must mention one of",
  and "never alert if it mentions" (whole-word, case-insensitive).
- **Filters:** fixed / hourly, minimum fixed budget, minimum hourly rate, minimum client spend,
  minimum client rating, experience level, verified payment, max job age, only / skip client countries.
- **Email:** on/off, recipients, SMTP host / port / user / password, from address, test email.
- **Schedule:** check interval (minimum 5 min) and the pause between searches.
- **Recent jobs:** every job seen in the last 30 days with its status — *emailed*, *filtered out*
  (with the exact reason, e.g. "Contains excluded word “wordpress”"), *email failed* — so you can
  tune filters. If you loosen a filter, still-fresh jobs it previously hid are sent on the next check.

## Staying connected
Everything lives in one SQLite file, `jobs.db`, next to `app.py` (always — regardless of the folder
you start the app from): the Upwork login (access + refresh tokens), your settings and the job history.
Restarting the app or the computer does **not** log you out; the access token is refreshed automatically.

If Upwork ever refuses a refresh, the app does **not** throw the session away: it keeps retrying every
cycle, the dashboard shows **Reconnect needed**, and (after two failures in a row) it emails you once.
The flag clears itself as soon as a check succeeds. Back up or move `jobs.db` to keep your login.
The only time you must sign in again is if you click **Disconnect**, delete `jobs.db`, revoke the app
in Upwork settings, or the app stays off longer than Upwork keeps refresh tokens valid.

## Endpoints
- `/`                dashboard
- `/api/state`       JSON status (also at `/status`)
- `/api/settings`    GET / POST settings (JSON; the SMTP password is never returned)
- `/api/jobs`        job history (`?status=emailed|filtered|failed&limit=50&offset=0`)
- `/api/poll`, `/api/test-email`, `/api/disconnect`   POST actions used by the dashboard
- `/connect`, `/oauth/callback`   Upwork sign-in

The dashboard only listens on `127.0.0.1`, checks the `Host` / `Origin` headers and requires JSON for
every state-changing request, so other websites you visit can't change your settings.

## Notes
- **Account safety (built in):** uses only Upwork's official MCP server with your OAuth consent
  (no scraping, no password, no proxies). Read-only: it only searches jobs — it never applies,
  messages or spends Connects. Polls at most every 5 min (default 10), pauses between searches,
  and automatically backs off 15 min → 2 h if Upwork ever rate-limits. You can revoke access anytime
  in Upwork settings.
- Job details (title, description, client info) are stored locally in `jobs.db` for 30 days.
- If you open the app from another host (server, tunnel), set `PUBLIC_URL` to that URL so
  Upwork can redirect back to `PUBLIC_URL/oauth/callback`.
- First launch saves existing jobs silently (no email flood). Set `EMAIL_ON_FIRST_RUN=true` to disable that.
- Run as a single process to avoid duplicate polling.
- Requires `mcp` 1.x (pinned in requirements.txt).
