#!/usr/bin/env python3
"""Watch Amazon offers (new + Amazon Warehouse) and send ntfy alerts when a
price drops to or below the configured limit. Notify-only: never buys."""

import argparse
import json
import logging
import random
import re
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

log = logging.getLogger("amazon-watch")

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|o/ASIN)/([A-Z0-9]{10})(?:[/?#]|$)")
SHORT_HOSTS = {"amzn.eu", "amzn.to", "amzn.com", "a.co"}
NEW_WORDS = {"new", "neu", "neuf", "nuevo", "nuovo", "nieuw", "nowy"}
DEFAULT_WAREHOUSE_SELLERS = ["amazon warehouse", "amazon resale", "warehouse deals", "amazon retourenkauf"]
DEFAULT_WAREHOUSE_IDS = ["A8KICS1PHF7ZO"]  # Amazon Warehouse on amazon.de
CAPTCHA_MARKERS = ("validateCaptcha", "api-services-support@amazon.com", "<title>Robot Check</title>")

MIN_INTERVAL_MINUTES = 5
BACKOFF_START = 30 * 60
BACKOFF_MAX = 6 * 3600


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
    if settings["interval_minutes"] < MIN_INTERVAL_MINUTES:
        log.warning("interval_minutes < %d is asking for CAPTCHAs; using %d",
                    MIN_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES)
        settings["interval_minutes"] = MIN_INTERVAL_MINUTES

    ntfy = cfg.get("ntfy") or {}
    if not ntfy.get("topic"):
        raise ValueError("[ntfy] topic is required")
    ntfy.setdefault("server", "https://ntfy.sh")

    products = []
    for i, p in enumerate(cfg.get("product", []), 1):
        if not p.get("url"):
            raise ValueError(f"product #{i}: url is required")
        if p.get("max_new") is None and p.get("max_warehouse") is None:
            raise ValueError(f"product #{i}: set max_new and/or max_warehouse")
        products.append(Product(p["url"], p.get("name"), p.get("max_new"), p.get("max_warehouse")))
    if not products:
        raise ValueError("no [[product]] entries configured")
    cfg["products"] = products
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


def classify(condition: str, seller: str, seller_id: str, wh_names, wh_ids) -> str:
    seller_l = seller.lower()
    if seller_id in wh_ids or any(n in seller_l for n in wh_names):
        return "warehouse"
    first = re.split(r"[\s\-–:]+", condition.strip().lower(), maxsplit=1)[0] if condition.strip() else ""
    return "new" if first in NEW_WORDS else "other"


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _offer_price(el) -> float | None:
    for sel in (".a-price .a-offscreen", ".a-price .aok-offscreen", ".aok-offscreen"):
        p = parse_price(_text(el.select_one(sel)))
        if p:
            return p
    whole = _text(el.select_one(".a-price-whole"))
    frac = _text(el.select_one(".a-price-fraction"))
    return parse_price(f"{whole.rstrip('.,')},{frac or '00'}") if whole else None


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
        link = sold_by.select_one("a[href]") if sold_by else None
        seller = _text(link) or _text(sold_by.select_one(".a-col-right") if sold_by else None)
        m = re.search(r"seller=([A-Z0-9]+)", link["href"]) if link else None
        seller_id = m.group(1) if m else ""
        offers.append(Offer(price, condition, seller, seller_id,
                            classify(condition, seller, seller_id, wh_names, wh_ids)))
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
        if r.status_code in (429, 503) or any(m in r.text[:20000] for m in CAPTCHA_MARKERS):
            raise Blocked(f"HTTP {r.status_code} for {url}")
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
        url = (f"https://{host}/gp/product/ajax/ref=dp_aod_ALL_mbc?asin={asin}"
               "&m=&qid=&smid=&sourcecustomerorglistid=&sourcecustomerorglistitemid=&sr=&pc=dp"
               "&experienceId=aodAjaxMain")
        return self.get(url, headers={"Referer": f"https://{host}/dp/{asin}"}).text


# --------------------------------------------------------------------------- notifications

def ntfy(cfg: dict, title: str, message: str, click: str | None = None,
         priority: int = 4, tags: list[str] | None = None, dry_run: bool = False):
    n = cfg["ntfy"]
    body = {"topic": n["topic"], "title": title, "message": message,
            "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    if dry_run:
        log.info("[dry-run] ntfy: %s | %s", title, message)
        return
    headers = {"Content-Type": "application/json"}
    if n.get("token"):
        headers["Authorization"] = f"Bearer {n['token']}"
    # JSON publishing avoids header-encoding trouble with € and umlauts.
    req = urllib.request.Request(n["server"].rstrip("/"), data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:
        log.error("ntfy delivery failed: %s", e)


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
    s = cfg["settings"]
    resolved = state.setdefault("resolved", {})
    if p.url not in resolved:
        host, asin = amz.resolve(p.url)
        resolved[p.url] = {"host": host, "asin": asin}
    host, asin = resolved[p.url]["host"], resolved[p.url]["asin"]

    title, offers = parse_offers(amz.offers(host, asin), s["warehouse_sellers"], s["warehouse_seller_ids"])
    name = p.name or (title[:60] if title else asin)
    if not offers:
        log.warning("%s: no offers parsed (out of stock, or Amazon changed its markup)", name)

    best: dict[str, Offer] = {}
    for o in offers:
        if o.kind != "other" and (o.kind not in best or o.price < best[o.kind].price):
            best[o.kind] = o
    log.info("%s: new=%s warehouse=%s (%d offers)", name,
             best["new"].price if "new" in best else "-",
             best["warehouse"].price if "warehouse" in best else "-", len(offers))

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
                ntfy(cfg, f"{label}: {name}", msg, click=f"https://{host}/dp/{asin}",
                     tags=["moneybag"] if kind == "new" else ["package"], dry_run=dry_run)
                alerts[key] = {"price": o.price, "ts": now}
        else:
            alerts.pop(key, None)


def run(config_path: Path, once: bool, dry_run: bool):
    cfg = load_config(config_path)
    state_path = (config_path.parent / cfg["settings"]["state_file"]).resolve()
    state = load_state(state_path)
    amz = Amazon(cfg["settings"]["accept_language"])
    backoff = 0

    while True:
        try:
            cfg = load_config(config_path)  # pick up edits without a restart
        except Exception as e:
            log.error("config reload failed, keeping previous config: %s", e)
        s = cfg["settings"]

        products = list(cfg["products"])
        random.shuffle(products)
        blocked = False
        for i, p in enumerate(products):
            if i:
                time.sleep(random.uniform(*s["delay_between_products_seconds"]))
            try:
                check_product(amz, p, cfg, state, dry_run)
            except Blocked as e:
                blocked = True
                log.warning("Amazon is blocking us (%s); stopping this round", e)
                break
            except Exception:
                log.exception("check failed for %s", p.url)
        save_state(state_path, state)

        if blocked:
            backoff = min(backoff * 2, BACKOFF_MAX) if backoff else BACKOFF_START
            if not state.get("block_notified"):
                ntfy(cfg, "Amazon watcher blocked",
                     f"CAPTCHA/throttling detected. Pausing {backoff // 60} min, backing off further if it persists.",
                     priority=3, tags=["warning"], dry_run=dry_run)
                state["block_notified"] = True
                save_state(state_path, state)
            # Fresh session: a CAPTCHA'd cookie jar tends to stay flagged.
            amz = Amazon(s["accept_language"])
        elif backoff or state.get("block_notified"):
            backoff = 0
            state.pop("block_notified", None)
            save_state(state_path, state)

        if once:
            return
        pause = backoff or (s["interval_minutes"] + random.uniform(-1, 1) * s["jitter_minutes"]) * 60
        log.info("next round in %.1f min", pause / 60)
        time.sleep(max(pause, 60))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml", type=Path)
    ap.add_argument("--once", action="store_true", help="run a single round and exit")
    ap.add_argument("--dry-run", action="store_true", help="log alerts instead of sending them")
    ap.add_argument("--test-notify", action="store_true", help="send a test ntfy message and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.test_notify:
        ntfy(load_config(args.config), "Amazon watcher", "Test notification - ntfy works.", tags=["white_check_mark"])
        return
    try:
        run(args.config, args.once, args.dry_run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
