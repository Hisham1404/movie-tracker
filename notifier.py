#!/usr/bin/env python3
"""
BookMyShow booking watcher.

For every watch in config.json it loads the film's BookMyShow "buytickets"
page for each date and checks whether the chosen cinema has shows on that
exact date. When a watch flips from closed to open it sends one alert by
email, Telegram and/or WhatsApp (Twilio), whichever secrets are set.

State (what was already open, how many fetches failed in a row) is kept in
state.json, which the workflow carries between runs with the Actions cache.

Environment variables (all optional except at least one alert channel):
  Email:     SMTP_USER, SMTP_APP_PASSWORD, ALERT_EMAIL_TO,
             SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465)
  Telegram:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  WhatsApp:  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
             TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO   (e.g. whatsapp:+14155238886)
  Fetching:  SCRAPERAPI_KEY  (routes through an India IP), or
             PROXY_URL       (any http(s) proxy in India)
  Testing:   TEST_ALERT=1    send a test message on every channel and exit
"""

import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
from email.message import EmailMessage
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------

def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg.get("watches"):
        sys.exit("config.json has no 'watches'")
    return cfg


URL_TEMPLATE = "https://in.bookmyshow.com/movies/{city}/{slug}/buytickets/{event}/{date}"

# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


def fetch(url):
    """Fetch a page, through an India IP when one is configured.

    BookMyShow refuses most datacenter and non-India IPs (GitHub's runners
    included), so without SCRAPERAPI_KEY or PROXY_URL expect HTTP 403.
    """
    key = os.environ.get("SCRAPERAPI_KEY")
    try:
        if key:
            api = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
                {"api_key": key, "country_code": "in", "url": url}
            )
            resp = requests.get(api, timeout=90)
        else:
            proxy = os.environ.get("PROXY_URL")
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30, proxies=proxies)
    except requests.RequestException as exc:
        raise FetchError(f"network error: {exc.__class__.__name__}") from exc

    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    text = resp.text
    # A 200 can still be a bot-check page rather than BookMyShow itself.
    if "bookmyshow" not in text.lower():
        raise FetchError("got a page that is not BookMyShow (likely a bot check)")
    return text


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def find_venue_link(page, city, date, venue_code=None, venue_slug_hint=None):
    """Return the venue code if the cinema has a booking link for this date.

    BookMyShow only renders a link of the form
        /cinemas/<city>/<venue-slug>/buytickets/<VENUECODE>/<YYYYMMDD>
    for a cinema that has live shows on that exact date. When the asked date
    is not open yet, BookMyShow falls back to the nearest open date, but the
    links then carry that other date, so this check cannot be fooled by it.
    """
    if venue_code:
        if f"/buytickets/{venue_code}/{date}" in page:
            return venue_code
        return None
    if venue_slug_hint:
        pattern = (
            r"/cinemas/" + re.escape(city) + r"/([^/\"'\s]*"
            + re.escape(venue_slug_hint.lower())
            + r"[^/\"'\s]*)/buytickets/([A-Z0-9]+)/" + re.escape(date)
        )
        m = re.search(pattern, page, flags=re.IGNORECASE)
        if m:
            return m.group(2)
    return None


def screens_seen(page, keywords):
    """Which of the wanted screen words (IMAX, EPIQ, ...) appear on the page.

    BookMyShow's page lists formats for the whole film, not per cinema, so
    this is a hint for the alert text, not proof that the cinema's IMAX or
    EPIQ shows are open.
    """
    low = page.lower()
    return [k for k in keywords if k.lower() in low]


def discover_event_code(city, slug):
    """Find a film's BookMyShow event code (ET########) from the city's
    movie list. Returns None when the film isn't listed there yet."""
    page = fetch(f"https://in.bookmyshow.com/explore/movies-{city}")
    found = re.findall(
        r"/movies/(?:[^/\"'\s]+/)?(" + re.escape(slug) + r"[^/\"'\s]*)/(ET\d{8})", page
    )
    for found_slug, code in found:  # prefer the exact slug over e.g. a dubbed version
        if found_slug == slug:
            return code
    return found[0][1] if found else None


def check_watch(watch, city, venue, state):
    """Check one film for every date not already seen open.
    Returns {date: info or None}."""
    dates = [d for d in watch["dates"]
             if not state.get("open", {}).get(f"{watch['film']}|{d}")]
    if not dates:
        print(f"[{watch['film']}] already open on every watched date; skipping")
        return {}
    event = watch.get("event_code") or state.setdefault("event_codes", {}).get(watch["slug"])
    if not event:
        event = discover_event_code(city, watch["slug"])
        if not event:
            print(f"[{watch['film']}] not listed on BookMyShow {city} yet")
            return {date: None for date in dates}
        print(f"[{watch['film']}] found BookMyShow event code {event}")
        state["event_codes"][watch["slug"]] = event

    results = {}
    for date in dates:
        url = URL_TEMPLATE.format(city=city, slug=watch["slug"], event=event, date=date)
        page = fetch(url)
        code = find_venue_link(
            page, city, date,
            venue_code=venue.get("code") or None,
            venue_slug_hint=venue.get("slug_hint"),
        )
        if code:
            results[date] = {
                "url": url,
                "venue_code": code,
                "screens": screens_seen(page, watch.get("screens", [])),
            }
        else:
            results[date] = None
    return results


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def send_email(subject, body):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_APP_PASSWORD")
    to = os.environ.get("ALERT_EMAIL_TO") or user
    if not (user and password and to):
        return None
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
    return "email"


def send_telegram(subject, body):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return None
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": f"{subject}\n\n{body}"},
        timeout=30,
    )
    resp.raise_for_status()
    return "telegram"


def send_whatsapp(subject, body):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    sender = os.environ.get("TWILIO_WHATSAPP_FROM")
    to = os.environ.get("TWILIO_WHATSAPP_TO")
    if not (sid and token and sender and to):
        return None
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data={"From": sender, "To": to, "Body": f"{subject}\n\n{body}"},
        auth=(sid, token),
        timeout=30,
    )
    resp.raise_for_status()
    return "whatsapp"


def notify(subject, body):
    sent, failed = [], []
    for sender in (send_email, send_telegram, send_whatsapp):
        try:
            channel = sender(subject, body)
            if channel:
                sent.append(channel)
        except Exception as exc:  # one broken channel must not stop the others
            failed.append(f"{sender.__name__}: {exc.__class__.__name__}")
    print(f"alert sent via: {', '.join(sent) or 'nothing'}"
          + (f"; failed: {'; '.join(failed)}" if failed else ""))
    return sent


def pretty(date):
    return time.strftime("%a %d %b %Y", time.strptime(date, "%Y%m%d"))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    cfg = load_config()

    if os.environ.get("TEST_ALERT") == "1":
        sent = notify(
            "Test: BookMyShow notifier",
            "This is a test alert. If you can read it, this channel works.",
        )
        if not sent:
            sys.exit("No alert channel is set up. Add the secrets listed in the README.")
        return 0

    stop_after = cfg.get("stop_after")
    if stop_after and time.strftime("%Y%m%d", time.gmtime(time.time() + 19800)) > stop_after:
        print(f"Past stop_after ({stop_after}); nothing to do. Disable the workflow to stop runs.")
        return 0

    city = cfg["city"]
    venue = cfg["venue"]
    fail_limit = int(cfg.get("alert_after_failed_runs", 6))
    state = load_json(STATE_PATH, {"open": {}, "failed_runs": 0, "blocked_alerted": False})

    watches = [w for w in cfg["watches"] if w.get("enabled", True)]
    if not watches:
        sys.exit("No enabled watches in config.json")

    fetch_errors = []
    for watch in watches:
        name = watch["film"]
        try:
            results = check_watch(watch, city, venue, state)
        except FetchError as exc:
            print(f"[{name}] could not read BookMyShow: {exc}")
            fetch_errors.append(str(exc))
            continue

        for date, info in results.items():
            key = f"{name}|{date}"
            was_open = state["open"].get(key, False)
            print(f"[{name} {date}] open={bool(info)} (was {was_open})")
            if info and not was_open:
                screens = ", ".join(info["screens"]) or "none of " + ", ".join(watch.get("screens", []))
                body = (
                    f"{name} ({watch.get('language', 'any language')}) has shows at "
                    f"{venue['label']} on {pretty(date)}.\n"
                    f"Screens wanted: {', '.join(watch.get('screens', [])) or 'any'}. "
                    f"Formats mentioned on the page: {screens}.\n"
                    f"Open the page and check the {watch.get('language', '')} "
                    f"{'/'.join(watch.get('screens', []))} show before booking:\n{info['url']}"
                )
                notify(f"Booking open: {name} on {pretty(date)}", body)
            state["open"][key] = bool(info)

    # Tell the owner once if BookMyShow keeps refusing us, so silence never
    # looks like "not open yet" when the watcher is actually blind.
    if fetch_errors:
        state["failed_runs"] = state.get("failed_runs", 0) + 1
        if state["failed_runs"] >= fail_limit and not state.get("blocked_alerted"):
            notify(
                "BookMyShow notifier cannot read BookMyShow",
                f"The last {state['failed_runs']} runs could not load BookMyShow "
                f"({fetch_errors[0]}). Alerts will not fire until this is fixed. "
                "Add SCRAPERAPI_KEY or PROXY_URL (see README).",
            )
            state["blocked_alerted"] = True
    else:
        state["failed_runs"] = 0
        state["blocked_alerted"] = False

    state["checked_at"] = int(time.time())
    save_json(STATE_PATH, state)
    # Fail the run visibly in the Actions tab when nothing could be read.
    return 1 if fetch_errors and len(fetch_errors) == len(watches) else 0


if __name__ == "__main__":
    raise SystemExit(main())
