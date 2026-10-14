"""Unit tests for the parsing helpers.

The fixtures in tests/fixtures/ are real responses captured from amazon.de and apple.com/de on
2026-10-14 (gzipped; anonymous-session CSRF tokens replaced with REDACTED). If a site changes its
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
        return ok, [c.args[1] for c in notify.call_args_list]

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

    def test_blocked(self):
        session = mock.Mock()
        session.get.return_value = FakeResponse({}, status=430)
        cfg = {"shop_watches": [], "settings": {"shop_interval_minutes": 1}}
        w = ShopWatch(MSI, ["5090"], [], ["VGA"], None, None)
        with mock.patch.object(watch, "notify"), mock.patch.object(watch.time, "sleep"):
            self.assertFalse(watch.shop_round(session, cfg, {}, False, [w]))


if __name__ == "__main__":
    unittest.main()
