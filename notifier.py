#!/usr/bin/env python3
"""
BookMyShow booking watcher.

For every watch in config.json it loads the film's BookMyShow "buytickets"
page for each date and checks whether the chosen cinema has shows on that
exact date. When a watch flips from closed to open it sends one alert by
email, Telegram and/or WhatsApp (Twilio), whichever secrets are set. The
email is flagged urgent (a [URGENT] subject and high-priority headers).

State (what was already open, how many fetches failed in a row) is kept in
state.json, which the workflow carries between runs with the Actions cache.

Environment variables (all optional except at least one alert channel):
  Email:     SMTP_USER, SMTP_APP_PASSWORD, ALERT_EMAIL_TO,
             SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465)
  Telegram:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  WhatsApp:  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
             TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO   (e.g. whatsapp:+14155238886)
  Fetching:  SCRAPERAPI_KEY, SCRAPEDO_TOKEN, SCRAPINGANT_KEY
                 scraping services that fetch through an India IP. Each one
                 whose secret is set takes its turn; if it fails (error, out
                 of credits, bot-check page) the next one is used. Services
                 without a secret are skipped.
             PROXY_URL
                 any http(s) proxy in India, the last fallback
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
from typing import Callable, NamedTuple

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


class _RouteFailed(Exception):
    """One route gave no usable page, so the next route gets a go."""


class Service(NamedTuple):
    name: str                # shown in the logs
    env: str                 # environment variable holding its key or token
    endpoint: str
    params: Callable         # (secret, page url) -> query parameters of one request
    cost_header: str         # response header saying how many credits a request used
    errors: dict             # its own error statuses -> what they mean, for the log
    status_header: str = ""  # header carrying the page's own status, if it uses one


# The scraping services, in the order they take turns. Each is asked for an
# India exit IP, because BookMyShow refuses datacenter and non-India addresses,
# and for a plain page without JavaScript rendering, the cheapest kind of
# request. Request formats and credit costs are from the services' own docs;
# the README has the numbers.
SERVICES = (
    Service(
        # https://docs.scraperapi.com/  India needs the Business plan or above.
        # ScraperAPI wants its own parameters before url, so url comes last.
        name="ScraperAPI",
        env="SCRAPERAPI_KEY",
        endpoint="https://api.scraperapi.com/",
        params=lambda key, url: {"api_key": key, "country_code": "in", "url": url},
        cost_header="sa-credit-cost",
        errors={401: "key rejected", 403: "out of credits",
                429: "too many parallel requests"},
    ),
    Service(
        # https://scrape.do/documentation/  India is only in the residential
        # country list, so geoCode=in needs super=true (10 credits, not 1).
        name="Scrape.do",
        env="SCRAPEDO_TOKEN",
        endpoint="https://api.scrape.do/",
        params=lambda key, url: {"token": key, "geoCode": "in", "super": "true", "url": url},
        cost_header="Scrape.do-Request-Cost",
        errors={401: "out of credits or account suspended",
                429: "too many parallel requests"},
    ),
    Service(
        # https://docs.scrapingant.com/  browser defaults to true (10 credits);
        # false is the 1-credit plain request. A good answer is always HTTP 200
        # with the page's own status in a header.
        name="ScrapingAnt",
        env="SCRAPINGANT_KEY",
        endpoint="https://api.scrapingant.com/v2/general",
        params=lambda key, url: {
            "x-api-key": key, "proxy_type": "datacenter", "proxy_country": "IN",
            "browser": "false", "url": url,
        },
        cost_header="Ant-credits-cost",
        errors={403: "token rejected or out of credits",
                409: "free plan allows one request at a time",
                422: "token missing or bad parameter",
                423: "site detected the request"},
        status_header="Ant-page-status-code",
    ),
)

# ScraperAPI keeps retrying for up to 70 s before it answers 500, and bills a
# request that is cancelled sooner, so the wait has to outlast that.
SERVICE_TIMEOUT = 75
DIRECT_TIMEOUT = 30  # direct and PROXY_URL requests

_fetch_calls = 0     # fetch() calls so far in this run; with the run number it sets whose turn it is
_failed_routes = {}  # route name -> why it failed; it is skipped for the rest of the run


def reset_fetch_state():
    """Start a new run: the turn counter back to zero, every route allowed again."""
    global _fetch_calls
    _fetch_calls = 0
    _failed_routes.clear()


def _secret(name):
    # GitHub passes an empty string for a secret that was never created.
    return (os.environ.get(name) or "").strip()


def _run_number():
    try:
        return int(os.environ.get("GITHUB_RUN_NUMBER", ""))
    except ValueError:
        return 0  # not running in GitHub Actions


def _scrub(text):
    """Blank out every configured key, token and proxy login in a message."""
    proxy = _secret("PROXY_URL")
    secrets = {_secret(s.env) for s in SERVICES} | {proxy}
    try:
        login = urllib.parse.urlsplit(proxy)
        secrets |= {login.username or "", login.password or ""}
    except ValueError:  # not a URL at all
        pass
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            quoted = {urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
            for form in {secret} | quoted:
                text = text.replace(form, "***")
    return text


def _log(message):
    print("[fetch] " + _scrub(message))


def _ask(service, url):
    """One request to one route. Returns (page text, credits used or None).

    Raises _RouteFailed when the route gave no usable page, and FetchError when
    the page itself does not exist, which no other route would change.
    """
    try:
        if service:
            resp = requests.get(
                service.endpoint,
                params=service.params(_secret(service.env), url),
                timeout=SERVICE_TIMEOUT,
            )
        else:
            proxy = _secret("PROXY_URL")
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=DIRECT_TIMEOUT, proxies=proxies)
    except Exception as exc:  # keep only the class name: the message can hold the key
        raise _RouteFailed(f"network error: {exc.__class__.__name__}") from None

    outer = resp.status_code
    page = outer  # the status BookMyShow itself answered with
    if service and service.status_header:
        # ScrapingAnt: HTTP 200 carries the page's status in a header, any
        # other status is ScrapingAnt's own error.
        page = None
        if outer == 200:
            try:
                page = int(resp.headers.get(service.status_header) or 200)
            except ValueError:
                page = 200
    if page in (404, 410):
        raise FetchError(f"HTTP {page}")
    if page != 200:
        reason = f"HTTP {outer if page is None else page}"
        hint = service.errors.get(outer) if service else None
        raise _RouteFailed(f"{reason} ({hint})" if hint else reason)

    text = resp.text
    # A 200 can still be a bot-check page rather than BookMyShow itself.
    if "bookmyshow" not in text.lower():
        raise _RouteFailed("got a page that is not BookMyShow (likely a bot check)")
    cost = (resp.headers.get(service.cost_header) or "") if service else ""
    return text, cost if re.fullmatch(r"\d+(\.\d+)?", cost) else None


def fetch(url):
    """Fetch a page through an India IP.

    BookMyShow refuses most datacenter and non-India IPs (GitHub's runners
    included). Every scraping service with a secret set takes its turn, one
    step further on for each page and each run, so the load is spread evenly
    without any stored state. A route that fails (error, out of credits, a
    bot-check page) is skipped for the rest of the run and the page is asked
    of the next one; PROXY_URL comes last. With nothing configured the page is
    requested directly, which gets HTTP 403 from GitHub's runners.
    """
    global _fetch_calls
    services = [s for s in SERVICES if _secret(s.env)]
    turn = (_run_number() + _fetch_calls) % len(services) if services else 0
    _fetch_calls += 1
    routes = [(s.name, s) for s in services[turn:] + services[:turn]]
    if _secret("PROXY_URL"):
        routes.append(("PROXY_URL", None))
    elif not routes:
        routes.append(("direct", None))

    page = urllib.parse.urlsplit(url).path
    for name, service in routes:
        if name in _failed_routes:
            continue
        started = time.monotonic()
        try:
            text, credits = _ask(service, url)
        except _RouteFailed as exc:
            _failed_routes[name] = str(exc)
            _log(f"{name} failed: {exc}")
            continue
        except FetchError as exc:  # the page is missing; another route would only cost credits
            _log(f"{name} reports {exc}")
            raise
        took = time.monotonic() - started
        _log(f"{name} ok in {took:.1f}s" + (f", credits {credits}" if credits else "") + f": {page}")
        return text

    reasons = []
    for name, _ in routes:
        if name in _failed_routes:
            why = _failed_routes[name]
            reasons.append(why if name == "direct" else f"{name}: {why}")
    raise FetchError(_scrub("; ".join(reasons)))


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

def send_email(subject, body, urgent=False):
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_APP_PASSWORD")
    to = os.environ.get("ALERT_EMAIL_TO") or user
    if not (user and password and to):
        return None
    msg = EmailMessage()
    msg["Subject"] = f"[URGENT] {subject}" if urgent else subject
    msg["From"] = user
    msg["To"] = to
    if urgent:
        # Mail apps that support message priority, such as Outlook, flag this
        # as high importance. Gmail's own "Important" marker is Google's
        # decision, so on Gmail also use a filter on the subject (see README).
        msg["X-Priority"] = "1 (Highest)"
        msg["X-MSMail-Priority"] = "High"
        msg["Importance"] = "High"
        msg["Priority"] = "urgent"
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)
    return "email"


def send_telegram(subject, body, urgent=False):  # urgent is for email only: Telegram always notifies
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


def send_whatsapp(subject, body, urgent=False):  # urgent is for email only: WhatsApp always notifies
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


def notify(subject, body, urgent=False):
    """Send on every channel whose secrets are set. urgent only changes the
    email (high-priority headers and a [URGENT] subject marker)."""
    sent, failed = [], []
    for sender in (send_email, send_telegram, send_whatsapp):
        try:
            channel = sender(subject, body, urgent)
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
    reset_fetch_state()  # one call of main() is one run
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
                notify(f"Booking open: {name} on {pretty(date)}", body, urgent=True)
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
                "Check the scraping-service secrets and PROXY_URL (see README).",
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
