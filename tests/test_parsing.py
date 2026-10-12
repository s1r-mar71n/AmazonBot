"""Unit tests for the parsing helpers.

The AOD snippet below is hand-written to mirror Amazon's offer-list markup;
it checks our parsing logic, not that Amazon still serves this exact markup."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amazon_watch import (DEFAULT_WAREHOUSE_IDS, DEFAULT_WAREHOUSE_SELLERS,  # noqa: E402
                          extract_asin, parse_offers, parse_price)

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


if __name__ == "__main__":
    unittest.main()
