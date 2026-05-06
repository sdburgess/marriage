# cupid

NYC marriage **ceremony** appointment cancellation-watcher and auto-booker.

You already have a license appointment. You have a specific ceremony date in
mind that's currently booked. This polls the City Clerk's scheduler around
the clock, notifies both phones the moment a slot opens, and (if you let it)
books your preferred date automatically.

The original 2021 Go Lambda lives in `legacy/` for reference.

## How it works

- **Site:** `clerkscheduler.cityofnewyork.us` -- a Salesforce Experience Cloud
  SPA. Naive HTTP scraping does not work, so we drive a real Chromium via
  Playwright.
- **Watch:** every ~45 seconds (jittered) we open the ceremony flow, walk to
  the Manhattan calendar, and read the available days/times in your date
  window.
- **Notify:** when new slots appear, fan-out alerts go to Twilio SMS, email
  (SMTP), and Slack -- independently. SMS to both partners' phones is the
  recommended primary.
- **Book:** if `auto_book` is on and a slot matches `preferred_date`, the
  booker fills the form and submits. If a CAPTCHA blocks submit, it stops at
  the confirmation page and texts you a link to finalize manually.
- **Slack-confirm (optional):** for slots that don't match `preferred_date`,
  the Slack message has *Book* / *Skip* buttons. If no one clicks within
  `server.slack_confirm_timeout_seconds`, we auto-book anyway so a missed
  phone doesn't lose the slot.
- **Web dashboard:** every fly.io machine ships a small dashboard at
  `https://<your-app>.fly.dev/`. It shows status, recent activity, pending
  Slack confirmations, all bookings, and lets you edit `config.yaml`,
  pause/resume, or reload from disk. HTTP-basic auth gates it.
- **State:** `state.json` remembers slots we've already announced (so we
  don't spam) and bookings we've already made (so we don't double-book).

## End-to-end setup

Plan ~60-90 minutes. You'll spend ~$10/mo total: Twilio ~$1 + fly.io ~$3-5
+ Resend free tier.

### 1. Clone and install

Requires Python 3.11+.

```sh
git clone https://github.com/sdburgess/marriage.git
cd marriage
git checkout claude/nyc-marriage-appointment-scraper-8GweX

python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

### 2. Twilio (SMS)

1. Sign up at <https://www.twilio.com/try-twilio>. Trial gives ~$15 credit.
2. **Buy a number**: Console &rarr; Phone Numbers &rarr; Buy a number &rarr;
   any US number with SMS (~$1/mo).
3. **Verify recipient phones** (trial only): Console &rarr; Phone Numbers
   &rarr; Verified Caller IDs &rarr; add both phones. Each gets a code text.
4. From the Console homepage copy:
   - **Account SID** &rarr; `TWILIO_ACCOUNT_SID`
   - **Auth Token** &rarr; `TWILIO_AUTH_TOKEN`
   - The number you bought &rarr; `TWILIO_FROM_NUMBER` (E.164, e.g. `+12125550100`)

### 3. Slack app (notifications + Book/Skip buttons)

1. Go to <https://api.slack.com/apps?new_app=1> &rarr; **Create New App**
   &rarr; **From scratch**. Name it (e.g. `cupid-watcher`), pick the
   workspace you and your fianc&eacute; share.
2. Sidebar &rarr; **OAuth & Permissions** &rarr; under **Bot Token Scopes** add:
   - `chat:write`
   - `chat:write.public`
3. Scroll up &rarr; **Install to <Workspace>** &rarr; approve.
4. Copy the **Bot User OAuth Token** (starts `xoxb-`) &rarr; `SLACK_BOT_TOKEN`.
5. **Basic Information** &rarr; **Signing Secret** &rarr; copy &rarr; `SLACK_SIGNING_SECRET`.
6. In Slack: create a channel (e.g. `#wedding-bot`), invite your fianc&eacute;,
   type `/invite @cupid-watcher`. Right-click channel &rarr; **View channel
   details** &rarr; bottom &rarr; copy **Channel ID** &rarr; `SLACK_CHANNEL_ID`.
7. Leave Interactivity off for now -- we'll come back in step 10 when we
   have a public URL.

### 4. Email (Resend)

1. Sign up at <https://resend.com>. Free tier is 3,000/mo.
2. **Domains** &rarr; add and verify a domain you own (10 min DNS), or
   skip and use `onboarding@resend.dev`.
3. **API Keys** &rarr; create &rarr; copy the value.
4. Set:
   - `SMTP_HOST=smtp.resend.com`
   - `SMTP_PORT=587`
   - `SMTP_USERNAME=resend`
   - `SMTP_PASSWORD=<the API key>`
   - `SMTP_FROM=onboarding@resend.dev` (or your verified address)

(Gmail SMTP also works -- `smtp.gmail.com` with an App Password.)

### 5. Fill in `.env`

```sh
cp .env.example .env
```

Paste everything from steps 2-4 plus:

```
WEB_USERNAME=cupid
WEB_PASSWORD=<pick a strong random one -- gates the dashboard>
```

Leave `TWOCAPTCHA_API_KEY=` empty unless/until you actually hit a CAPTCHA.

### 6. Fill in `config.yaml`

```sh
cp config.example.yaml config.yaml
```

Edit:

- `targets[0].earliest_date` / `latest_date` -- acceptable ceremony window
- `targets[0].preferred_date` -- the date you actually want
- `targets[0].preferred_time` -- optional preferred time
- `targets[0].preferred_only: true` if you'll only accept preferred_date
- `targets[0].license_number` -- the confirmation # from your already-booked license
- `applicants.partner_a/b` -- first name, last name, email, phone for each
- `notifications.sms_to` -- both phones in E.164 format
- `notifications.email_to` -- both emails

`config.yaml` is gitignored. Don't commit it.

### 7. Lock in scraper selectors (one-time, ~15 min)

The selectors in `cupid/scraper.py` and `cupid/booker.py` are educated
guesses. Run a recording session to confirm them against the live site:

```sh
python -m cupid record --kind ceremony
```

Chromium opens at `clerkscheduler.cityofnewyork.us`. Click through:
**In-Person Marriage Ceremony Appointment** &rarr; **Manhattan** &rarr; a
future date &rarr; a time &rarr; start the form. The script logs every
clicked element and every XHR. If button labels or form-field labels
differ from what's in `SELECTORS` (scraper.py) or `SCHEMA_BY_KIND`
(booker.py), update them and commit your changes locally.

### 8. Test locally

```sh
# Verify SMS + email + Slack arrive
python -m cupid notify-test

# Run one polling tick and exit
python -m cupid once

# Run the loop + dashboard locally
python -m cupid run
```

Open <http://localhost:8080>, basic-auth with `WEB_USERNAME`/`WEB_PASSWORD`,
confirm the dashboard loads. Hit Pause/Resume to verify. Ctrl-C when
satisfied.

### 9. Deploy to fly.io

```sh
# Install fly CLI
brew install flyctl                 # Mac
# or: curl -L https://fly.io/install.sh | sh   # Linux

fly auth signup                     # or: fly auth login

# Set up the app. Pick a unique name -- "cupid-watcher" probably collides.
# Region: ewr (Newark). Postgres/Redis: no.
fly launch --no-deploy --copy-config

# 1 GB volume in the same region
fly volumes create cupid_data --size 1 --region ewr

# Push every secret from .env
fly secrets set $(grep -v '^#' .env | grep -v '^$' | xargs)

# Upload config.yaml to the persistent volume
fly ssh sftp shell
# In the sftp prompt:
# put config.yaml /data/config.yaml
# exit

# Deploy
fly deploy
fly logs
```

You should see structured JSON, the scheduler starting, and FastAPI
binding to 8080. Note the public URL `https://<your-app>.fly.dev` and
verify the dashboard loads in a browser.

### 10. Wire Slack interactivity (now that you have a public URL)

1. Back at <https://api.slack.com/apps> &rarr; your app &rarr;
   **Interactivity & Shortcuts**.
2. Toggle **Interactivity** on.
3. **Request URL**: `https://<your-app>.fly.dev/slack/actions`
4. Save. Slack pings the URL; the HMAC check passes and verification succeeds.

If verification fails: check `fly logs`, confirm `SLACK_SIGNING_SECRET` is
set with `fly secrets list`. The webhook returns 401 if the secret is
unset -- safer than open routes.

### 11. Smoke test

```sh
fly ssh console
python -m cupid notify-test
```

You should get the test SMS, email, and Slack message. Check `fly logs`
again -- within ~45 seconds you should see polling activity.

### Daily operation

- **Watch from your phone**: bookmark the dashboard URL.
- **Slot found**: SMS to both phones + email + Slack. If the slot matches
  your preferred date, the booker books immediately and texts the
  confirmation number. Otherwise (a fallback date opened) the Slack
  message has Book/Skip buttons -- click within 90s or it auto-books.
- **Change preferred date**: edit YAML inline on the dashboard, click
  **Save and reload**. Next tick uses the new config.
- **Pause** if you booked manually or want to stop temporarily.

### Common failure modes

- **`fill_skipped` warnings** -- form field label changed; rerun
  `python -m cupid record` and update `SCHEMA_BY_KIND`.
- **Slack 401** -- `SLACK_SIGNING_SECRET` mismatch; re-copy and `fly secrets set`.
- **Twilio "unverified number"** -- trial account; upgrade or add the
  recipient as a Verified Caller ID.
- **CAPTCHA hit** -- booker stops at submit and texts a deep link.
  Click and finalize manually.
- **Rate-limit / IP block** -- bump `polling.base_interval_seconds` to
  60-90s. Don't go below 15s.

## Configuration cheatsheet

`config.yaml`:

| key                            | what it does                                         |
| ------------------------------ | ---------------------------------------------------- |
| `targets[].kind`               | `ceremony` (most common) or `license`                |
| `targets[].boroughs`           | leave as `[Manhattan]` -- that's the only location we want |
| `targets[].earliest_date`      | start of search window                               |
| `targets[].latest_date`        | end of search window                                 |
| `targets[].preferred_date`     | the date you actually want                           |
| `targets[].preferred_time`     | optional preferred time-of-day                       |
| `targets[].preferred_only`     | only book preferred_date; never settle for others    |
| `targets[].auto_book`          | true to actually submit, false for notify-only       |
| `targets[].slack_confirm`      | gate non-preferred slots on a Slack Book/Skip click  |
| `targets[].license_number`     | required for ceremony booking                        |
| `server.enabled`               | run the dashboard + Slack webhook (default true)     |
| `server.slack_confirm_timeout_seconds` | seconds to wait for Slack click before auto-booking |
| `polling.constant_mode`        | true for cancellation-watch (default)                |
| `polling.base_interval_seconds`| seconds between polls (45 is reasonable)             |
| `polling.jitter_seconds`       | random +/- jitter so we're not exactly periodic      |

`.env` / fly secrets:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
- `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`, `SLACK_SIGNING_SECRET`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
- `WEB_USERNAME`, `WEB_PASSWORD` (HTTP basic auth on the dashboard)
- `TWOCAPTCHA_API_KEY` (optional, only if you want CAPTCHA solving)

## Honest caveats

- **CAPTCHA.** If the site shows a CAPTCHA at submit and `TWOCAPTCHA_API_KEY`
  is unset, the booker stops at submit and texts you a link to finalize.
  That's the safe default. Wiring 2captcha requires a recording session to
  identify which CAPTCHA widget the city is using -- I left a clearly-marked
  `NotImplementedError` in `cupid/booker.py::_solve_captcha` for that.
- **Selector drift.** Salesforce changes class names occasionally. If the
  watcher starts logging `fill_skipped` or `no_service_dropdown_visible`,
  re-run `python -m cupid record` and update selectors.
- **Polling rate.** 45s with jitter is respectful and unlikely to get you
  IP-banned. Don't lower it below ~15s.
- **PII.** `config.yaml` has names, phones, emails, and license number. It's
  gitignored. Don't commit it.

## Layout

```
cupid/
  __main__.py            # CLI: run / once / serve / notify-test / record
  config.py              # Pydantic models for config.yaml + env Secrets
  controller.py          # Shared state: pause flag, pending Slack confirmations
  state.py               # JSON-on-disk: seen slots, recorded bookings
  notify.py              # Twilio SMS + SMTP + Slack fan-out (incl. Book/Skip buttons)
  scraper.py             # Playwright reader of clerkscheduler (SELECTORS dict)
  booker.py              # Form-driver, CAPTCHA fallback (SCHEMA_BY_KIND dict)
  scheduler.py           # Main loop, preferred-date priority, polling cadence
  server.py              # FastAPI dashboard + /slack/actions webhook
  templates/
    dashboard.html       # single-page Jinja dashboard
Dockerfile               # Playwright base image
fly.toml                 # fly.io machine + volume + http_service
config.example.yaml
.env.example
legacy/                  # the original Go Lambda
```
