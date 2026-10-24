# Upwork Job Alerts

Polls the Upwork MCP server and emails you every new job matching your tech stack
(title, description, budget, client location, client spend, payment verification, link).

## Setup
1. `python -m venv venv && source venv/bin/activate`  (Windows: `venv\Scripts\activate`)
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`
   (Gmail: use an App Password)
4. `python app.py`
5. Open **http://localhost:5050** and click **Connect Upwork** → sign in on Upwork → approve.
   That's it — no API key or token to copy. The session is saved in `jobs.db` and
   refreshed automatically.

## Endpoints
- `/`               dashboard (connect / disconnect, stats, "Check now", "Send test email")
- `/status`         JSON status (last poll, errors, emails sent, connected)
- `/connect`        start Upwork sign-in
- `/oauth/callback` Upwork redirects here after sign-in
- `/poll`           trigger a check now
- `/test-email`     send a sample alert

## Notes
- **Account safety (built in):** uses only Upwork's official MCP server with your OAuth consent
  (no scraping, no password, no proxies). Read-only: it only searches jobs — it never applies,
  messages or spends Connects. Polls at most every 5 min (default 10), pauses between searches,
  and automatically backs off 15 min → 2 h if Upwork ever rate-limits. Only job IDs are stored,
  and only for `MAX_AGE_HOURS + 6h`. You can revoke access anytime in Upwork settings.
- If you open the app from another host (server, tunnel), set `PUBLIC_URL` to that URL so
  Upwork can redirect back to `PUBLIC_URL/oauth/callback`.
- If the session ever expires, the dashboard shows "Not connected" — just click Connect again.
- First launch saves existing jobs silently (no email flood). Set `EMAIL_ON_FIRST_RUN=true` to disable that.
- Run as a single process to avoid duplicate polling.
- Requires `mcp` 1.x (pinned in requirements.txt).
