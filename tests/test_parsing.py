"""Unit tests for the parsing helpers.

The AOD snippet below is hand-written to mirror Amazon's offer-list markup;
it checks our parsing logic, not that Amazon still serves this exact markup.
The Apple snippets follow the selectors used by the `refurbished` PyPI package."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watch import (DEFAULT_WAREHOUSE_IDS, DEFAULT_WAREHOUSE_SELLERS, AppleWatch,  # noqa: E402
                   apple_matches, extract_asin, parse_offers, parse_price, parse_refurb)

AOD = """
<div id="aod-asin-title-text">Some Graphics Card 16GB</div>
<div id="aod-pinned-offer">
  <span class="a-price"><span class="a-offscreen">1.049,00&nbsp;€</span></span>
  <div id="aod-offer-heading"><h5>Neu</h5></div>
  <div id="aod-offer-soldBy"><div class="a-col-right"><span>Amazon.de</span></div></div>
</div>
<div id="aod-offer">
  <span class="a-price"><span class="a-price-whole">899,</span><span class="a-price-fraction">90</span></span>
  <div id="aod-offer-heading"><h5>Gebraucht - Wie neu</h5></div>
  <div id="aod-offer-soldBy"><a href="/gp/aag/main?seller=A8KICS1PHF7ZO&isAmazonFulfilled=1">Amazon Warehouse</a></div>
</div>
<div id="aod-offer">
  <span class="a-price"><span class="a-offscreen">999,00 €</span></span>
  <div id="aod-offer-heading"><h5>Neu</h5></div>
  <div id="aod-offer-soldBy"><a href="/gp/aag/main?seller=AXYZ123">Some Shop</a></div>
</div>
<div id="aod-offer">
  <span class="a-price"><span class="a-offscreen">700,00 €</span></span>
  <div id="aod-offer-heading"><h5>Gebraucht - Gut</h5></div>
  <div id="aod-offer-soldBy"><a href="/gp/aag/main?seller=AOTHER">Random Reseller</a></div>
</div>
"""


class ParsePrice(unittest.TestCase):
    def test_formats(self):
        cases = {
            "1.234,56 €": 1234.56, "€1,234.56": 1234.56, "899,00 €": 899.0,
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
    def test_classification(self):
        title, offers = parse_offers(AOD, DEFAULT_WAREHOUSE_SELLERS, DEFAULT_WAREHOUSE_IDS)
        self.assertEqual(title, "Some Graphics Card 16GB")
        got = [(o.price, o.kind) for o in offers]
        self.assertEqual(got, [(1049.0, "new"), (899.9, "warehouse"), (999.0, "new"), (700.0, "other")])


# Mac Studio has no stock, so Apple shows the whole Mac category instead.
REFURB_GRID = """
<link rel="canonical" href="https://www.apple.com/de/shop/refurbished/mac">
<div class="refurbished-category-grid-no-js"><ul>
  <li><h3><a href="/de/shop/product/FXNE3D/A/refurb-mac-mini-apple-m4-chip?fnode=abc">Mac mini mit Apple M4 Chip mit 10‑Core CPU (generalüberholt)</a></h3>
      <div><span class="as-price-currentprice">599,00&nbsp;€</span>
           <span class="as-price-previousprice">Ursprünglich 699,00 €</span></div></li>
  <li><h3><a href="/de/shop/product/FXCR3D/A/refurb-macbook-air">13" MacBook Air mit Apple M3 Chip (generalüberholt)</a></h3>
      <div>1.049,00 €</div></li>
</ul></div>
"""

REFURB_JSON = """<script>window.REFURB_GRID_BOOTSTRAP = {"tiles": [
 {"title": "Mac Studio mit Apple M4 Max Chip (generalüberholt)",
  "productDetailsUrl": "/de/shop/product/FX123D/A/refurb-mac-studio",
  "price": {"currentPrice": {"amount": "1.869,00 €", "raw_amount": "1869.00"},
            "previousPrice": {"amount": "2.199,00 €", "raw_amount": "2199.00"}}}]};</script>"""

PAGE = "https://www.apple.com/de/shop/refurbished/mac/mac-studio"


class ParseRefurb(unittest.TestCase):
    def test_grid(self):
        items = parse_refurb(REFURB_GRID, PAGE)
        self.assertEqual([(i.part, i.price, i.previous_price) for i in items],
                         [("FXNE3D", 599.0, 699.0), ("FXCR3D", 1049.0, None)])
        self.assertEqual(items[0].url, "https://www.apple.com/de/shop/product/FXNE3D/A/refurb-mac-mini-apple-m4-chip")

    def test_filter_ignores_fallback_listing(self):
        items = parse_refurb(REFURB_GRID, PAGE)
        studio = AppleWatch(PAGE, ["Mac Studio"], [], None)
        mini = AppleWatch(PAGE, ["Mac mini"], [], 650)
        self.assertEqual([i.part for i in items if apple_matches(i, studio)], [])
        self.assertEqual([i.part for i in items if apple_matches(i, mini)], ["FXNE3D"])
        self.assertFalse(apple_matches(items[0], AppleWatch(PAGE, ["Mac mini"], [], 500)))
        self.assertFalse(apple_matches(items[0], AppleWatch(PAGE, ["Mac mini"], ["M4"], None)))

    def test_json_fallback(self):
        [item] = parse_refurb(REFURB_JSON, PAGE)
        self.assertEqual((item.part, item.price, item.previous_price), ("FX123D", 1869.0, 2199.0))


if __name__ == "__main__":
    unittest.main()
