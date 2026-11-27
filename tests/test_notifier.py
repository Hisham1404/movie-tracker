import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifier  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
