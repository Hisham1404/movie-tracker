# BookMyShow booking notifier

Checks BookMyShow every 15 minutes from GitHub Actions and alerts you by **email**, **Telegram** or **WhatsApp (Twilio)** the moment a cinema opens bookings for a film on a date you care about.

Set up for Broadway Cinemas, Coimbatore:

| Film | Language | Screen | Dates |
|---|---|---|---|
| Avengers: Doomsday | English | EPIQ | Mon 21 Dec 2026 |
| Dune: Part Three | English | IMAX | Mon 21 Dec 2026 |

The detection idea (a cinema only gets a `/buytickets/<venue code>/<date>` link once it has shows on that date) comes from [ezioMJ/Movie-Alert](https://github.com/ezioMJ/Movie-Alert). That repo has no licence, so this code is written fresh rather than copied.

## The catch: BookMyShow blocks GitHub's servers

BookMyShow only serves Indian, non-datacenter IPs. A plain request from a GitHub runner gets **HTTP 403**. So the notifier reads BookMyShow through scraping services that exit from an Indian IP. Add the secret for each service you have an account with; a service whose secret is missing is skipped.

| Secret | Service | Asks for an India IP with | Credits per request |
|---|---|---|---|
| `SCRAPERAPI_KEY` | [ScraperAPI](https://www.scraperapi.com/) | `country_code=in` | 10 (1 on a site without bot protection) |
| `SCRAPEDO_TOKEN` | [Scrape.do](https://scrape.do/) | `geoCode=in` and `super=true` | 10 |
| `SCRAPINGANT_KEY` | [ScrapingAnt](https://scrapingant.com/) | `proxy_country=IN` and `browser=false` | 1 |

Every page the notifier fetches goes to the next service in turn (ScraperAPI, Scrape.do, ScrapingAnt, then ScraperAPI again, and so on). GitHub numbers its runs and the notifier uses that number to carry the turns on from run to run, so nothing has to be stored. If a service errors, is out of credits or returns a bot-check page, the page goes to the next service instead and the failed one is skipped for the rest of that run. `PROXY_URL` (any http(s) proxy in India, as `http://user:pass@host:port`) is tried last. A page that BookMyShow itself answers with 404 is reported as missing without asking another service.

The run log says which service served each page and what it cost, for example `ScraperAPI ok in 1.6s, credits 10: /movies/...`. Keys are never printed.

Check these against your plans. They come from each service's docs (27 Nov 2026):

- **ScraperAPI** lists India (`in`) for the Business plan and above. Hobby and Startup get only US and EU, and the free plan is not listed, so on those plans ScraperAPI cannot give you an Indian IP; leave `SCRAPERAPI_KEY` unset to skip it. A site behind bot protection (Cloudflare, Akamai and similar) costs about 10 credits a request instead of 1, and BookMyShow is one: the run log shows 10 credits for each page.
- **Scrape.do** has India only in its residential country list, so the request needs `super=true`, which is 10 credits instead of 1. Its free plan has 1,000 credits a month, about 100 requests.
- **ScrapingAnt** loads a full browser unless told not to (`browser` defaults to true, 10 credits), so the notifier sends `browser=false`. Its free plan has 10,000 credits a month and allows one request at a time.

Without any of these, every run fails, and after 6 failed runs in a row you get one warning alert saying the notifier is blind.

### How many credits this uses

The notifier runs 64 times a day (every 15 minutes from 02:00 to 17:45 UTC) and asks for one page per film and date that has not opened yet. With one date for each of the two films that is 2 pages a run, about **128 pages a day**, a third of them on each service, so about 43 each. A film date that has opened is not checked again. At the prices above (the run log showed 10 for ScraperAPI and 1 for ScrapingAnt) that is roughly 430 credits a day on ScraperAPI, 430 on Scrape.do and 43 on ScrapingAnt, so the free plans last about 2 days, 2 days and 7 months. Every extra date multiplies this: four dates for each film is 8 pages a run, about 512 pages a day. When a service runs out, its turns go to the next one.

## Set it up

1. **Make the repo.** On GitHub, create a new **private** repository (for example `bms-notifier`) and upload every file in this folder, keeping the `.github/workflows/watch.yml` path.
2. **Add secrets.** Repo → Settings → Secrets and variables → Actions → New repository secret. Add the ones for the channels you want and for the scraping services you use (table below).
3. **Test the alerts.** Actions tab → `bookmyshow-watch` → Run workflow → tick *Only send a test alert* → Run. You should get a test message on each channel you set up.
4. **Test BookMyShow.** Run the workflow again without the tick. Open the run log: `open=False` means it read BookMyShow and the date isn't open yet (good), and the `[fetch]` line above it says which service did the reading. `could not read BookMyShow:` followed by what each service answered, such as `ScraperAPI: HTTP 403 (out of credits)`, means none of them got through, so check the keys and credits. With no scraping service or proxy set it is just `HTTP 403`: BookMyShow is blocking the runner.

After that it runs by itself every 15 minutes from 07:30 to 23:15 IST, and stops checking after 21 Dec 2026 (`stop_after` in `config.json`). Disable the workflow in the Actions tab when you're done.

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
| `SCRAPERAPI_KEY` | Reading BookMyShow | Your ScraperAPI key. See "The catch" above |
| `SCRAPEDO_TOKEN` | Reading BookMyShow | Your Scrape.do token |
| `SCRAPINGANT_KEY` | Reading BookMyShow | Your ScrapingAnt API key |
| `PROXY_URL` | Reading BookMyShow (last fallback) | An http(s) proxy in India, as `http://user:pass@host:port` |

Use any one channel or all of them. A broken channel doesn't stop the others. The same goes for the three scraping services: use one, two or all three.

### About WhatsApp through Twilio

It works, but the free Twilio **sandbox** is a poor fit for a watch that runs for weeks:

- You join the sandbox by sending it a `join <code>` message, and that lapses after **3 days**, so you'd have to rejoin every 3 days.
- WhatsApp only delivers free-text messages within **24 hours** of your last message to the sandbox. Outside that window only Twilio's pre-approved templates go through, and custom ones aren't allowed in the sandbox.
- Messages are billed at Twilio's normal WhatsApp rates (trial accounts include some free messages).

A proper WhatsApp sender needs a Meta-verified WhatsApp Business profile and an approved message template. For this watch, **email plus Telegram is the reliable pair**.

## Changing what it watches

Everything is in `config.json`:

- `venue.code`: Broadway's BookMyShow venue code. Leave empty to match any cinema whose link contains `slug_hint` (`broadway`). Once you see Broadway on BookMyShow, its link looks like `/cinemas/coimbatore/<name>/buytickets/<CODE>/<date>`; put `<CODE>` here to be exact. The run log also prints it when Broadway opens.
- `watches[].dates`: add more days like `20261222` (each extra date is one more BookMyShow request per run, so watch the credit budget).
- `watches[].slug` and `watches[].event_code`: the film's part of the BookMyShow link, `/movies/<city>/<slug>/<event code>`. **BookMyShow gives a premium format its own event**, so use the code of the format you want, not the film's main one. Avengers uses `ET00526287` (EPIQ) and Dune uses `ET00526290` (slug `dune-part-three-imax-2d`, the IMAX 2D event). Dune's plain English 2D event is `ET00491771`; Broadway had no shows under it when this was checked, so watching it never alerted. To find a format's code, open that format's showtimes page on BookMyShow and copy the `ET…` part of the address. Leave `event_code` empty only when the film's main event is the one you want: the notifier then looks it up on the Coimbatore movie list (`/explore/movies-coimbatore`) each run until the film is listed, picks the plain slug, and remembers it.
- `watches[].screens`: words like `EPIQ` or `IMAX`. The alert mentions which of them appear on the page. BookMyShow's page lists formats for the whole film, not per cinema, so **check the show yourself before booking**: the alert means "Broadway has shows for this film on this date", not "the EPIQ English show is confirmed".

## Actions minutes

GitHub bills each run in whole minutes, rounded up. 64 runs a day is about 1,920 minutes a month if every run finishes in under a minute; a run of 61 seconds bills 2. A **public** repo has unlimited free minutes. A private repo on GitHub Free gets 2,000 a month, which this schedule only just fits and a few slow runs would push past; if you keep the repo private and run short, set the cron in `.github/workflows/watch.yml` back to hourly. A service that fails is not asked again for the rest of the run, so a run waits at most a few minutes on failing services.

## Running the tests

```
pip install requests
python tests/test_notifier.py
```
