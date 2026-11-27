import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import notifier  # noqa: E402

REAL_FETCH = notifier.fetch  # several tests swap notifier.fetch for a fake; this is the real one

OPEN_PAGE = (
    '<html>bookmyshow IMAX EPIQ '
    '<a href="/cinemas/coimbatore/broadway-cinemas-coimbatore/buytickets/BRDW/20261221">Broadway</a>'
    '</html>'
)
FALLBACK_PAGE = (  # asked for the 21st, BookMyShow showed the 19th instead
    '<html>bookmyshow '
    '<a href="/cinemas/coimbatore/broadway-cinemas-coimbatore/buytickets/BRDW/20261219">Broadway</a>'
    '</html>'
)


class Detect(unittest.TestCase):
    def test_open_by_slug(self):
        self.assertEqual(notifier.find_venue_link(OPEN_PAGE, "coimbatore", "20261221", None, "broadway"), "BRDW")

    def test_open_by_code(self):
        self.assertEqual(notifier.find_venue_link(OPEN_PAGE, "coimbatore", "20261221", "BRDW"), "BRDW")

    def test_fallback_date_is_not_open(self):
        self.assertIsNone(notifier.find_venue_link(FALLBACK_PAGE, "coimbatore", "20261221", None, "broadway"))
        self.assertIsNone(notifier.find_venue_link(FALLBACK_PAGE, "coimbatore", "20261221", "BRDW"))

    def test_other_cinema_is_not_open(self):
        page = OPEN_PAGE.replace("broadway-cinemas-coimbatore", "pvr-brookefields")
        self.assertIsNone(notifier.find_venue_link(page, "coimbatore", "20261221", None, "broadway"))

    def test_screens(self):
        self.assertEqual(notifier.screens_seen(OPEN_PAGE, ["EPIQ", "4DX"]), ["EPIQ"])


class Discover(unittest.TestCase):
    def test_prefers_exact_slug(self):
        page = ('bookmyshow <a href="/movies/coimbatore/dune-part-three-hindi/ET00999991">x</a>'
                '<a href="/movies/coimbatore/dune-part-three/ET00999990">y</a>')
        notifier.fetch = lambda url: page
        self.assertEqual(notifier.discover_event_code("coimbatore", "dune-part-three"), "ET00999990")

    def test_not_listed(self):
        notifier.fetch = lambda url: "bookmyshow nothing here"
        self.assertIsNone(notifier.discover_event_code("coimbatore", "dune-part-three"))


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        notifier.STATE_PATH = Path(self.tmp.name) / "state.json"
        notifier.CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
        self.alerts = []
        notifier.notify = lambda s, b: self.alerts.append(s) or ["test"]
        os.environ.pop("TEST_ALERT", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, page_or_exc):
        def fake_fetch(url):
            if isinstance(page_or_exc, Exception):
                raise page_or_exc
            return page_or_exc
        notifier.fetch = fake_fetch
        return notifier.main()

    def test_alert_once_on_opening(self):
        self.run_with(FALLBACK_PAGE)
        self.assertEqual(self.alerts, [])
        self.run_with(OPEN_PAGE)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Avengers: Doomsday", self.alerts[0])
        calls = []
        notifier.fetch = lambda url: calls.append(url) or OPEN_PAGE
        notifier.main()
        self.assertEqual(len(self.alerts), 1)  # no repeat
        self.assertFalse(any("avengers" in u for u in calls))  # open film not re-fetched

    def test_dune_found_and_opened(self):
        explore = 'bookmyshow <a href="/movies/coimbatore/dune-part-three/ET00999990">Dune</a>'
        dune_open = OPEN_PAGE
        def fake_fetch(url):
            if "explore" in url:
                return explore
            if "dune-part-three/buytickets/ET00999990/20261221" in url:
                return dune_open
            return FALLBACK_PAGE
        notifier.fetch = fake_fetch
        notifier.main()
        self.assertEqual(self.alerts, ["Booking open: Dune: Part Three on Mon 21 Dec 2026"])
        state = json.loads(notifier.STATE_PATH.read_text())
        self.assertEqual(state["event_codes"]["dune-part-three"], "ET00999990")

    def test_blocked_warning_after_repeated_failures(self):
        for _ in range(5):
            self.assertEqual(self.run_with(notifier.FetchError("HTTP 403")), 1)
        self.assertEqual(self.alerts, [])
        self.run_with(notifier.FetchError("HTTP 403"))
        self.assertEqual(self.alerts, ["BookMyShow notifier cannot read BookMyShow"])
        self.run_with(notifier.FetchError("HTTP 403"))
        self.assertEqual(len(self.alerts), 1)  # warned once only


# --------------------------------------------------------------------------
# fetch(): the scraping services
# --------------------------------------------------------------------------

KEYS = {
    "SCRAPERAPI_KEY": "sa-key-1111",
    "SCRAPEDO_TOKEN": "do-token-2222",
    "SCRAPINGANT_KEY": "ant-key-3333",
}
PROXY = "http://user:proxy-pass-4444@proxy.example:8080"
PAGE_PATH = "/movies/coimbatore/dune-part-three/buytickets/ET00999990/20261221"
PAGE_URL = "https://in.bookmyshow.com" + PAGE_PATH
SA, DO, ANT = "api.scraperapi.com", "api.scrape.do", "api.scrapingant.com"
DIRECT = "in.bookmyshow.com"  # a direct or PROXY_URL request goes to BookMyShow itself
BMS_PAGE = "<html>bookmyshow</html>"


class FakeResponse:
    def __init__(self, status=200, text=BMS_PAGE, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FetchRouting(unittest.TestCase):
    """Services take turns, fall back to each other, and keep keys out of the log."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        notifier.fetch = REAL_FETCH
        self.calls = []    # (host, query parameters, other arguments) of every outgoing request
        self.answers = {}  # host -> FakeResponse, or an exception to raise; default is a good page
        get = mock.patch.object(notifier.requests, "get", side_effect=self.fake_get)
        get.start()
        self.addCleanup(get.stop)
        self.log = io.StringIO()
        out = contextlib.redirect_stdout(self.log)
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        self.fresh()

    def fake_get(self, endpoint, params=None, **kwargs):
        host = urllib.parse.urlsplit(endpoint).netloc
        self.calls.append((host, params, kwargs))
        answer = self.answers.get(host, FakeResponse())
        if isinstance(answer, Exception):
            raise answer
        return answer

    def fresh(self, *names, **env):
        """A new run (a new process, in real life) with only the named secrets set."""
        for name in (*KEYS, "PROXY_URL", "GITHUB_RUN_NUMBER", "TEST_ALERT"):
            os.environ.pop(name, None)
        for name in names:
            os.environ[name] = KEYS.get(name, PROXY)
        os.environ.update(env)
        notifier.reset_fetch_state()
        self.calls.clear()
        self.answers.clear()

    def hosts(self):
        return [host for host, _, _ in self.calls]

    def test_nothing_configured_asks_bookmyshow_directly(self):
        self.assertEqual(notifier.fetch(PAGE_URL), BMS_PAGE)
        (host, _, extra), = self.calls
        self.assertEqual(host, DIRECT)
        self.assertIsNone(extra["proxies"])

    def test_each_service_gets_the_request_its_docs_describe(self):
        for name, host, expected in (
            ("SCRAPERAPI_KEY", SA, {"api_key": KEYS["SCRAPERAPI_KEY"], "country_code": "in"}),
            ("SCRAPEDO_TOKEN", DO, {"token": KEYS["SCRAPEDO_TOKEN"], "geoCode": "in", "super": "true"}),
            ("SCRAPINGANT_KEY", ANT, {"x-api-key": KEYS["SCRAPINGANT_KEY"], "proxy_type": "datacenter",
                                      "proxy_country": "IN", "browser": "false"}),
        ):
            with self.subTest(host):
                self.fresh(name)
                notifier.fetch(PAGE_URL)
                (called, params, _), = self.calls
                self.assertEqual(called, host)
                self.assertEqual(params, {**expected, "url": PAGE_URL})
                self.assertEqual(list(params)[-1], "url")  # ScraperAPI wants its parameters before url

    def test_services_without_a_secret_are_skipped(self):
        self.fresh("SCRAPERAPI_KEY")
        os.environ["SCRAPEDO_TOKEN"] = ""  # what GitHub passes for a secret that was never created
        os.environ["SCRAPINGANT_KEY"] = " \n"
        for _ in range(3):
            notifier.fetch(PAGE_URL)
        self.assertEqual(self.hosts(), [SA, SA, SA])

    def test_pages_take_turns(self):
        self.fresh(*KEYS)
        for _ in range(6):
            notifier.fetch(PAGE_URL)
        self.assertEqual(self.hosts(), [SA, DO, ANT, SA, DO, ANT])

    def test_the_turn_carries_on_from_run_to_run(self):
        # Every Actions run is a new process, but GitHub numbers the runs.
        used = []
        for run in (1, 2, 3):
            self.fresh(*KEYS, GITHUB_RUN_NUMBER=str(run))
            notifier.fetch(PAGE_URL)
            notifier.fetch(PAGE_URL)  # a run fetches about two pages
            used += self.hosts()
        self.assertEqual(Counter(used), {SA: 2, DO: 2, ANT: 2})

    def test_main_hands_each_run_to_the_next_service(self):
        config = {
            "city": "coimbatore",
            "venue": {"label": "Broadway", "code": "", "slug_hint": "broadway"},
            "watches": [
                {"film": "A", "slug": "a", "event_code": "ET1", "dates": ["20261221"]},
                {"film": "B", "slug": "b", "event_code": "ET2", "dates": ["20261221"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config))
            with mock.patch.object(notifier, "CONFIG_PATH", config_path), \
                    mock.patch.object(notifier, "STATE_PATH", Path(tmp) / "state.json"), \
                    mock.patch.object(notifier, "notify", return_value=["test"]):
                self.fresh(*KEYS)
                for run in (1, 2):
                    os.environ["GITHUB_RUN_NUMBER"] = str(run)
                    self.assertEqual(notifier.main(), 0)
        self.assertEqual(self.hosts(), [DO, ANT, ANT, SA])  # two pages a run, one step on each time

    def test_a_failing_service_hands_over_to_the_next(self):
        self.fresh(*KEYS)
        self.answers[SA] = FakeResponse(403)
        self.assertEqual(notifier.fetch(PAGE_URL), BMS_PAGE)
        self.assertEqual(self.hosts(), [SA, DO])
        self.assertIn("ScraperAPI failed: HTTP 403 (out of credits)", self.log.getvalue())

    def test_every_kind_of_bad_answer_falls_back(self):
        for label, bad in (
            ("timeout", notifier.requests.Timeout("read timed out")),
            ("server error", FakeResponse(500)),
            ("too many requests", FakeResponse(429)),
            ("bot-check page", FakeResponse(200, "<html>please verify you are human</html>")),
        ):
            with self.subTest(label):
                self.fresh(*KEYS)
                self.answers[SA] = bad
                self.assertEqual(notifier.fetch(PAGE_URL), BMS_PAGE)
                self.assertEqual(self.hosts(), [SA, DO])

    def test_a_failed_service_is_skipped_for_the_rest_of_the_run(self):
        self.fresh(*KEYS)
        self.answers[SA] = FakeResponse(500)
        for _ in range(4):
            notifier.fetch(PAGE_URL)
        # ScraperAPI fails on the first page and is not asked again, even on its next turn
        self.assertEqual(self.hosts(), [SA, DO, DO, ANT, DO])

    def test_proxy_url_is_the_last_resort(self):
        self.fresh(*KEYS, "PROXY_URL")
        notifier.fetch(PAGE_URL)
        self.assertEqual(self.hosts(), [SA])  # a service worked, so the proxy was left alone
        self.fresh(*KEYS, "PROXY_URL")
        self.answers.update({SA: FakeResponse(500), DO: FakeResponse(401), ANT: FakeResponse(403)})
        self.assertEqual(notifier.fetch(PAGE_URL), BMS_PAGE)
        self.assertEqual(self.hosts(), [SA, DO, ANT, DIRECT])
        self.assertEqual(self.calls[-1][2]["proxies"], {"http": PROXY, "https": PROXY})

    def test_when_everything_fails_the_error_names_each_route(self):
        self.fresh(*KEYS, "PROXY_URL")
        leaky = "Max retries exceeded with url: /?api_key=" + KEYS["SCRAPEDO_TOKEN"]
        self.answers.update({
            SA: FakeResponse(403),
            DO: notifier.requests.ConnectTimeout(leaky),
            ANT: FakeResponse(423),
            DIRECT: FakeResponse(403),
        })
        with self.assertRaises(notifier.FetchError) as caught:
            notifier.fetch(PAGE_URL)
        message = str(caught.exception)
        for part in ("ScraperAPI: HTTP 403 (out of credits)", "Scrape.do: network error: ConnectTimeout",
                     "ScrapingAnt: HTTP 423 (site detected the request)", "PROXY_URL: HTTP 403"):
            self.assertIn(part, message)
        for secret in (*KEYS.values(), "proxy-pass-4444"):
            self.assertNotIn(secret, message)
        sent = len(self.calls)
        with self.assertRaises(notifier.FetchError):  # nothing left to try for the rest of the run
            notifier.fetch(PAGE_URL)
        self.assertEqual(len(self.calls), sent)

    def test_a_missing_page_is_final(self):
        self.fresh(*KEYS)
        self.answers[SA] = FakeResponse(404)
        with self.assertRaises(notifier.FetchError) as caught:
            notifier.fetch(PAGE_URL)
        self.assertEqual(str(caught.exception), "HTTP 404")
        self.assertEqual(self.hosts(), [SA])  # another service would only cost more credits

    def test_scrapingant_reports_the_pages_own_status_in_a_header(self):
        ant_first = {"GITHUB_RUN_NUMBER": "2"}
        # BookMyShow refused: a block page that still mentions the site name
        self.fresh(*KEYS, **ant_first)
        self.answers[ANT] = FakeResponse(200, "<html>Access Denied bookmyshow</html>",
                                         {"Ant-page-status-code": "403"})
        self.assertEqual(notifier.fetch(PAGE_URL), BMS_PAGE)
        self.assertEqual(self.hosts(), [ANT, SA])
        self.assertIn("ScrapingAnt failed: HTTP 403", self.log.getvalue())
        # the page does not exist
        self.fresh(*KEYS, **ant_first)
        self.answers[ANT] = FakeResponse(200, BMS_PAGE, {"Ant-page-status-code": "404"})
        with self.assertRaises(notifier.FetchError) as caught:
            notifier.fetch(PAGE_URL)
        self.assertEqual(str(caught.exception), "HTTP 404")
        self.assertEqual(self.hosts(), [ANT])
        # ScrapingAnt's own 404 means "site can't be reached", not "page missing"
        self.fresh(*KEYS, **ant_first)
        self.answers[ANT] = FakeResponse(404, "This site can't be reached")
        self.assertEqual(notifier.fetch(PAGE_URL), BMS_PAGE)
        self.assertEqual(self.hosts(), [ANT, SA])

    def test_log_names_the_service_and_its_cost_and_never_a_key(self):
        self.fresh(*KEYS, "PROXY_URL")
        self.answers[SA] = notifier.requests.ConnectionError(
            "Max retries exceeded with url: /?api_key=" + KEYS["SCRAPERAPI_KEY"] + "&url=x")
        self.answers[DO] = FakeResponse(200, BMS_PAGE, {"Scrape.do-Request-Cost": "10"})
        notifier.fetch(PAGE_URL)
        log = self.log.getvalue()
        self.assertIn("ScraperAPI failed: network error: ConnectionError", log)
        self.assertIn("Scrape.do ok", log)
        self.assertIn("credits 10", log)
        self.assertIn(PAGE_PATH, log)
        self.assertNotIn("api_key", log)
        for secret in (*KEYS.values(), "proxy-pass-4444", PROXY):
            self.assertNotIn(secret, log)

    def test_a_cost_header_that_is_not_a_number_is_not_logged(self):
        self.fresh("SCRAPEDO_TOKEN")
        self.answers[DO] = FakeResponse(200, BMS_PAGE, {"Scrape.do-Request-Cost": "see invoice"})
        notifier.fetch(PAGE_URL)
        self.assertNotIn("credits", self.log.getvalue())

    def test_scrub_hides_keys_and_the_proxy_login_in_any_form(self):
        self.fresh(*KEYS, "PROXY_URL")
        odd = "tok/en+with&odd=chars"
        os.environ["SCRAPEDO_TOKEN"] = odd
        forms = [KEYS["SCRAPERAPI_KEY"], KEYS["SCRAPINGANT_KEY"], odd, urllib.parse.quote(odd, safe=""),
                 PROXY, "proxy-pass-4444"]
        clean = notifier._scrub(" | ".join(forms))
        for form in forms:
            self.assertNotIn(form, clean)
        self.assertEqual(notifier._scrub("nothing secret here"), "nothing secret here")


class Wiring(unittest.TestCase):
    def test_every_secret_reaches_the_job_and_is_documented(self):
        # A secret that is missing from the workflow's env is silently never used.
        workflow = (ROOT / ".github" / "workflows" / "watch.yml").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in [service.env for service in notifier.SERVICES] + ["PROXY_URL"]:
            with self.subTest(name):
                self.assertIn("%s: ${{ secrets.%s }}" % (name, name), workflow)
                self.assertIn("`%s`" % name, readme)


if __name__ == "__main__":
    unittest.main()
