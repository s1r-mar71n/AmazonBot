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

from watch import (DEFAULT_WAREHOUSE_IDS, DEFAULT_WAREHOUSE_SELLERS, AppleWatch,  # noqa: E402
                   apple_matches, extract_asin, parse_offers, parse_price, parse_refurb)

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


if __name__ == "__main__":
    unittest.main()
