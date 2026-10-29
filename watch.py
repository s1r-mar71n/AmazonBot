#!/usr/bin/env python3
"""Price/stock watcher with ntfy and/or Telegram alerts. Notify-only: never buys.

- Amazon: cheapest New and Amazon Warehouse offer vs. your price limits, from the home IP or
  spread over a proxy pool that halts each proxy at Amazon's first refusal.
- Apple Certified Refurbished: new listings whose title matches your keywords.
- Shopify stores (e.g. the MSI store), Cyberport and notebooksbilliger.de listing pages: matching
  products that come into stock."""

import argparse
import json
import logging
import random
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

log = logging.getLogger("watch")

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|o/ASIN)/([A-Z0-9]{10})(?:[/?#]|$)")
SHORT_HOSTS = {"amzn.eu", "amzn.to", "amzn.com", "a.co"}
NEW_WORDS = {"new", "neu", "neuf", "nuevo", "nuovo", "nieuw", "nowy"}
DEFAULT_WAREHOUSE_SELLERS = ["amazon warehouse", "amazon resale", "warehouse deals", "amazon retourenkauf"]
DEFAULT_WAREHOUSE_IDS = ["A8KICS1PHF7ZO"]  # Amazon Warehouse on amazon.de
# Not "api-services-support@amazon.com": Amazon's ordinary 404 page carries it in a comment.
CAPTCHA_MARKERS = ("validateCaptcha", "<title>Robot Check</title>")
# Bot challenges Amazon answers instead of the page: AWS WAF's JavaScript challenge (HTTP 202) and
# Akamai's interstitial (HTTP 200). Both are in tests/fixtures.
CHALLENGE_MARKERS = ("awsWafCookieDomainList", "AwsWafIntegration", "bm-verify", "/_sec/verify")

MIN_INTERVAL_MINUTES = 2
MIN_APPLE_INTERVAL_MINUTES = 2
BACKOFF_START = 30 * 60
BACKOFF_MAX = 6 * 3600
APPLE_BACKOFF_START = 15 * 60
APPLE_BACKOFF_MAX = 30 * 60  # Apple's 503s are usually store-wide outages; check back soon
MIN_SHOP_INTERVAL_MINUTES = 1
MIN_PROXY_INTERVAL_MINUTES = 3
PROXY_LIST_REFRESH = 6 * 3600  # Rayobyte replaces IPs now and then
PROXY_BENCH = 30 * 60  # after a proxy-side failure (unreachable, 407, timeout): not Amazon's doing
PROXY_HALT_MAX = 7 * 24 * 3600
SHOP_BACKOFF_START = 15 * 60
SHOP_BACKOFF_MAX = 2 * 3600
SHOP_PAGE_SIZE = 250  # Shopify's maximum for products.json
# Akamai Bot Manager on notebooksbilliger.de answers curl_cffi's Chrome fingerprints with a fake 404;
# Safari 17's passes (tested from the homeserver on 2026-10-29).
SHOP_IMPERSONATE = {"www.notebooksbilliger.de": "safari17_0"}
# A proxy a store refused sits out that store this long. Akamai on notebooksbilliger.de lets one
# request per IP through, then refuses that IP for 10+ minutes, longer while it keeps asking.
SHOP_PROXY_BENCH = 60 * 60


class Blocked(Exception):
    """Amazon answered with a CAPTCHA / throttling page."""


@dataclass
class Offer:
    price: float
    condition: str
    seller: str
    seller_id: str
    kind: str  # "new", "warehouse" or "other"


@dataclass
class Product:
    url: str
    name: str | None
    max_new: float | None
    max_warehouse: float | None
    proxy: bool = False


@dataclass
class Proxy:
    index: int
    ip: str
    port: str
    user: str
    password: str

    @property
    def label(self) -> str:  # never the credentials
        return f"#{self.index} {self.ip}"

    @property
    def url(self) -> str:
        return f"http://{self.user}:{self.password}@{self.ip}:{self.port}"


@dataclass
class AppleWatch:
    url: str
    match: list[str]
    exclude: list[str]
    max_price: float | None


@dataclass
class ShopWatch:
    url: str  # a store root (whole catalogue) or a /collections/<handle> page
    match: list[str]
    exclude: list[str]
    types: list[str]  # Shopify product_type, e.g. "VGA"; empty = any
    max_price: float | None
    interval_minutes: float | None  # None = settings.shop_interval_minutes
    sold_out_alerts: bool = False  # also a quiet alert when a new listing appears already sold out
    # At least one must appear, at no more than its price limit (None: any price); empty = no constraint.
    match_any: dict[str, float | None] = field(default_factory=dict)
    proxy: bool = False  # each request over the next proxy of the [proxies] pool


@dataclass
class ShopItem:
    key: str  # host/handle/variant id: the same variant seen through two feeds is one item
    title: str
    url: str
    price: float | None
    available: bool
    product_type: str
    details: str = ""  # extra lines for the alert
    image: str | None = None


@dataclass
class RefurbItem:
    title: str
    url: str
    part: str
    price: float | None
    previous_price: float | None


# --------------------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        cfg = tomllib.load(f)

    settings = cfg.setdefault("settings", {})
    settings.setdefault("interval_minutes", 20)
    settings.setdefault("jitter_minutes", 5)
    settings.setdefault("delay_between_products_seconds", [20, 60])
    settings.setdefault("renotify_hours", 12)
    settings.setdefault("accept_language", "de-DE,de;q=0.9,en;q=0.8")
    settings.setdefault("warehouse_sellers", DEFAULT_WAREHOUSE_SELLERS)
    settings.setdefault("warehouse_seller_ids", DEFAULT_WAREHOUSE_IDS)
    settings.setdefault("state_file", "state.json")
    settings.setdefault("apple_interval_minutes", 10)
    settings.setdefault("shop_interval_minutes", 5)
    if settings["interval_minutes"] < MIN_INTERVAL_MINUTES:
        log.warning("interval_minutes < %d is asking for CAPTCHAs; using %d",
                    MIN_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES)
        settings["interval_minutes"] = MIN_INTERVAL_MINUTES
    if settings["apple_interval_minutes"] < MIN_APPLE_INTERVAL_MINUTES:
        settings["apple_interval_minutes"] = MIN_APPLE_INTERVAL_MINUTES
    settings["shop_interval_minutes"] = max(settings["shop_interval_minutes"], MIN_SHOP_INTERVAL_MINUTES)

    ntfy = cfg.setdefault("ntfy", {})
    ntfy.setdefault("server", "https://ntfy.sh")
    tg = cfg.setdefault("telegram", {})
    if not ntfy.get("topic") and not (tg.get("bot_token") and tg.get("chat_id")):
        raise ValueError("set [ntfy] topic and/or [telegram] bot_token + chat_id")

    products, proxied = [], []
    for i, p in enumerate(cfg.get("product", []), 1):
        urls = ([p["url"]] if p.get("url") else []) + list(p.get("urls", []))
        if not urls:
            raise ValueError(f"product #{i}: url (or urls) is required")
        if p.get("max_new") is None and p.get("max_warehouse") is None:
            raise ValueError(f"product #{i}: set max_new and/or max_warehouse")
        via_proxy = bool(p.get("proxy", False))
        for url in urls:
            if via_proxy and not (extract_asin(url) and "amazon." in (urlparse(url).hostname or "")):
                # A short link would need an extra, non-offer-list request through the proxy.
                raise ValueError(f"product #{i}: proxied products need a full amazon URL with /dp/<ASIN>: {url}")
            item = Product(url, p.get("name") if len(urls) == 1 else None,
                           p.get("max_new"), p.get("max_warehouse"), via_proxy)
            (proxied if via_proxy else products).append(item)
    px = cfg.setdefault("proxies", {})
    px.setdefault("per_proxy_interval_minutes", 10)
    px.setdefault("halt_hours", 24)
    px.setdefault("breaker_halts", 3)
    px.setdefault("breaker_window_hours", 6)
    px.setdefault("breaker_pause_hours", 12)
    px["per_proxy_interval_minutes"] = max(px["per_proxy_interval_minutes"], MIN_PROXY_INTERVAL_MINUTES)
    as_list = lambda v: [v] if isinstance(v, str) else list(v or [])
    apple = []
    for i, a in enumerate(cfg.get("apple", []), 1):
        if not a.get("url") or not a.get("match"):
            raise ValueError(f"apple #{i}: url and match are required")
        apple.append(AppleWatch(a["url"], as_list(a["match"]), as_list(a.get("exclude")), a.get("max_price")))
    shops = []
    for i, w in enumerate(cfg.get("shop", []), 1):
        if not w.get("url") or not (w.get("match") or w.get("match_any")):
            raise ValueError(f"shop #{i}: url and match (or match_any) are required")
        interval = w.get("interval_minutes")
        any_of = w.get("match_any")  # a list, or a table of keyword -> price limit
        any_of = dict(any_of) if isinstance(any_of, dict) else dict.fromkeys(as_list(any_of))
        shops.append(ShopWatch(w["url"], as_list(w.get("match")), as_list(w.get("exclude")), as_list(w.get("types")),
                               w.get("max_price"),
                               max(interval, MIN_SHOP_INTERVAL_MINUTES) if interval is not None else None,
                               bool(w.get("sold_out_alerts", False)), any_of, bool(w.get("proxy", False))))
    if (proxied or any(w.proxy for w in shops)) and not (px.get("rayobyte_secret") or px.get("list_file")):
        raise ValueError("proxied products and shops need [proxies] rayobyte_secret or list_file")
    if not products and not proxied and not apple and not shops:
        raise ValueError("nothing to watch: add [[product]], [[apple]] and/or [[shop]] entries")
    cfg["products"], cfg["proxy_products"] = products, proxied
    cfg["apple_watches"], cfg["shop_watches"] = apple, shops
    return cfg


# --------------------------------------------------------------------------- parsing

def parse_price(text: str | None) -> float | None:
    """Parse '1.234,56 €', '€1,234.56', '899,–' etc. into a float."""
    if not text:
        return None
    s = re.sub(r"[^\d.,]", "", text)
    if not re.search(r"\d", s):
        return None
    m = re.search(r"[.,](\d{1,2})$", s)
    whole, frac = (s[:m.start()], m.group(1)) if m else (s, "0")
    whole = re.sub(r"[.,]", "", whole) or "0"
    return float(f"{whole}.{frac}")


def extract_asin(url: str) -> str | None:
    m = ASIN_RE.search(urlparse(url).path + "/")
    return m.group(1) if m else None


def classify(condition: str, seller: str, seller_id: str, wh_names, wh_ids, wh_link: bool = False) -> str:
    seller_l = seller.lower()
    if wh_link or seller_id in wh_ids or any(n in seller_l for n in wh_names):
        return "warehouse"
    first = re.split(r"[\s\-–:]+", condition.strip().lower(), maxsplit=1)[0] if condition.strip() else ""
    return "new" if first in NEW_WORDS else "other"


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


def _offer_price(el) -> float | None:
    for p in el.select(".a-price"):
        if "a-text-price" in p.get("class", []) or p.get("data-a-strike"):
            continue  # struck-through list price (UVP)
        price = parse_price(_text(p.select_one(".a-offscreen")))  # often empty nowadays
        if price:
            return price
        whole = _text(p.select_one(".a-price-whole"))
        if whole:
            return parse_price(f"{whole.rstrip(' .,')},{_text(p.select_one('.a-price-fraction')) or '00'}")
    # e.g. "2.747,90 € mit 9 Prozent Einsparungen": take the first price token only.
    return first_price(_text(el.select_one(".apex-pricetopay-accessibility-label")))


def parse_offers(html: str, wh_names, wh_ids) -> tuple[str | None, list[Offer]]:
    """Parse Amazon's 'All Offers Display' (AOD) fragment."""
    soup = BeautifulSoup(html, "html.parser")
    title = _text(soup.select_one("#aod-asin-title-text")) or None
    wh_names = [n.lower() for n in wh_names]
    offers = []
    for el in soup.select("[id=aod-pinned-offer], [id=aod-offer]"):
        price = _offer_price(el)
        if price is None:
            continue
        condition = _text(el.select_one("#aod-offer-heading"))
        sold_by = el.select_one("#aod-offer-soldBy")
        # The seller column's first entry is the seller: a <span> for Amazon itself, a link otherwise
        # (to /gp/aag/main?seller=... for marketplace sellers, to /Warehouse-Deals/ for Amazon
        # Retourenkauf, which carries no seller id). A "return policy" link follows it.
        name = sold_by.select_one(".a-col-right > span, .a-col-right > a") if sold_by else None
        seller = _text(name) or _text(sold_by)
        link = sold_by.select_one("a[href*='seller=']") if sold_by else None
        m = re.search(r"seller=([A-Z0-9]+)", link["href"]) if link else None
        seller_id = m.group(1) if m else ""
        wh_link = bool(sold_by and sold_by.select_one("a[href*='Warehouse-Deals']"))
        offers.append(Offer(price, condition, seller, seller_id,
                            classify(condition, seller, seller_id, wh_names, wh_ids, wh_link)))
    return title, offers


# --------------------------------------------------------------------------- amazon client

class Amazon:
    def __init__(self, accept_language: str):
        # Browser TLS/HTTP2 fingerprint; plain python clients get flagged fast.
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({"Accept-Language": accept_language})
        self.warmed: set[str] = set()

    def get(self, url: str, **kw):
        r = self.session.get(url, timeout=30, allow_redirects=True, **kw)
        if r.status_code in (429, 503):
            raise Blocked(f"HTTP {r.status_code}", url)
        if any(m in r.text[:20000] for m in CAPTCHA_MARKERS):
            raise Blocked("CAPTCHA page", url)
        r.raise_for_status()
        return r

    def warm_up(self, host: str):
        """First visit the homepage once so we carry normal session cookies."""
        if host not in self.warmed:
            self.get(f"https://{host}/")
            self.warmed.add(host)
            time.sleep(random.uniform(3, 8))

    def resolve(self, url: str) -> tuple[str, str]:
        host = urlparse(url).hostname or ""
        if host in SHORT_HOSTS:
            r = self.session.get(url, timeout=30, allow_redirects=True)
            url, host = r.url, urlparse(r.url).hostname or ""
        asin = extract_asin(url)
        if not asin or "amazon." not in host:
            raise ValueError(f"cannot find an ASIN in {url}")
        return host.removeprefix("m."), asin

    def offers(self, host: str, asin: str):
        self.warm_up(host)
        url = (f"https://{host}/gp/product/ajax/aodAjaxMain/ref=dp_aod_ALL_mbc?asin={asin}"
               "&m=&qid=&smid=&sourcecustomerorglistid=&sourcecustomerorglistitemid=&sr=&pc=dp")
        return self.get(url, headers={"Referer": f"https://{host}/dp/{asin}"}).text


# --------------------------------------------------------------------------- apple refurbished

PRICE_TOKEN_RE = re.compile(r"\d{1,3}(?:[.,\s\u00a0\u202f]\d{3})*(?:[.,](?:\d{1,2}|[-–]))?")


def first_price(text: str | None) -> float | None:
    m = PRICE_TOKEN_RE.search(text or "")
    return parse_price(m.group(0)) if m else None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _refurb_item(title: str, url: str, price, previous) -> RefurbItem:
    url = urljoin(url, urlparse(url).path)  # drop query/fragment
    m = re.search(r"/shop/product/([^/]+)", url)
    return RefurbItem(title, url, m.group(1) if m else url, price, previous)


def _strip_tags(s: str | None) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def parse_refurb(html: str, page_url: str) -> list[RefurbItem]:
    """Parse an Apple refurbished category page into its product tiles.

    Note: when a category (e.g. mac-studio) has no stock, Apple shows the whole
    parent category instead, so callers must filter tiles by title."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    grid = soup.select_one("div.refurbished-category-grid-no-js, div.rf-refurb-category-grid-no-js")
    if grid:
        for li in grid.select("li"):
            a = li.select_one("h3 a[href]")
            if not a:
                continue
            price = first_price(_text(li.select_one(".as-price-currentprice"))) or first_price(_text(li.find("div")))
            previous = first_price(_text(li.select_one(".as-price-previousprice")))
            items.append(_refurb_item(_text(a), urljoin(page_url, a["href"]), price, previous))
        return items

    # Fallback: the JSON the page's JavaScript grid is rendered from.
    m = re.search(r"REFURB_GRID_BOOTSTRAP\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
    if m:
        try:
            tiles = json.loads(m.group(1)).get("tiles", [])
        except json.JSONDecodeError:
            tiles = []
        for t in tiles:
            p = t.get("price") or {}
            cur, prev = p.get("currentPrice") or {}, p.get("previousPrice") or {}
            if t.get("title") and t.get("productDetailsUrl"):
                items.append(_refurb_item(
                    t["title"], urljoin(page_url, t["productDetailsUrl"]),
                    _num(cur.get("raw_amount")) or first_price(_strip_tags(cur.get("amount"))),
                    # previousPrice.raw_amount is the CURRENT price; the original is elsewhere.
                    _num(p.get("originalProductAmount"))
                    or first_price(_strip_tags(prev.get("previousPrice") or prev.get("amount")))))
    return items


def _norm(s: str) -> str:
    """Lowercase and fold Apple's typography: 'Mac\\xa0mini' (no-break space), soft
    hyphens and non-breaking hyphens would otherwise defeat plain substring matching."""
    s = s.replace("­", "").replace("‑", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def apple_matches(item: RefurbItem, w: AppleWatch) -> bool:
    t = _norm(item.title)
    return (all(_norm(k) in t for k in w.match)
            and not any(_norm(k) in t for k in w.exclude)
            and (w.max_price is None or (item.price is not None and item.price <= w.max_price)))


def new_session(accept_language: str, impersonate: str = "chrome"):
    s = requests.Session(impersonate=impersonate)
    s.headers.update({"Accept-Language": accept_language})
    return s


def apple_round(session, cfg: dict, state: dict, dry_run: bool) -> bool:
    """Check all [[apple]] watches. Returns False if Apple refused us."""
    seen = state.setdefault("apple_seen", {})  # url -> {part: price}
    by_url: dict[str, list[AppleWatch]] = {}
    for w in cfg["apple_watches"]:
        by_url.setdefault(w.url, []).append(w)

    ok = True
    for i, (url, watches) in enumerate(by_url.items()):
        if i:
            time.sleep(random.uniform(5, 15))
        try:
            r = session.get(url, timeout=30, allow_redirects=True)
        except Exception as e:
            log.error("apple: fetching %s failed: %s", url, e)
            continue
        if r.status_code in (403, 429, 503):
            title = BeautifulSoup(r.text[:20000], "html.parser").title
            title = _text(title) if title else ""
            log.warning("apple: HTTP %d for %s (page title: %r), backing off", r.status_code, url, title)
            last_refusal["apple"] = f"HTTP {r.status_code}" + (f", page title “{title}”" if title else "")
            ok = False
            break
        if r.status_code != 200:
            log.error("apple: HTTP %d for %s", r.status_code, url)
            continue

        items = parse_refurb(r.text, r.url)
        if not items:
            log.warning("apple: no tiles parsed on %s (did Apple change its markup?)", url)
            continue
        matched = {it.part: it for it in items if any(apple_matches(it, w) for w in watches)}
        log.info("apple: %s: %d tiles, %d matching", urlparse(url).path, len(items), len(matched))

        prev = seen.get(url, {})
        for part, it in matched.items():
            if part in prev and (it.price is None or prev[part] is None or it.price >= prev[part] - 0.005):
                continue
            msg = f"{it.price:.2f}" if it.price is not None else "price unknown"
            if it.previous_price and it.price and it.previous_price > it.price:
                msg += f" (was {it.previous_price:.2f}, -{1 - it.price / it.previous_price:.0%})"
            notify(cfg, f"Refurb: {it.title}", msg, click=it.url, tags=["apple"], dry_run=dry_run)
        # Forget listings that disappeared, so they alert again if they come back.
        seen[url] = {part: it.price for part, it in matched.items()}
    return ok


# --------------------------------------------------------------------------- shopify stores

def shop_feed_url(url: str, page: int) -> str:
    """products.json of a collection page, or of the whole store for any other URL."""
    u = urlparse(url)
    m = re.match(r"(/(?:[a-z]{2}(?:-[a-z]{2})?/)?collections/[^/?#]+)", u.path)
    return f"{u.scheme}://{u.netloc}{m.group(1) if m else ''}/products.json?limit={SHOP_PAGE_SIZE}&page={page}"


def parse_shop(data: dict, page_url: str) -> list[ShopItem]:
    """One item per variant of Shopify's products.json. Collections may hide sold-out
    products; the store-wide feed lists them with available=false."""
    u = urlparse(page_url)
    items = []
    for p in data.get("products", []):
        variants = p.get("variants") or []
        for v in variants:
            title = p["title"]
            if len(variants) > 1 and v.get("title") and v["title"] != "Default Title":
                title += f" ({v['title']})"
            url = f"{u.scheme}://{u.netloc}/products/{p['handle']}"
            if len(variants) > 1:
                url += f"?variant={v['id']}"
            items.append(ShopItem(f"{u.netloc}/{p['handle']}/{v['id']}", title, url, _num(v.get("price")),
                                  bool(v.get("available")), p.get("product_type") or ""))
    return items


def _squash(s: str) -> str:
    """Shops space model names differently ("RTX 5070 Ti", "RTX 5070Ti"): compare without spaces."""
    return _norm(s).replace(" ", "")


def shop_matches(item: ShopItem, w: ShopWatch) -> bool:
    t = _squash(item.title)
    return (all(_squash(k) in t for k in w.match)
            and (not w.match_any or any(_squash(k) in t and (limit is None or (item.price is not None
                                                                         and item.price <= limit))
                                        for k, limit in w.match_any.items()))
            and not any(_squash(k) in t for k in w.exclude)
            and (not w.types or item.product_type.lower() in {x.lower() for x in w.types})
            and (w.max_price is None or (item.price is not None and item.price <= w.max_price)))


CYBERPORT_AVAILABILITY = {"AVAILABLE": "sofort lieferbar", "UNAVAILABLE": "nicht lieferbar"}


def parse_cyberport(html: str, page_url: str) -> list[ShopItem] | None:
    """The listing of a Cyberport category page: its Next.js data embeds the Algolia results the
    page renders (up to 20, in the URL's filter and sort order). None if the data isn't there."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]["fullPageProps"]["staticProps"]
        results = next(iter(props["algoliaServerState"]["initialResults"].values()))["results"][0]
    except (KeyError, StopIteration, IndexError, json.JSONDecodeError):
        return None
    host = urlparse(page_url).hostname or ""
    shop = "WS_AT" if host.endswith(".at") else "WS_DE"
    if results.get("nbPages", 1) > 1:
        log.warning("cyberport: %s has %d pages; only the first is read, narrow the filter", page_url,
                    results["nbPages"])
    items = []
    for h in results.get("hits", []):
        gross = h.get("salesPricesGross") or {}
        price = _num(gross.get(shop)) or (min(map(float, gross.values())) if gross else None)
        status = (h.get("availabilityStatus") or {}).get("ZL_001", "")
        bullets = [_text(li) for li in BeautifulSoup(h.get("bulletpoints") or "", "html.parser").select("li")]
        ids = [f"Art.-Nr. {h['objectID']}"]
        if h.get("manufacturerpartnumber"):
            ids.append(f"{h.get('manufacturer', '')} {h['manufacturerpartnumber']}".strip())
        if h.get("gtin"):
            ids.append(f"EAN {h['gtin']}")
        lines = []
        if h.get("bware_error_description"):
            lines.append(f"Zustand: {h['bware_error_description']}")
        elif h.get("usedproduct"):
            lines.append("Zustand: gebraucht / B-Ware")
        label = CYBERPORT_AVAILABILITY.get(status) or ("bald lieferbar" if status.startswith("SOON_AVAILABLE")
                                                       else status or "unbekannt")
        lines.append(f"Verfügbarkeit: {label}")
        lines += [f"• {b}" for b in bullets]
        lines.append(" · ".join(ids))
        if h.get("timestamp_created"):
            lines.append("Angelegt: " + time.strftime("%d.%m.%Y", time.localtime(h["timestamp_created"])))
        items.append(ShopItem(
            key=f"{host}/{h['objectID']}", title=h.get("name", h["objectID"]),
            url=urljoin(f"https://{host}/", h.get("url", "")), price=price,
            # SOON_AVAILABLE_* can still be ordered; only UNAVAILABLE counts as sold out.
            available=bool(status) and status != "UNAVAILABLE",
            product_type=h.get("categoryid") and str(h["categoryid"]) or "",
            details="\n".join(lines),
            image=f"https://img-resizer.cyberport.de/cp/images/600x600/{h['image_url']}" if h.get("image_url") else None))
    return items


def fetch_cyberport(session, url: str) -> list[ShopItem] | None:
    r = session.get(url, timeout=30, allow_redirects=True)
    items = parse_cyberport(r.text, url) if r.status_code == 200 else None
    if items is None:
        title = BeautifulSoup(r.text[:20000], "html.parser").title
        log.warning("shop: HTTP %d for %s without listing data (page title: %r), backing off",
                    r.status_code, url, _text(title) if title else "")
        last_refusal[urlparse(url).hostname or ""] = (f"HTTP {r.status_code}"
                                                      + ("" if r.status_code != 200 else ", no listing data"))
    return items


NBB_SOLD_OUT = re.compile(r"nicht (?:lieferbar|verfügbar)|ausverkauft", re.I)


def parse_nbb(html: str, page_url: str) -> list[ShopItem] | None:
    """The product cards of a notebooksbilliger.de category or outlet page (up to 20, in the URL's
    filter and sort order). None if there is no product listing, e.g. Akamai's fake 404 page."""
    soup = BeautifulSoup(html, "html.parser")
    listing = soup.select_one("#product-listing")
    if listing is None:
        return None
    cards = listing.select("div.product-card[data-product-id]")
    if len(cards) >= 20 and not listing.select_one(".product-listing__no-more-products"):
        log.warning("notebooksbilliger: %s has more than one page; only the first is read, narrow the filter",
                    page_url)
    host = urlparse(page_url).hostname or ""
    items = []
    for c in cards:
        availability = _text(c.select_one(".js-availability-text"))
        lines = []
        if condition := _text(c.select_one(".product-card__outlet-state")):
            lines.append(f"Zustand: {condition}")
        lines.append(f"Verfügbarkeit: {availability or 'unbekannt'}")
        lines += [f"• {_text(li)}" for li in c.select(".product-card__list-item")]
        lines.append(" · ".join(x for x in (f"Art.-Nr. {c.get('data-product-article-number', '')}".strip(),
                                            c.get("data-product-manufacturer")) if x))
        img = c.select_one("img.product-card__image")
        link = c.select_one("a.product-card__link")
        items.append(ShopItem(
            key=f"{host}/{c['data-product-id']}", title=_text(c.select_one(".product-card__product-heading-title")),
            url=urljoin(page_url, link["href"]) if link else page_url, price=parse_price(c.get("data-product-price")),
            available=bool(availability) and not NBB_SOLD_OUT.search(availability), product_type="",
            details="\n".join(lines),
            image=re.sub(r"size=\d+", "size=600", img["src"]) if img and img.get("src") else None))
    return items


def fetch_nbb(session, url: str) -> list[ShopItem] | None:
    # Akamai's _abck cookie from the first response is never validated (we don't run its sensor
    # script), and the next request carrying it gets the fake 404: ask without cookies every time.
    session.cookies.clear()
    r = session.get(url, timeout=30, allow_redirects=True)
    items = parse_nbb(r.text, url) if r.status_code == 200 else None
    if items is None:
        # Akamai blocks with "404 Die Seite wurde nicht gefunden", even for the home page.
        log.warning("shop: HTTP %d for %s without a product listing (refused)", r.status_code, url)
        last_refusal[urlparse(url).hostname or ""] = (f"HTTP {r.status_code}"
                                                      + ("" if r.status_code != 200 else ", no product listing"))
    return items


def fetch_shop(session, url: str) -> list[ShopItem] | None:
    """All pages of a products.json feed (or a Cyberport / notebooksbilliger listing). None if the store
    refused us."""
    host = urlparse(url).hostname or ""
    if "cyberport." in host:
        return fetch_cyberport(session, url)
    if "notebooksbilliger." in host:
        return fetch_nbb(session, url)
    items = []
    for page in range(1, 21):
        if page > 1:
            time.sleep(random.uniform(2, 5))
        r = session.get(shop_feed_url(url, page), timeout=30, allow_redirects=True)
        if r.status_code in (403, 429, 430, 503):  # 430: Shopify's bot protection
            log.warning("shop: HTTP %d for %s, backing off", r.status_code, url)
            last_refusal[host] = f"HTTP {r.status_code}"
            return None
        r.raise_for_status()
        data = r.json()
        items += parse_shop(data, url)
        if len(data.get("products", [])) < SHOP_PAGE_SIZE:
            break
    return items


def _shop_message(it: ShopItem, price: str) -> str:
    return f"{price} · {urlparse(it.url).netloc}" + (f"\n{it.details}" if it.details else "")


def shop_round(sessions, cfg: dict, state: dict, dry_run: bool, watches: list[ShopWatch]) -> set[str]:
    """Check the given [[shop]] watches, with sessions[host] for each store (a ShopSessions proxy for
    proxy = true watches; a refused proxy sits out and the next one gets a go). Alerts once per variant
    when it is (or comes back) in stock, and again if its price drops; with sold_out_alerts, quietly when
    a new listing is already sold out. Returns the hosts that refused us; their other URLs are skipped."""
    avail = state.setdefault("shop_available", {})  # key -> {"price", "seen"} for in-stock variants
    listed = state.setdefault("shop_listed", {})  # key -> last seen, for every matching variant
    by_url: dict[str, list[ShopWatch]] = {}
    for w in watches:
        by_url.setdefault(w.url, []).append(w)

    now, refused, matched = time.time(), set(), {}
    for i, (url, ws) in enumerate(by_url.items()):
        host = urlparse(url).hostname or ""
        if host in refused:
            continue
        if i:
            time.sleep(random.uniform(3, 8))
        via = None
        try:
            if any(w.proxy for w in ws):
                items = None
                for _ in range(2):
                    if not (via := sessions.proxy_for(host, cfg.get("proxies", {}))):
                        log.warning("shop: no proxy available for %s", host)
                        break
                    items = fetch_shop(via[0], url)
                    if items is not None:
                        break
                    sessions.bench(host, via[1])
            else:
                items = fetch_shop(sessions[host], url)
        except Exception as e:  # a proxy URL holds credentials: name the proxy, not the error text
            log.error("shop: fetching %s failed: %s", url,
                      f"{type(e).__name__} via proxy {via[1].label}" if via else e)
            continue
        if items is None:
            refused.add(host)
            continue
        hits = [(it, any(w.sold_out_alerts for w in ws if shop_matches(it, w)))
                for it in items if any(shop_matches(it, w) for w in ws)]
        log.info("shop: %s%s: %d variants, %d matching (%d in stock)", url, f" via {via[1].label}" if via else "",
                 len(items), len(hits), sum(it.available for it, _ in hits))
        for it, quiet in hits:
            matched[it.key] = (it, quiet or matched.get(it.key, (None, False))[1])

    for key, (it, sold_out_alerts) in matched.items():
        new_listing = key not in listed
        listed[key] = now
        if not it.available:
            avail.pop(key, None)  # sold out: alert again when it comes back
            if new_listing and sold_out_alerts:
                price = f"{it.price:.2f} €" if it.price is not None else "price unknown"
                notify(cfg, f"Listed, sold out: {it.title}", _shop_message(it, price),
                       click=it.url, priority=2, tags=["eyes"], dry_run=dry_run, photo=it.image)
            continue
        prev = avail.get(key)
        if prev is None or (it.price is not None and prev["price"] is not None and it.price < prev["price"] - 0.005):
            price = f"{it.price:.2f} €" if it.price is not None else "price unknown"
            notify(cfg, f"In stock: {it.title}", _shop_message(it, price), click=it.url,
                   priority=5, tags=["rotating_light"], dry_run=dry_run, photo=it.image)
        avail[key] = {"price": it.price, "seen": now}
    # Collections hide sold-out products, so "not in this feed" doesn't mean sold out. A variant that
    # no feed has shown for a while is gone, though: forget it so a relisting alerts again.
    longest = max([(w.interval_minutes or cfg["settings"]["shop_interval_minutes"]) * 60
                   for w in cfg["shop_watches"]] or [0])
    for key in [k for k, v in avail.items() if now - v["seen"] > max(3600, 3 * longest)]:
        del avail[key]
    # Longer for listings: a store scan failing for an hour shouldn't repeat a sold-out alert.
    for key in [k for k, seen in listed.items() if now - seen > max(6 * 3600, 3 * longest)]:
        del listed[key]
    return refused


class ShopSessions(dict):
    """host -> session, made on first use with the browser fingerprint that store lets through.
    proxy_for() hands out a fresh session over the next proxy of the pool instead."""

    def __init__(self, accept_language: str):
        super().__init__()
        self.accept_language = accept_language
        self.proxies: list[Proxy] = []
        self.loaded_at = 0.0
        self.turn: dict[str, int] = {}  # host -> rotation position
        self.benched: dict[tuple[str, str], float] = {}  # (host, proxy ip) -> until

    def __missing__(self, host: str):
        s = self[host] = new_session(self.accept_language, SHOP_IMPERSONATE.get(host, "chrome"))
        return s

    def proxy_for(self, host: str, px: dict):
        """(session, proxy) for the next proxy this store hasn't refused lately, or None."""
        if not self.proxies or time.time() - self.loaded_at > PROXY_LIST_REFRESH:
            try:
                self.proxies = load_proxies(px) or self.proxies
            except Exception as e:  # the export URL contains the API key: log the type only
                log.error("proxies: loading the list failed: %s", type(e).__name__)
            self.loaded_at = time.time()
        if not self.proxies:
            return None
        # A random start, so a restart doesn't always hit the same few IPs first.
        turn, now = self.turn.get(host, random.randrange(len(self.proxies))), time.time()
        for k in range(len(self.proxies)):  # round the pool in order, skipping benched proxies
            p = self.proxies[(turn + k) % len(self.proxies)]
            if self.benched.get((host, p.ip), 0) <= now:
                self.turn[host] = turn + k + 1
                s = new_session(self.accept_language, SHOP_IMPERSONATE.get(host, "chrome"))
                s.proxies = {"https": p.url, "http": p.url}
                return s, p
        return None

    def bench(self, host: str, p: Proxy):
        log.info("shop: %s refused proxy %s; it sits out %d min", host, p.label, SHOP_PROXY_BENCH // 60)
        self.benched[(host, p.ip)] = time.time() + SHOP_PROXY_BENCH


# --------------------------------------------------------------------------- notifications

def _post_json(url: str, body: dict, headers: dict | None = None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    urllib.request.urlopen(req, timeout=15).close()


TELEGRAM_CAPTION_MAX = 1024


def telegram_text(title: str, message: str, click: str | None, limit: int) -> str:
    """title, message, link; the message is cut (never the link) to fit Telegram's limit."""
    tail = f"\n{click}" if click else ""
    text = f"{title}\n{message}"
    if len(text) + len(tail) > limit:
        text = text[:limit - len(tail) - 1].rstrip() + "…"
    return text + tail


def notify(cfg: dict, title: str, message: str, click: str | None = None,
           priority: int = 4, tags: list[str] | None = None, dry_run: bool = False, photo: str | None = None):
    """Send to every configured channel: ntfy and/or Telegram. With a photo URL, Telegram gets a
    photo with the text as caption (falling back to a plain message), ntfy an attachment."""
    if dry_run:
        log.info("[dry-run] notify: %s | %s%s", title, message, f" | photo {photo}" if photo else "")
        return
    n, tg = cfg["ntfy"], cfg["telegram"]
    if n.get("topic"):
        body = {"topic": n["topic"], "title": title, "message": message,
                "priority": priority, "tags": tags or []}
        if click:
            body["click"] = click
        if photo:
            body["attach"] = photo
        # JSON publishing avoids header-encoding trouble with € and umlauts.
        try:
            _post_json(n["server"].rstrip("/"), body,
                       {"Authorization": f"Bearer {n['token']}"} if n.get("token") else None)
        except Exception as e:
            log.error("ntfy delivery failed: %s", e)
    if tg.get("bot_token") and tg.get("chat_id"):
        # The link also goes in the text: Telegram asks "Open this link?" before following a
        # button, while a URL in the text opens with one tap (same as immocrawler's alerts).
        api = f"https://api.telegram.org/bot{tg['bot_token']}"
        base = {"chat_id": tg["chat_id"], "disable_notification": priority < 3}
        if click:
            base["reply_markup"] = {"inline_keyboard": [[{"text": "Open", "url": click}]]}
        if photo:
            try:
                _post_json(f"{api}/sendPhoto", {**base, "photo": photo,
                                                "caption": telegram_text(title, message, click, TELEGRAM_CAPTION_MAX)})
                return
            except Exception as e:  # e.g. Telegram can't fetch the image: send the text alone
                log.warning("telegram photo failed (%s); sending text only", type(e).__name__)
        try:
            _post_json(f"{api}/sendMessage", {**base, "text": telegram_text(title, message, click, 4096),
                                              "disable_web_page_preview": True})
        except Exception as e:  # str(e) can contain the URL, i.e. the bot token: log the type only
            log.error("telegram delivery failed: %s", type(e).__name__)


# --------------------------------------------------------------------------- main loop

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def check_product(amz: Amazon, p: Product, cfg: dict, state: dict, dry_run: bool):
    resolved = state.setdefault("resolved", {})
    if p.url not in resolved:
        host, asin = amz.resolve(p.url)
        resolved[p.url] = {"host": host, "asin": asin}
    host, asin = resolved[p.url]["host"], resolved[p.url]["asin"]
    evaluate_offers(p, host, asin, amz.offers(host, asin), cfg, state, dry_run)


def evaluate_offers(p: Product, host: str, asin: str, html: str, cfg: dict, state: dict, dry_run: bool,
                    via: str = ""):
    """Parse an offer list and alert on New/Warehouse offers within the product's limits."""
    s = cfg["settings"]
    title, offers = parse_offers(html, s["warehouse_sellers"], s["warehouse_seller_ids"])
    name = p.name or (title[:60] if title else asin)
    if not offers:
        log.warning("%s: no offers parsed (out of stock, or Amazon changed its markup)", name)

    best: dict[str, Offer] = {}
    for o in offers:
        if o.kind != "other" and (o.kind not in best or o.price < best[o.kind].price):
            best[o.kind] = o
    log.info("%s: new=%s warehouse=%s (%d offers)%s", name,
             best["new"].price if "new" in best else "-",
             best["warehouse"].price if "warehouse" in best else "-", len(offers), via and f" via {via}")

    alerts = state.setdefault("alerts", {})
    now = time.time()
    for kind, limit in (("new", p.max_new), ("warehouse", p.max_warehouse)):
        if limit is None:
            continue
        key, o, prev = f"{asin}:{kind}", best.get(kind), alerts.get(f"{asin}:{kind}")
        if o and o.price <= limit:
            if prev is None or o.price < prev["price"] - 0.005 or now - prev["ts"] > s["renotify_hours"] * 3600:
                label = "Warehouse" if kind == "warehouse" else "New"
                msg = f"{o.price:.2f} (limit {limit:.2f})\n{o.condition or label} · {o.seller or 'unknown seller'}"
                notify(cfg, f"{label}: {name}", msg, click=f"https://{host}/dp/{asin}",
                     tags=["moneybag"] if kind == "new" else ["package"], dry_run=dry_run)
                alerts[key] = {"price": o.price, "ts": now}
        else:
            alerts.pop(key, None)


def amazon_round(amz: Amazon, cfg: dict, state: dict, dry_run: bool) -> bool:
    """Check all [[product]] entries. Returns False if Amazon blocked us."""
    products = list(cfg["products"])
    random.shuffle(products)
    for i, p in enumerate(products):
        if i:
            time.sleep(random.uniform(*cfg["settings"]["delay_between_products_seconds"]))
        try:
            check_product(amz, p, cfg, state, dry_run)
        except Blocked as e:
            log.warning("Amazon is blocking us (%s for %s); stopping this round", *e.args)
            last_refusal["amazon"] = e.args[0]
            return False
        except Exception:
            log.exception("check failed for %s", p.url)
    return True


# --------------------------------------------------------------------------- amazon over proxies

def _yaml_section(text: str, section: str) -> dict:
    """key: value pairs of one top-level section of a flat YAML file (enough for the secret file)."""
    out, sec = {}, None
    for line in text.splitlines():
        if re.match(r"^\S", line):
            sec = line.split(":")[0].strip()
        m = re.match(r'^\s+([\w-]+):\s*["\']?([^"\'\n]*)["\']?\s*$', line)
        if m and sec == section:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_proxy_list(text: str) -> list[Proxy]:
    """ip:port:user:pass per line, Rayobyte's export format."""
    rows = [line.strip().split(":") for line in text.splitlines()]
    return [Proxy(i, *row) for i, row in enumerate(r for r in rows if len(r) == 4)]


def load_proxies(px: dict) -> list[Proxy]:
    if px.get("list_file"):
        return parse_proxy_list(Path(px["list_file"]).read_text())
    ray = _yaml_section(Path(px["rayobyte_secret"]).read_text(), "rayobyte")
    url = f"https://rayobyte.com/proxy/dashboard/api/export/4/all/{ray['email']}/{ray['api-key']}/list.csv"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "curl/8"}), timeout=20) as r:
        return parse_proxy_list(r.read().decode())


def rejection(status: int, headers, text: str) -> str | None:
    """Why an offer-list response is not a normal answer, or None. Anything unexpected counts:
    halting a proxy on a false alarm costs little, using one Amazon has flagged costs the IP."""
    waf = headers.get("x-amzn-waf-action")
    head = text[:20000]
    if waf:
        return f"AWS WAF {waf} (HTTP {status})"
    if any(m in head for m in CAPTCHA_MARKERS):
        return f"CAPTCHA page (HTTP {status})"
    if any(m in head for m in CHALLENGE_MARKERS):
        return f"bot challenge page (HTTP {status})"
    if status != 200:
        return f"HTTP {status}"
    if 'id="aod-container"' not in text:
        return f"not an offer list ({len(text)} bytes)"
    return None


class ProxyFleet:
    """Amazon offer lists over a proxy pool, one request at a time, evenly spread: each proxy is used
    at most every per_proxy_interval_minutes, and the pool as a whole sends one request every
    interval / active proxies. No homepage warm-up: Amazon answers it with a WAF challenge even for
    the home IP, while the offer list works without cookies."""

    def __init__(self, accept_language: str):
        self.accept_language = accept_language
        self.proxies: list[Proxy] = []
        self.loaded_at = 0.0
        self.sessions: dict[str, object] = {}
        self.last_used: dict[str, float] = {}  # ip -> time
        self.benched: dict[str, float] = {}  # ip -> until (proxy-side failures, in memory only)
        self.checked: dict[str, float] = {}  # product url -> time
        self.next_request = 0.0

    def refresh(self, px: dict):
        if self.proxies and time.time() - self.loaded_at < PROXY_LIST_REFRESH:
            return
        try:
            fresh = load_proxies(px)
        except Exception as e:  # the export URL contains the API key: log the type only
            log.error("proxies: loading the list failed: %s", type(e).__name__)
            fresh = []
        if fresh:
            if {p.ip for p in fresh} != {p.ip for p in self.proxies}:
                log.info("proxies: %d loaded", len(fresh))
            self.proxies = fresh
            self.loaded_at = time.time()
        elif not self.proxies:
            self.loaded_at = time.time() - PROXY_LIST_REFRESH + 30 * 60  # retry in 30 min

    def session(self, p: Proxy):
        key = f"{p.ip}:{p.port}"
        if key not in self.sessions:
            s = requests.Session(impersonate="chrome")
            s.headers.update({"Accept-Language": self.accept_language})
            s.proxies = {"https": p.url, "http": p.url}
            self.sessions[key] = s
        return self.sessions[key]

    def drop_session(self, p: Proxy):
        self.sessions.pop(f"{p.ip}:{p.port}", None)

    def get_offers(self, p: Proxy, host: str, asin: str):
        url = (f"https://{host}/gp/product/ajax/aodAjaxMain/ref=dp_aod_ALL_mbc?asin={asin}"
               "&m=&qid=&smid=&sourcecustomerorglistid=&sourcecustomerorglistitemid=&sr=&pc=dp")
        return self.session(p).get(url, timeout=30, allow_redirects=False,
                                   headers={"Referer": f"https://{host}/dp/{asin}"})


def _ago(seconds: float) -> str:
    h, m = divmod(int(seconds // 60), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def _scrub(text: str, p: Proxy) -> str:
    return text.replace(p.password, "***").replace(p.user, "***")


def proxy_step(fleet: ProxyFleet, cfg: dict, state: dict, dry_run: bool):
    """At most one proxied offer-list request. Sets fleet.next_request.
    Amazon refusing a proxy halts that IP (halt_hours, doubling on a repeat within a week, max 7 days)
    and alerts at once; breaker_halts halts within breaker_window_hours pause the whole pool, since
    then Amazon is likely flagging the pattern rather than single IPs."""
    px, now = cfg["proxies"], time.time()
    halts = state.setdefault("proxy_halts", {})  # ip -> {since, until, reason, count, cleared}
    paused = state.get("proxy_paused_until", 0)
    if paused > now:
        fleet.next_request = paused
        return
    if paused:
        state.pop("proxy_paused_until")
        notify(cfg, "Proxy watch resumed", "The pause is over; checking again, one proxy at a time.",
               priority=2, tags=["white_check_mark"], dry_run=dry_run)

    fleet.refresh(px)
    halted = lambda p: halts.get(p.ip, {}).get("until", 0) > now
    active = [p for p in fleet.proxies if not halted(p)]
    if not active:
        waits = [halts[p.ip]["until"] for p in fleet.proxies if halted(p)]
        fleet.next_request = min(waits) if waits else now + 30 * 60
        return
    interval = px["per_proxy_interval_minutes"] * 60
    gap = interval / len(active) * random.uniform(0.8, 1.2)
    ready = [p for p in active if fleet.benched.get(p.ip, 0) <= now and now - fleet.last_used.get(p.ip, 0) >= interval]
    if not ready:
        fleet.next_request = now + max(gap, 30)
        return
    p = min(ready, key=lambda x: (fleet.last_used.get(x.ip, 0), random.random()))
    prod = min(cfg["proxy_products"], key=lambda x: (fleet.checked.get(x.url, 0), random.random()))
    host, asin = (urlparse(prod.url).hostname or "").removeprefix("m."), extract_asin(prod.url)
    fleet.last_used[p.ip] = now
    fleet.next_request = now + gap

    try:
        r = fleet.get_offers(p, host, asin)
    except Exception as e:  # unreachable proxy, 407, timeout: the proxy's problem, not Amazon's
        log.warning("proxy %s: request failed (%s: %s); benched %d min", p.label, type(e).__name__,
                    _scrub(str(e), p)[:150], PROXY_BENCH // 60)
        fleet.benched[p.ip] = now + PROXY_BENCH
        fleet.drop_session(p)
        return

    reason = rejection(r.status_code, r.headers, r.text)
    if reason == "HTTP 404":  # Amazon's plain "page not found": the ASIN is gone, the proxy is fine
        fleet.checked[prod.url] = now
        missing = state.setdefault("proxy_missing", [])
        log.warning("%s: Amazon says the product doesn't exist (HTTP 404 via %s)", asin, p.label)
        if asin not in missing:
            missing.append(asin)
            notify(cfg, f"Product not found: {asin}", "Amazon answers 404 for it. Remove it from the config.",
                   click=prod.url, priority=2, tags=["question"], dry_run=dry_run)
        return
    if reason:
        prev = halts.get(p.ip, {})
        count = prev.get("count", 0) + 1 if now - prev.get("since", 0) < PROXY_HALT_MAX else 1
        hold = min(px["halt_hours"] * 3600 * 2 ** (count - 1), PROXY_HALT_MAX)
        halts[p.ip] = {"since": now, "until": now + hold, "reason": reason, "count": count, "cleared": False}
        fleet.drop_session(p)
        left = len(active) - 1
        log.warning("proxy %s: Amazon refused %s: %s; halted for %s (%d of %d proxies left)",
                    p.label, asin, reason, _ago(hold), left, len(fleet.proxies))
        notify(cfg, f"Amazon refused proxy {p.ip}",
               f"{reason} on {asin}. Proxy halted for {_ago(hold)}"
               f"{f' (refused {count}x within a week)' if count > 1 else ''}. "
               f"{left} of {len(fleet.proxies)} proxies left.",
               priority=4, tags=["warning"], dry_run=dry_run)
        recent = [h for h in halts.values() if now - h["since"] < px["breaker_window_hours"] * 3600]
        if len(recent) >= px["breaker_halts"] or not left:
            pause = px["breaker_pause_hours"] * 3600
            state["proxy_paused_until"] = now + pause
            fleet.next_request = now + pause
            why = "no proxies left" if not left else \
                f"{len(recent)} proxies refused within {px['breaker_window_hours']} h"
            log.warning("proxies: pausing the proxied watch for %s: %s", _ago(pause), why)
            notify(cfg, "Proxy watch paused",
                   f"{why}, so Amazon may be flagging the pattern, not single IPs. "
                   f"Pausing all proxied checks for {_ago(pause)}.",
                   priority=4, tags=["warning"], dry_run=dry_run)
        return

    fleet.checked[prod.url] = now
    if p.ip in halts and not halts[p.ip].get("cleared"):
        halts[p.ip]["cleared"] = True
        notify(cfg, f"Proxy {p.ip} back in use",
               f"Amazon answers it normally again ({_ago(now - halts[p.ip]['since'])} after the refusal).",
               priority=2, tags=["white_check_mark"], dry_run=dry_run)
    for ip in [ip for ip, h in halts.items() if h.get("cleared") and now - h["since"] > PROXY_HALT_MAX]:
        del halts[ip]
    try:
        evaluate_offers(prod, host, asin, r.text, cfg, state, dry_run, via=p.label)
    except Exception:
        log.exception("check failed for %s", prod.url)


last_refusal: dict[str, str] = {}  # source -> why the last refused request failed


def _backoff(current: int, start: int, cap: int) -> int:
    return min(current * 2, cap) if current else start


def _jittered(minutes: float, jitter: float) -> float:
    return (minutes + random.uniform(-1, 1) * jitter) * 60


def _block_notice(cfg, state, source: str, blocked: bool, backoff: int, dry_run: bool, name: str | None = None):
    """Send one warning per blocked period, and a quiet all-clear once requests work again."""
    key = f"{source}_block_notified"
    name = name or source.title()
    if blocked and not state.get(key):
        reason = last_refusal.get(source)
        notify(cfg, f"{name} watcher blocked",
               f"Requests are being refused{f' ({reason})' if reason else ''}. "
               f"Pausing {backoff // 60} min, backing off further if it persists.",
               priority=3, tags=["warning"], dry_run=dry_run)
        state[key] = time.time()
    elif not blocked and state.get(key):
        since = state.pop(key)
        took = ""
        if not isinstance(since, bool):  # older state stored True
            mins = int((time.time() - since) // 60)
            took = f" after {mins // 60} h {mins % 60} min" if mins >= 60 else f" after {mins} min"
        notify(cfg, f"{name} watcher back to normal", f"Requests work again{took}.",
               priority=2, tags=["white_check_mark"], dry_run=dry_run)


def run(config_path: Path, once: bool, dry_run: bool):
    cfg = load_config(config_path)
    state_path = (config_path.parent / cfg["settings"]["state_file"]).resolve()
    state = load_state(state_path)
    if "block_notified" in state:  # key name used before Apple support
        state["amazon_block_notified"] = state.pop("block_notified")
    state.pop("shop_block_notified", None)  # shops back off per store now, keyed by host
    amz = Amazon(cfg["settings"]["accept_language"])
    apple = new_session(cfg["settings"]["accept_language"])
    shops = ShopSessions(cfg["settings"]["accept_language"])
    fleet = ProxyFleet(cfg["settings"]["accept_language"])
    amz_backoff = apple_backoff = 0
    next_amz = next_apple = 0.0
    next_shop: dict[str, float] = {}  # per watch URL
    shop_backoff: dict[str, int] = {}  # per host: one store refusing us doesn't pause the others
    shop_paused_until: dict[str, float] = {}
    host_of = lambda w: urlparse(w.url).hostname or ""

    while True:
        try:
            cfg = load_config(config_path)  # pick up edits without a restart
        except Exception as e:
            log.error("config reload failed, keeping previous config: %s", e)
        s = cfg["settings"]

        if cfg["apple_watches"] and time.time() >= next_apple:
            ok = apple_round(apple, cfg, state, dry_run)
            apple_backoff = 0 if ok else _backoff(apple_backoff, APPLE_BACKOFF_START, APPLE_BACKOFF_MAX)
            _block_notice(cfg, state, "apple", not ok, apple_backoff, dry_run)
            if not ok:
                apple = new_session(s["accept_language"])
            next_apple = time.time() + (apple_backoff or _jittered(s["apple_interval_minutes"],
                                                                   s["apple_interval_minutes"] * 0.2))
            save_state(state_path, state)

        if cfg["products"] and time.time() >= next_amz:
            ok = amazon_round(amz, cfg, state, dry_run)
            amz_backoff = 0 if ok else _backoff(amz_backoff, BACKOFF_START, BACKOFF_MAX)
            _block_notice(cfg, state, "amazon", not ok, amz_backoff, dry_run)
            if not ok:
                # Fresh session: a CAPTCHA'd cookie jar tends to stay flagged.
                amz = Amazon(s["accept_language"])
            next_amz = time.time() + (amz_backoff or _jittered(s["interval_minutes"], s["jitter_minutes"]))
            save_state(state_path, state)

        if cfg["proxy_products"] and time.time() >= fleet.next_request:
            proxy_step(fleet, cfg, state, dry_run)
            save_state(state_path, state)

        due_shops = [w for w in cfg["shop_watches"]
                     if time.time() >= max(next_shop.get(w.url, 0), shop_paused_until.get(host_of(w), 0))]
        if due_shops:
            refused = shop_round(shops, cfg, state, dry_run, due_shops)
            for host in {host_of(w) for w in due_shops}:
                blocked = host in refused
                shop_backoff[host] = (_backoff(shop_backoff.get(host, 0), SHOP_BACKOFF_START, SHOP_BACKOFF_MAX)
                                      if blocked else 0)
                _block_notice(cfg, state, host, blocked, shop_backoff[host], dry_run,
                              name=host.removeprefix("www."))
                if blocked:
                    shops.pop(host, None)  # fresh session after the pause
                    shop_paused_until[host] = time.time() + shop_backoff[host]
            for w in due_shops:
                if host_of(w) not in refused:
                    minutes = w.interval_minutes or s["shop_interval_minutes"]
                    next_shop[w.url] = time.time() + _jittered(minutes, minutes * 0.2)
            save_state(state_path, state)

        if once:
            return
        due = [t for t, on in ((next_apple, cfg["apple_watches"]), (next_amz, cfg["products"])) if on]
        due += [max(next_shop.get(w.url, 0), shop_paused_until.get(host_of(w), 0)) for w in cfg["shop_watches"]]
        if cfg["proxy_products"]:
            due.append(fleet.next_request)
        pause = max(min(due) - time.time(), 5)
        log.info("sleeping %.1f min", pause / 60)
        time.sleep(pause)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml", type=Path)
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    ap.add_argument("--dry-run", action="store_true", help="log alerts instead of sending them")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.test_notify:
        notify(load_config(args.config), "Price watcher", "Test notification - alerts work.", tags=["white_check_mark"])
        return
    try:
        run(args.config, args.once, args.dry_run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
