# BookMyShow booking notifier

Checks BookMyShow every hour from GitHub Actions and alerts you by **email**, **Telegram** or **WhatsApp (Twilio)** the moment a cinema opens bookings for a film on a date you care about.

Set up for Broadway Cinemas, Coimbatore:

| Film | Language | Screen | Dates |
|---|---|---|---|
| Avengers: Doomsday | English | EPIQ | Mon 21 Dec 2026 |
| Dune: Part Three | English | IMAX | Mon 21 Dec 2026 |

The detection idea (a cinema only gets a `/buytickets/<venue code>/<date>` link once it has shows on that date) comes from [ezioMJ/Movie-Alert](https://github.com/ezioMJ/Movie-Alert). That repo has no licence, so this code is written fresh rather than copied.

## The catch: BookMyShow blocks GitHub's servers

BookMyShow only serves Indian, non-datacenter IPs. A plain request from a GitHub runner gets **HTTP 403**. The notifier needs one of these two secrets to actually read BookMyShow:

- `SCRAPERAPI_KEY`: a key from scraperapi.com. Requests go out through an Indian IP. The free plan gives 1,000 credits a month (5,000 in the first 7 days). This setup makes 2 requests per run (one per film), plus one to look up Dune until BookMyShow lists it, so 32 to 48 a day. A film that has opened is not checked again. Across 1 to 21 Dec that is about 670 to 1,000 requests, inside the free plan.
- `PROXY_URL`: any http(s) proxy in India, as `http://user:pass@host:port`.

Without either, every run fails, and after 6 failed runs in a row you get one warning alert saying the notifier is blind.

## Set it up

1. **Make the repo.** On GitHub, create a new **private** repository (for example `bms-notifier`) and upload every file in this folder, keeping the `.github/workflows/watch.yml` path.
2. **Add secrets.** Repo → Settings → Secrets and variables → Actions → New repository secret. Add the ones for the channels you want (table below).
3. **Test the alerts.** Actions tab → `bookmyshow-watch` → Run workflow → tick *Only send a test alert* → Run. You should get a test message on each channel you set up.
4. **Test BookMyShow.** Run the workflow again without the tick. Open the run log: `open=False` means it read BookMyShow and the date isn't open yet (good). `could not read BookMyShow: HTTP 403` means it is blocked (add `SCRAPERAPI_KEY` or `PROXY_URL`).

After that it runs by itself every hour from 07:47 to 22:47 IST, and stops checking after 21 Dec 2026 (`stop_after` in `config.json`). Disable the workflow in the Actions tab when you're done.

## Secrets

| Secret | Needed for | What to put in it |
|---|---|---|
| `SMTP_USER` | Email | The Gmail address that sends the alert |
| `SMTP_APP_PASSWORD` | Email | A Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords). Not your normal password. |
| `ALERT_EMAIL_TO` | Email (optional) | Where alerts go. Defaults to `SMTP_USER`. |
| `TELEGRAM_BOT_TOKEN` | Telegram | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram | Your chat id (message your bot, then read it from `getUpdates`) |
| `TWILIO_ACCOUNT_SID` | WhatsApp | From the Twilio console |
| `TWILIO_AUTH_TOKEN` | WhatsApp | From the Twilio console |
| `TWILIO_WHATSAPP_FROM` | WhatsApp | The Twilio WhatsApp sender, e.g. `whatsapp:+14155238886` for the sandbox |
| `TWILIO_WHATSAPP_TO` | WhatsApp | Your number, e.g. `whatsapp:+91XXXXXXXXXX` |
| `SCRAPERAPI_KEY` | Reading BookMyShow | See "The catch" above |
| `PROXY_URL` | Reading BookMyShow | Alternative to `SCRAPERAPI_KEY` |

Use any one channel or all of them. A broken channel doesn't stop the others.

### About WhatsApp through Twilio

It works, but the free Twilio **sandbox** is a poor fit for a watch that runs for weeks:

- You join the sandbox by sending it a `join <code>` message, and that lapses after **3 days**, so you'd have to rejoin every 3 days.
- WhatsApp only delivers free-text messages within **24 hours** of your last message to the sandbox. Outside that window only Twilio's pre-approved templates go through, and custom ones aren't allowed in the sandbox.
- Messages are billed at Twilio's normal WhatsApp rates (trial accounts include some free messages).

A proper WhatsApp sender needs a Meta-verified WhatsApp Business profile and an approved message template. For this watch, **email plus Telegram is the reliable pair**.

## Changing what it watches

Everything is in `config.json`:

- `venue.code`: Broadway's BookMyShow venue code. Leave empty to match any cinema whose link contains `slug_hint` (`broadway`). Once you see Broadway on BookMyShow, its link looks like `/cinemas/coimbatore/<name>/buytickets/<CODE>/<date>`; put `<CODE>` here to be exact. The run log also prints it when Broadway opens.
- `watches[].dates`: add more days like `20261222` (each extra date is one more BookMyShow request per run, so watch the free-plan budget).
- `watches[].slug` and `watches[].event_code`: the film's part of the BookMyShow link, `/movies/<city>/<slug>/<event code>`. Avengers uses `ET00526287`, taken from another public setup, so check it against the real link. Dune's code isn't public yet, so `event_code` is empty and the notifier looks it up on the Coimbatore movie list each run until the film is listed, then remembers it. Once you see Dune on BookMyShow, you can paste its `ET…` code in to skip the lookup.
- `watches[].screens`: words like `EPIQ` or `IMAX`. The alert mentions which of them appear on the page. BookMyShow's page lists formats for the whole film, not per cinema, so **check the show yourself before booking**: the alert means "Broadway has shows for this film on this date", not "the EPIQ English show is confirmed".

## Actions minutes

Each run takes under a minute and is billed as 1 minute. 16 runs a day is about **480 minutes a month**, inside the 2,000 free minutes a private repo gets on GitHub Free.

## Running the tests

```
pip install requests
python tests/test_notifier.py
```
