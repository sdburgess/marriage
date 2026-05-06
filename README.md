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
  the calendar for each configured borough, and read the available days/times
  in your date window.
- **Notify:** when new slots appear, fan-out alerts go to Twilio SMS, email
  (SMTP), and Slack. SMS to both partners' phones is the recommended primary.
- **Book:** if `auto_book` is on and a slot matches `preferred_date`, the
  booker fills the form and submits. If a CAPTCHA blocks submit, it stops at
  the confirmation page and texts you a link to finalize manually.
- **State:** `state.json` remembers slots we've already announced (so we
  don't spam) and bookings we've already made (so we don't double-book).

## Setup

### 1. Local dev

Requires Python 3.11+.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium

cp config.example.yaml config.yaml
cp .env.example .env
# Edit both. config.yaml has PII, .env has secrets. Both are gitignored.
```

### 2. Lock in selectors with a recording session

The selectors in `cupid/scraper.py` are educated starting guesses; Salesforce
sites occasionally rename roles/labels. Run once with a real browser before
trusting auto-book:

```sh
python -m cupid record --kind ceremony
```

A Chromium window opens. Walk through the booking flow yourself. The script
prints the role/label of every element you click and every XHR the page makes.
Update `SELECTORS` in `cupid/scraper.py` and `SCHEMA_BY_KIND` in
`cupid/booker.py` to match what you saw.

### 3. Test notifications

```sh
python -m cupid notify-test
```

You should get an SMS, email, and (if configured) Slack message saying the
test went through.

### 4. Single-shot poll

To verify watching works without committing to the loop:

```sh
python -m cupid once
```

### 5. Run the loop

```sh
python -m cupid run
```

## Deploy to fly.io

```sh
# One-time
fly launch --no-deploy --copy-config           # accept name "cupid-watcher" or pick your own
fly volumes create cupid_data --size 1 --region ewr

# Push secrets (env, never committed)
fly secrets set $(grep -v '^#' .env | xargs)

# Upload your config.yaml into the persistent volume
fly ssh sftp shell
# > put config.yaml /data/config.yaml
# > exit

fly deploy
fly logs
```

State (`state.json`) and `config.yaml` live on the volume so deploys don't
wipe them.

## Configuration cheatsheet

`config.yaml`:

| key                            | what it does                                         |
| ------------------------------ | ---------------------------------------------------- |
| `targets[].kind`               | `ceremony` (most common) or `license`                |
| `targets[].boroughs`           | list of NYC boroughs to search                       |
| `targets[].earliest_date`      | start of search window                               |
| `targets[].latest_date`        | end of search window                                 |
| `targets[].preferred_date`     | the date you actually want                           |
| `targets[].preferred_time`     | optional preferred time-of-day                       |
| `targets[].preferred_only`     | only book preferred_date; never settle for others    |
| `targets[].auto_book`          | true to actually submit, false for notify-only       |
| `targets[].license_number`     | required for ceremony booking                        |
| `polling.constant_mode`        | true for cancellation-watch (default)                |
| `polling.base_interval_seconds`| seconds between polls (45 is reasonable)             |
| `polling.jitter_seconds`       | random +/- jitter so we're not exactly periodic      |

`.env` / fly secrets:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`
- `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
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
  __main__.py     # CLI: run / once / notify-test / record
  config.py       # Pydantic models for config.yaml + env Secrets
  state.py        # JSON-on-disk: seen slots, recorded bookings
  notify.py       # Twilio SMS + SMTP + Slack fan-out
  scraper.py      # Playwright reader of clerkscheduler (SELECTORS dict)
  booker.py       # Form-driver, CAPTCHA fallback (SCHEMA_BY_KIND dict)
  scheduler.py    # Main loop, preferred-date priority, polling cadence
Dockerfile        # Playwright base image
fly.toml          # fly.io machine + volume
config.example.yaml
.env.example
legacy/           # the original Go Lambda
```
