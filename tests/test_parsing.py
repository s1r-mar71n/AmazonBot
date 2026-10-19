"""Unit tests for the parsing helpers.

The fixtures in tests/fixtures/ are real responses captured from amazon.de and apple.com/de on
2026-10-14 and 2026-10-19 (gzipped; anonymous-session CSRF and challenge tokens replaced with REDACTED). If a site changes its
markup, capture a fresh page into a new fixture rather than hand-editing these."""

import gzip
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
from unittest import mock  # noqa: E402

import watch  # noqa: E402
from watch import (DEFAULT_WAREHOUSE_IDS, DEFAULT_WAREHOUSE_SELLERS, AppleWatch, ShopWatch,  # noqa: E402
                   apple_matches, extract_asin, parse_offers, parse_price, parse_refurb, parse_shop,
                   shop_feed_url, shop_matches)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    with gzip.open(FIXTURES / name, "rt", encoding="utf-8") as f:
        return f.read()


def offers_of(name: str, wh_names=DEFAULT_WAREHOUSE_SELLERS, wh_ids=DEFAULT_WAREHOUSE_IDS):
    return parse_offers(fixture(name), wh_names, wh_ids)


class ParsePrice(unittest.TestCase):
    def test_formats(self):
        cases = {
            "1.234,56 €": 1234.56, "€1,234.56": 1234.56, "899,00 €": 899.0,
            "EUR 49,9": 49.9, "1.234": 1234.0, "12,–": 12.0, "$5": 5.0,
            "": None, "€": None,
        }
        for text, want in cases.items():
            self.assertEqual(parse_price(text), want, text)


class ExtractAsin(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(extract_asin("https://www.amazon.de/MSI-RTX/dp/B08LNPPCWJ?th=1"), "B08LNPPCWJ")
        self.assertEqual(extract_asin("https://www.amazon.de/gp/product/B08164VTWH/ref=x"), "B08164VTWH")
        self.assertEqual(extract_asin("https://www.amazon.de/dp/B08164VTWH"), "B08164VTWH")
        self.assertIsNone(extract_asin("https://www.amazon.de/s?k=gpu"))


class ParseOffers(unittest.TestCase):
    def test_warehouse_offers(self):
        # DJI Osmo Mobile 8: Amazon new, four Amazon Retourenkauf (Warehouse) offers, Amazon UK new.
        title, offers = offers_of("amazon_aod_warehouse_B0GLPQCR51.html.gz")
        self.assertTrue(title.startswith("DJI Osmo Mobile 8"))
        self.assertEqual([(o.price, o.condition, o.seller, o.kind) for o in offers], [
            (89.75, "Neu", "Amazon", "new"),
            (72.4, "Gebraucht - Akzeptabel", "Amazon Retourenkauf", "warehouse"),
            (79.13, "Gebraucht - Gut", "Amazon Retourenkauf", "warehouse"),
            (80.81, "Gebraucht - Sehr gut", "Amazon Retourenkauf", "warehouse"),
            (83.34, "Gebraucht - Wie neu", "Amazon Retourenkauf", "warehouse"),
            (108.06, "Neu", "Amazon UK", "new"),
        ])
        self.assertEqual(offers[-1].seller_id, "A301WKE65PGVT5")

    def test_warehouse_found_by_link_alone(self):
        # Warehouse offers link to /Warehouse-Deals/ and carry no seller id. Even if Amazon renames
        # "Retourenkauf" again (it was "Warehouse Deals", then "Resale"), the link still identifies them.
        _, offers = offers_of("amazon_aod_warehouse_B0GLPQCR51.html.gz", wh_names=[], wh_ids=[])
        self.assertEqual([o.kind for o in offers].count("warehouse"), 4)

    def test_mac_studio_no_warehouse(self):
        # Struck-through UVP 3.024,20 must be ignored; the "mit 9 Prozent Einsparungen" label must not
        # leak into the price; the seller column's "Rücknahmerichtlinien" link is not the seller.
        title, offers = offers_of("amazon_aod_mac_studio_B0HGS3C159.html.gz")
        self.assertTrue(title.startswith("Apple Mac Studio mit Apple M5 Max Chip"))
        self.assertEqual([(o.price, o.seller, o.seller_id, o.kind) for o in offers], [
            (2747.9, "Amazon", "", "new"),
            (2933.45, "Office Partner GmbH", "A12CZU1FA165ZY", "new"),
        ])


# Mac Studio was sold out, so Apple served the whole refurbished-Mac category on that URL.
APPLE_PAGE = "https://www.apple.com/de/shop/refurbished/mac"


class ParseRefurb(unittest.TestCase):
    def setUp(self):
        self.html = fixture("apple_refurb_mac_studio_soldout.html.gz")

    def test_grid(self):
        items = parse_refurb(self.html, APPLE_PAGE)
        self.assertEqual(len(items), 132)
        self.assertTrue(all(i.price for i in items))
        mini = next(i for i in items if i.part == "fu9e3d")
        self.assertEqual((mini.price, mini.previous_price), (989.0, 1169.0))
        self.assertTrue(mini.url.startswith("https://www.apple.com/de/shop/product/fu9e3d/"))
        self.assertNotIn("?", mini.url)

    def test_matching(self):
        items = parse_refurb(self.html, APPLE_PAGE)
        match = lambda *a: sorted(i.part for i in items if apple_matches(i, AppleWatch(APPLE_PAGE, *a)))
        # "Studio Display" titles must not count as a Mac Studio.
        self.assertEqual(match(["Mac Studio"], [], None), [])
        # Apple writes "Mac\xa0mini" (no-break space): plain "Mac mini" must still match.
        self.assertEqual(match(["Mac mini"], [], None), ["fcx44d", "fcyt4d", "fu9e3d", "g1cf8d"])
        self.assertEqual(match(["Mac mini"], [], 1000), ["fu9e3d"])
        self.assertEqual(match(["Mac mini"], ["M4 Pro"], None), ["fcyt4d", "fu9e3d", "g1cf8d"])

    def test_json_fallback(self):
        # Same real page with the no-JS grid removed: the embedded REFURB_GRID_BOOTSTRAP JSON must
        # yield the same listings.
        no_grid = re.sub(r'class="(refurbished|rf-refurb)-category-grid-no-js', 'class="x', self.html)
        grid = {(i.part, i.price, i.previous_price) for i in parse_refurb(self.html, APPLE_PAGE)}
        json_items = parse_refurb(no_grid, APPLE_PAGE)
        self.assertEqual(len(json_items), 132)
        self.assertEqual({(i.part, i.price, i.previous_price) for i in json_items}, grid)


# MSI's Shopify store. The store fixture is a real subset of the store-wide products.json:
# every graphics card (product_type VGA) plus everything else with "5090" in the title,
# with product descriptions (body_html) emptied to keep it small.
MSI = "https://de-store.msi.com"
MSI_COLLECTION = MSI + "/collections/geforce-rtx-50-series-gpu?sort_by=price-descending"
REFURB_5090 = "de-store.msi.com/msi-grafikkarte-geforce-rtx-5090-32g-suprim-liquid-soc_erc/"


def msi_store() -> dict:
    return json.loads(fixture("msi_store_vga_and_5090_20261014.json.gz"))


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class Shopify(unittest.TestCase):
    def test_feed_url(self):
        self.assertEqual(shop_feed_url(MSI_COLLECTION, 1),
                         MSI + "/collections/geforce-rtx-50-series-gpu/products.json?limit=250&page=1")
        self.assertEqual(shop_feed_url(MSI + "/", 3), MSI + "/products.json?limit=250&page=3")

    def test_collection(self):
        # On 2026-10-14 the RTX 50 collection listed 14 products, all in stock, no 5090.
        items = parse_shop(json.loads(fixture("msi_collection_rtx50_20261014.json.gz")), MSI_COLLECTION)
        self.assertEqual(len(items), 14)
        self.assertTrue(all(i.available for i in items))
        five080 = next(i for i in items if i.key.startswith("de-store.msi.com/geforce-rtx-5080-16g-ventus-3x-oc-black/"))
        self.assertEqual((five080.price, five080.product_type), (1819.0, "VGA"))
        self.assertEqual(five080.url, MSI + "/products/geforce-rtx-5080-16g-ventus-3x-oc-black")
        self.assertFalse([i for i in items if shop_matches(i, ShopWatch(MSI, ["5090"], [], ["VGA"], None, None))])

    def test_store_5090_filter(self):
        # Only the (sold-out) refurbished 5090 card matches; laptops/desktops with an
        # "RTX 5090 Laptop GPU" are excluded by types = ["VGA"].
        items = parse_shop(msi_store(), MSI)
        five090 = [i for i in items if "5090" in i.title]
        self.assertGreater(len(five090), 1)
        hits = [i for i in items if shop_matches(i, ShopWatch(MSI, ["5090"], [], ["VGA"], None, None))]
        self.assertEqual([(i.price, i.available) for i in hits], [(6499.0, False)])
        self.assertTrue(hits[0].key.startswith(REFURB_5090))

    def _round(self, state, data, watches=None):
        watches = watches or [ShopWatch(MSI, ["5090"], [], ["VGA"], None, None)]
        cfg = {"shop_watches": watches, "settings": {"shop_interval_minutes": 1}}
        session = mock.Mock()
        session.get.return_value = FakeResponse(data)
        with mock.patch.object(watch, "notify") as notify, mock.patch.object(watch.time, "sleep"):
            ok = watch.shop_round(session, cfg, state, False, watches)
        self.last_priorities = [c.kwargs.get("priority") for c in notify.call_args_list]
        return ok, [c.args[1] for c in notify.call_args_list]

    @staticmethod
    def _5090_in_stock(data, available=True):
        for p in data["products"]:
            if "5090" in p["title"] and p["product_type"] == "VGA":
                for v in p["variants"]:
                    v["available"] = available
        return data

    def test_alerts_when_5090_comes_in_stock(self):
        sold_out = msi_store()
        # The only real 5090 card was sold out; flip it to in stock (the one edit to real data).
        in_stock = msi_store()
        for p in in_stock["products"]:
            if "5090" in p["title"] and p["product_type"] == "VGA":
                for v in p["variants"]:
                    v["available"] = True
        state = {}
        self.assertEqual(self._round(state, sold_out), (True, []))  # sold out: quiet
        ok, alerts = self._round(state, in_stock)
        self.assertTrue(ok)
        self.assertEqual(len(alerts), 1)
        self.assertIn("RTX 5090", alerts[0])
        self.assertEqual(self._round(state, in_stock)[1], [])  # still in stock: no repeat
        self._round(state, sold_out)  # sells out ...
        self.assertEqual(len(self._round(state, in_stock)[1]), 1)  # ... and is back: alert again

    def test_same_variant_through_two_feeds_alerts_once(self):
        in_stock = msi_store()
        for p in in_stock["products"]:
            for v in p["variants"]:
                v["available"] = True
        watches = [ShopWatch(MSI + "/collections/geforce-rtx-5090-series-gpu", ["5090"], [], ["VGA"], None, 1),
                   ShopWatch(MSI, ["5090"], [], ["VGA"], None, 10)]
        self.assertEqual(len(self._round({}, in_stock, watches)[1]), 1)

    def test_quiet_alert_when_listed_sold_out(self):
        # The real refurbished 5090 is listed and sold out: one quiet alert, then nothing.
        watches = [ShopWatch(MSI, ["5090"], [], ["VGA"], None, 10, sold_out_alerts=True)]
        state = {}
        ok, alerts = self._round(state, msi_store(), watches)
        self.assertTrue(ok)
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].startswith("Listed, sold out: MSI Grafikkarte GeForce RTX 5090"))
        self.assertEqual(self.last_priorities, [2])  # < 3: Telegram delivers it silently
        self.assertEqual(self._round(state, msi_store(), watches)[1], [])
        # Comes into stock: the loud alert. Sells out again: no new quiet alert (not a new listing).
        self.assertEqual(len(self._round(state, self._5090_in_stock(msi_store()), watches)[1]), 1)
        self.assertEqual(self.last_priorities, [5])
        self.assertEqual(self._round(state, msi_store(), watches)[1], [])

    def test_no_sold_out_alert_without_option(self):
        self.assertEqual(self._round({}, msi_store())[1], [])

    def test_blocked(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse({}, status=430)
        cfg = {"shop_watches": [], "settings": {"shop_interval_minutes": 1}}
        w = ShopWatch(MSI, ["5090"], [], ["VGA"], None, None)
        with mock.patch.object(watch, "notify"), mock.patch.object(watch.time, "sleep"):
            self.assertFalse(watch.shop_round(session, cfg, {}, False, [w]))


class BlockNotice(unittest.TestCase):
    def test_apple_503_warns_once_then_all_clear(self):
        refused = mock.Mock(status_code=503, text="")  # body content isn't known; only the status matters
        session = mock.Mock(get=mock.Mock(return_value=refused))
        cfg = {"apple_watches": [AppleWatch("https://www.apple.com/de/shop/refurbished/mac/mac-studio",
                                            ["Mac Studio"], [], None)]}
        state = {}
        with mock.patch.object(watch, "notify") as notify, mock.patch.object(watch.time, "sleep"):
            for _ in range(3):  # stays blocked for three rounds: one warning
                self.assertFalse(watch.apple_round(session, cfg, state, False))
                watch._block_notice(cfg, state, "apple", True, 900, False)
            watch._block_notice(cfg, state, "apple", False, 0, False)  # works again
            watch._block_notice(cfg, state, "apple", False, 0, False)  # still fine: nothing more
        (warn, _), (clear, _) = [(c.args, c.kwargs) for c in notify.call_args_list]
        self.assertIn("HTTP 503", warn[2])
        self.assertIn("back to normal", clear[1])
        self.assertEqual(notify.call_args_list[1].kwargs["priority"], 2)  # quiet
        self.assertNotIn("apple_block_notified", state)


class Rejection(unittest.TestCase):
    """Real pages: offer lists must pass; challenges, CAPTCHAs and errors must not."""

    def test_offer_lists_pass(self):
        for name in ("amazon_aod_rtx5080_warehouse_B0H86338KX.html.gz", "amazon_aod_warehouse_B0GLPQCR51.html.gz",
                     "amazon_aod_mac_studio_B0HGS3C159.html.gz"):
            self.assertIsNone(watch.rejection(200, {}, fixture(name)), name)

    def test_challenges_are_refusals(self):
        waf = fixture("amazon_waf_challenge_home_202.html.gz")
        self.assertEqual(watch.rejection(202, {"x-amzn-waf-action": "challenge"}, waf),
                         "AWS WAF challenge (HTTP 202)")
        self.assertEqual(watch.rejection(202, {}, waf), "bot challenge page (HTTP 202)")  # even without the header
        akamai = fixture("amazon_akamai_interstitial_search.html.gz")
        self.assertEqual(watch.rejection(200, {}, akamai), "bot challenge page (HTTP 200)")

    def test_errors(self):
        self.assertEqual(watch.rejection(404, {}, fixture("amazon_aod_unknown_asin_404.html.gz")), "HTTP 404")
        self.assertEqual(watch.rejection(503, {}, ""), "HTTP 503")
        self.assertEqual(watch.rejection(302, {}, ""), "HTTP 302")  # redirects aren't followed
        # A 200 that isn't an offer list (here: Amazon's not-found page) is a refusal too.
        self.assertTrue(watch.rejection(200, {}, fixture("amazon_aod_unknown_asin_404.html.gz"))
                        .startswith("not an offer list"))

    def test_rtx5080_warehouse_offer(self):
        title, offers = offers_of("amazon_aod_rtx5080_warehouse_B0H86338KX.html.gz")
        self.assertIn("RTX 5080", title)
        wh = [o for o in offers if o.kind == "warehouse"]
        self.assertEqual([(o.price, o.seller) for o in wh], [(1908.43, "Amazon Retourenkauf")])


class ProxyFleetTest(unittest.TestCase):
    URLS = ["https://www.amazon.de/dp/B0H86338KX", "https://www.amazon.de/dp/B0DS65VMC6"]

    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        cfg_path = Path(self.dir.name) / "config.toml"
        cfg_path.write_text(f"""
[telegram]
bot_token = "x"
chat_id = "1"
[proxies]
list_file = "unused"
per_proxy_interval_minutes = 10
breaker_halts = 2
[[product]]
urls = {json.dumps(self.URLS)}
max_warehouse = 1400
proxy = true
""")
        self.cfg = watch.load_config(cfg_path)
        self.clock = [1_000_000.0]
        self.fleet = watch.ProxyFleet("de-DE")
        self.fleet.proxies = watch.parse_proxy_list("10.0.0.1:4444:u:pw1\n10.0.0.2:4444:u:pw2\n10.0.0.3:4444:u:pw3\n")
        self.fleet.loaded_at = self.clock[0]
        self.state = {}
        self.used = []  # (ip, asin) per request

    def tearDown(self):
        self.dir.cleanup()

    def step(self, response, wait=True):
        """One proxy_step, by default at the moment the fleet asks for. Returns the notification titles."""
        if wait:
            self.clock[0] = max(self.clock[0], self.fleet.next_request)

        def get_offers(p, host, asin):
            self.used.append((p.ip, asin))
            return response
        self.fleet.get_offers = get_offers
        with mock.patch.object(watch, "notify") as notify, mock.patch.object(watch.time, "time", lambda: self.clock[0]):
            watch.proxy_step(self.fleet, self.cfg, self.state, False)
        return [c.args[1] for c in notify.call_args_list]

    def ok(self):
        return mock.Mock(status_code=200, headers={}, text=fixture("amazon_aod_rtx5080_warehouse_B0H86338KX.html.gz"))

    def waf(self):
        return mock.Mock(status_code=202, headers={"x-amzn-waf-action": "challenge"},
                         text=fixture("amazon_waf_challenge_home_202.html.gz"))

    def test_config(self):
        self.assertEqual([p.url for p in self.cfg["proxy_products"]], self.URLS)
        self.assertEqual(self.cfg["products"], [])

    def test_spreads_over_proxies_and_products(self):
        for _ in range(3):
            self.assertEqual(self.step(self.ok()), [])  # 1908.43 is above 1400: no alert
        self.assertEqual(len({ip for ip, _ in self.used}), 3)  # each proxy once
        self.assertEqual({a for _, a in self.used}, {"B0H86338KX", "B0DS65VMC6"})
        # 10 min per proxy over 3 proxies: about one request every 200 s
        self.assertAlmostEqual(self.fleet.next_request - self.clock[0], 200, delta=41)

    def test_refusal_halts_proxy_and_breaker_pauses_all(self):
        titles = self.step(self.waf())
        first = self.used[0][0]
        self.assertEqual(titles, [f"Amazon refused proxy {first}"])
        halt = self.state["proxy_halts"][first]
        self.assertEqual(halt["reason"], "AWS WAF challenge (HTTP 202)")
        self.assertEqual(halt["until"] - halt["since"], 24 * 3600)
        self.step(self.ok())
        self.assertNotEqual(self.used[1][0], first)  # the halted proxy isn't used
        titles = self.step(self.waf())  # a second refusal within 6 h trips the breaker (breaker_halts = 2)
        self.assertEqual(titles[-1], "Proxy watch paused")
        n = len(self.used)
        self.clock[0] += 3600
        self.step(self.ok(), wait=False)  # an hour later, still paused: no request
        self.assertEqual(len(self.used), n)
        self.assertAlmostEqual(self.fleet.next_request - self.clock[0], 11 * 3600, delta=1)  # 12 h pause

    def test_halted_proxy_comes_back_with_a_quiet_notice(self):
        self.step(self.waf())
        first = self.used[0][0]
        self.state["proxy_halts"][first]["until"] = self.clock[0]  # halt over
        titles = []
        for _ in range(6):  # the others first, then its 10 min gap is over
            titles += self.step(self.ok())
        self.assertIn(f"Proxy {first} back in use", titles)
        self.assertTrue(self.state["proxy_halts"][first]["cleared"])

    def test_repeat_refusal_doubles_the_halt(self):
        self.step(self.waf())
        first = self.used[0][0]
        self.state["proxy_halts"][first].update(until=self.clock[0], cleared=True)
        self.state["proxy_halts"][first]["since"] -= 7 * 3600  # outside the breaker window
        self.fleet.last_used = {p.ip: self.clock[0] for p in self.fleet.proxies}
        self.fleet.last_used[first] = 0  # make it the next one picked
        self.clock[0] += 600
        self.step(self.waf())
        self.assertEqual(self.used[-1][0], first)
        h = self.state["proxy_halts"][first]
        self.assertEqual((h["count"], h["until"] - h["since"]), (2, 48 * 3600))

    def test_unknown_asin_is_not_a_refusal(self):
        r = mock.Mock(status_code=404, headers={}, text=fixture("amazon_aod_unknown_asin_404.html.gz"))
        titles = self.step(r)
        self.assertEqual(titles, [f"Product not found: {self.used[0][1]}"])
        self.assertEqual(self.state["proxy_halts"], {})

    def test_proxy_failure_benches_without_alert(self):
        def boom(p, host, asin):
            raise ConnectionError(f"proxy {p.user}:{p.password} unreachable")
        self.fleet.get_offers = boom
        with mock.patch.object(watch, "notify") as notify, mock.patch.object(watch.time, "time", lambda: self.clock[0]), \
                self.assertLogs("watch", "WARNING") as logs:
            watch.proxy_step(self.fleet, self.cfg, self.state, False)
        notify.assert_not_called()
        self.assertEqual(len(self.fleet.benched), 1)
        self.assertNotIn("pw", " ".join(logs.output))  # credentials scrubbed


if __name__ == "__main__":
    unittest.main()
