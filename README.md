# Price Watch

A small Python service that sends push notifications through [ntfy](https://ntfy.sh) and/or Telegram for:

- **Amazon products**, from the home IP or spread over a proxy pool, with two price limits per product:
  - **New**: the cheapest offer in "New" condition, from any seller
  - **Warehouse**: the cheapest offer sold by Amazon Warehouse (also called Amazon Resale / Retourenkauf)
- **Apple Certified Refurbished**: new listings on a refurbished category page whose title
  matches your keywords, with an optional price limit (see [Apple refurbished](#apple-refurbished))
- **Shopify stores** such as the MSI store, and **Cyberport** and **notebooksbilliger.de** listing
  pages: matching products that come into stock (see [Shopify stores](#shopify-stores-eg-msi),
  [Cyberport](#cyberport) and [notebooksbilliger.de](#notebooksbilligerde))

It only notifies you and never buys anything. The old Chrome extension that auto-bought
items is still in the git history (commit `7922bcd`).

## How it works (Amazon)

In each round the watcher makes **one request per product** to Amazon's offer list (the "All
offers" panel on a product page). It reads every offer's price, condition and seller, keeps the
cheapest New offer and the cheapest Warehouse offer, and compares them with your limits. Before
the first round it also loads the Amazon homepage once to get normal session cookies.

- **No repeat alerts:** you're alerted once per deal. You get another alert only if the price drops
  further or `renotify_hours` has passed. When the price goes back above your limit, the next
  drop alerts you again.
- **Edit the config while it runs:** `config.toml` is re-read every round, so you can add or remove
  links without restarting.
- **Short links work:** a short link like `https://amzn.eu/d/...` is resolved once and the result
  is stored in `state.json`.

## Install on a Linux server

```bash
sudo useradd --system --home /opt/price-watch --shell /usr/sbin/nologin pricewatch
sudo git clone https://github.com/s1r-mar71n/AmazonBot /opt/price-watch
cd /opt/price-watch
sudo python3 -m venv .venv                       # Python 3.10+
sudo .venv/bin/pip install -r requirements.txt
sudo cp config.example.toml config.toml && sudo nano config.toml
sudo chown -R pricewatch:pricewatch /opt/price-watch

# test it
sudo -u pricewatch .venv/bin/python watch.py --test-notify        # should buzz your phone
sudo -u pricewatch .venv/bin/python watch.py --once --dry-run -v  # one round, logs alerts only

# run as a service
sudo cp deploy/price-watch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now price-watch
journalctl -u price-watch -f
```

It uses about 50 MB of RAM and needs two dependencies:
[`curl_cffi`](https://github.com/lexiforest/curl_cffi), which makes requests look like a real
Chrome browser, and `beautifulsoup4` for reading the pages. Pre-built `curl_cffi` packages exist
for x86_64 and ARM64, so a Raspberry Pi 4/5 works too.

## ntfy setup

1. Install the ntfy app (Android or iOS) and subscribe to a topic name that's hard to guess.
   Anyone who knows a topic name on ntfy.sh can read its messages.
2. Put the same name in `[ntfy] topic`. If you self-host ntfy or protect the topic, also set
   `server` and `token`.

Each alert opens the product page when you tap it.

## Telegram setup

Set `[telegram] bot_token` and `chat_id`. The bot must be allowed to post to that chat; for a
channel, make it an admin. Alerts carry an "Open" button with the product link. If both ntfy and
Telegram are configured, alerts go to both.

## Run from cron (no root)

Instead of the systemd unit, you can let cron keep the watcher alive, as on the homeserver:

```
*/2 * * * * flock -n /home/martin/price-watch/run.lock /home/martin/price-watch/deploy/run.sh
```

`deploy/run.sh` starts `watch.py` with the venv's Python, logs to `watch.log` (rotated at 20 MB),
and restarts it within 2 minutes after a crash or reboot. Python 3.10 works too (`tomli` is
installed automatically).

## Configuration

See [`config.example.toml`](config.example.toml). Example product entry:

```toml
[[product]]
url = "https://amzn.eu/d/01xSbc0F"
max_new = 300
max_warehouse = 250
```

Leave out `max_new` or `max_warehouse` to ignore that kind of offer.

## Amazon over proxies

For many products, the offer lists can go through a proxy pool instead of the home IP. This is
built to be careful with the proxies:

```toml
[proxies]
rayobyte_secret = "/home/martin/priceapi/skinbaron-secret.yml"   # or list_file = "proxies.txt"
per_proxy_interval_minutes = 10

[[product]]
urls = ["https://www.amazon.de/dp/B0DSQ1RCZV", "https://www.amazon.de/dp/B0DS65VMC6"]
max_warehouse = 1400
proxy = true
```

- **Spread out:** one request at a time, each proxy used at most every `per_proxy_interval_minutes`,
  and the pool sends one request every interval ÷ active proxies (20 proxies at 10 min: one
  every ~30 s, 144 requests per IP and day). Products are checked oldest-first.
- **Only the offer list:** one request per check, no homepage visit, no search, redirects not
  followed. (Since October 2026 Amazon answers the homepage with an AWS WAF JavaScript challenge
  even from a home IP, and search pages through datacenter proxies with an Akamai challenge; the
  offer list answers normally.)
- **Halt at the first refusal:** a CAPTCHA, an AWS WAF or Akamai challenge, any status other than
  200, or a page that isn't an offer list halts that proxy for `halt_hours` (24, doubling if it's
  refused again within a week, at most 7 days) and alerts you right away. When it works again
  you get a quiet "back in use" message. Halts are stored in `state.json` and survive restarts.
- **Circuit breaker:** `breaker_halts` (3) refusals within `breaker_window_hours` (6) pause all
  proxied checks for `breaker_pause_hours` (12), because then Amazon is probably flagging the
  pattern rather than single IPs.
- **Not Amazon's doing:** a proxy that can't be reached (timeout, 407) sits out 30 minutes
  without an alert, and a 404 (the product is gone) sends one quiet "product not found".
- **Credentials** are never logged; proxies show up as `#index ip`. The Rayobyte list is
  re-read every 6 hours, since Rayobyte replaces IPs now and then.

## Apple refurbished

```toml
[[apple]]
url = "https://www.apple.com/de/shop/refurbished/mac/mac-studio"
match = ["Mac Studio"]      # every word must appear in the listing title
# exclude = ["M2"]          # skip titles containing any of these
# max_price = 2500          # optional price limit
```

- **Why `match` is required:** when a category is sold out, Apple doesn't show an empty page. It
  shows the whole parent category (all refurbished Macs) on the same URL, so the watcher filters
  listings by their title.
- **One alert per listing:** each listing (Apple part number) alerts once. It alerts again if its
  price drops, or if it disappears and comes back later.
- **Prices in alerts:** alerts show the refurbished price, plus the original price and the
  discount where Apple lists them.
- **Shared requests:** each distinct URL is fetched once per round, every `apple_interval_minutes`
  (default 10, minimum 2). Several `[[apple]]` entries with the same URL share that one request.
- **Faster than Amazon:** Apple pages run on their own schedule, so Apple can be checked more often
  than Amazon. Refurbished Mac Studios often sell out within hours.
- **Blocking is rarely an issue:** Apple's store has no Amazon-style CAPTCHA wall, and 6–12
  requests an hour is negligible traffic. If Apple answers 403/429/503, the watcher backs off
  (15 min, then 30 min) and sends you one warning with the HTTP status. A 503 usually means
  Apple's store itself is down or overloaded rather than a block, so the watcher keeps
  checking every 30 minutes and sends a quiet "back to normal" message once the page loads again.

## Shopify stores (e.g. MSI)

```toml
[[shop]]
url = "https://de-store.msi.com/collections/geforce-rtx-5090-series-gpu"
match = ["5090"]            # every word must appear in the product title
types = ["VGA"]             # Shopify product_type; keeps "RTX 5090 Laptop GPU" laptops out
interval_minutes = 1
```

- **Several models in one entry:** `match_any = ["5090", "5080", "5070 Ti", "9070 XT"]` alerts on
  a title containing at least one of them (combine with `match` for words that must all appear).
  Spaces don't count when comparing, so "5070 Ti" also matches "RTX 5070Ti".
- **A price limit per model:** write `match_any` as a table instead,
  `match_any = { "5090" = 5000, "5080" = 1400, "5070 Ti" = 1000, "9070 XT" = 750 }`: a listing
  matches when its title contains one of them and its price is at most that model's limit.

- **How it reads the store:** through Shopify's public `products.json` feed of the collection
  (or of the whole store, if `url` is the store root), so there's no HTML to break.
- **One alert per variant:** when it's in stock, again if it sells out and comes back, and on a
  price drop. The same variant seen through two feeds alerts once.
- **Collections hide sold-out products**, and a card can be listed outside the collection (MSI's
  refurbished/outlet cards are), so add a slower `[[shop]]` entry for the store root as a backstop.
  On MSI that's about 4 requests and 4 MB per check, so every 10 minutes is plenty.
- **Sold-out listings:** with `sold_out_alerts = true`, a new listing that is already sold out
  sends a quiet alert (silent in Telegram, low priority in ntfy). Only a store-root entry sees
  these, because collections hide sold-out products.
- **Blocking:** on HTTP 403/429/430/503 it backs off (15 min, doubling up to 2 h), sends one
  warning, and sends a quiet "back to normal" message once requests work again. This applies to
  each store separately: a store that refuses us doesn't pause the others.

## Cyberport

A `[[shop]]` entry whose URL is a Cyberport category page (cyberport.at or .de, with any filters
and sorting from the browser's address bar) is read like a Shopify feed:

```toml
[[shop]]
url = "https://www.cyberport.at/markenshops/outlet/gebrauchtware-b-ware-/pc-zubehoer/komponenten/grafikkarten.html?sortBy=PRD-2B1T_de_price_desc&refinementList%5Bfacets.Grafikchiphersteller.values%5D%5B0%5D=NVIDIA"
match = ["5090"]
interval_minutes = 2
sold_out_alerts = true
```

- **How it reads the page:** one request per check. The page embeds the search results it shows
  (Cyberport's Algolia data), so nothing is rendered or clicked.
- **Only the first 20 results:** that's what one page holds, and `page=` in the URL is ignored.
  Narrow the URL with filters (e.g. add the "Grafikchip: GeForce RTX 5090" filter on the site and
  copy the URL) so everything you care about fits; the log warns when a page has more.
- **Rich alerts:** price (the webshop price of the .at or .de shop), condition text of B-stock
  ("Wie Neu | OVP geöffnet …"), availability, the listing's bullet points, item number,
  manufacturer part number, EAN and listing date, plus the product photo (Telegram sends it as a
  photo with the text as caption, ntfy as an attachment) and the direct product link.
- **Available** means anything but "UNAVAILABLE" (soon-available items can be ordered).

## notebooksbilliger.de

A `[[shop]]` entry whose URL is a notebooksbilliger.de category or outlet page (with filters from
the browser's address bar):

```toml
[[shop]]
url = "https://www.notebooksbilliger.de/vorfuehrware+gebrauchtware/b+ware+ruecklaeufer?box_65666_17978%5B%5D=143"
match_any = ["5090", "5080", "5070 Ti", "9070 XT"]
interval_minutes = 2
sold_out_alerts = true
```

- **How it reads the page:** one request per check; the product cards are in the HTML.
- **Bot protection:** the site sits behind Akamai Bot Manager, which answers a Chrome-like client
  with a fake "404 Die Seite wurde nicht gefunden", even on the home page. The watcher uses a
  Safari 17 fingerprint for this store, which gets the real page. A 404 or a page without a
  product listing counts as a refusal (backoff and warning as above).
- **One request per IP:** Akamai lets an IP's first request through and refuses the next ones for
  10+ minutes (longer while it keeps asking), so polling every few minutes from one IP gets
  blocked. Add `proxy = true` to the entry to send each request over the next proxy of the
  [`[proxies]`](#amazon-over-proxies) pool: with 20 proxies and `interval_minutes = 2`, each
  IP sees one request every 40 minutes. A proxy the store refuses sits out that store for an hour
  and the next one is tried at once; only when that one is refused too does the store back off.
- **Only the first 20 results:** that's one page. The log warns when a full page has no "no more
  products" marker; narrow the URL with filters then.
- **Alerts:** price, condition ("B-Ware"), availability, the card's spec lines, item number and
  manufacturer, plus the product photo and link.

## Amazon rate limits: read this before adding many products

Amazon does **not** publish rate limits for its website. Automated access is against its
Conditions of Use, and it blocks scrapers with heuristics instead of a fixed quota. What matters
in practice:

| Factor | Effect |
|---|---|
| **Type of IP address** | This matters most. Data-center IPs (Hetzner, AWS, DigitalOcean, OVH, …) get CAPTCHAs far sooner than home internet connections. A Raspberry Pi or home server is a much better host than a VPS. |
| **Request rate and bursts** | Steady, spread-out requests are tolerated much better than bursts. That's why the watcher spaces products 20–60 s apart and randomises the interval between rounds. |
| **Client fingerprint** | Plain Python HTTP clients are recognised by their TLS fingerprint. `curl_cffi` makes requests look like Chrome. |
| **Cookies and session** | The watcher keeps one cookie session, and starts a new one after a CAPTCHA. |

**Daily request count** ≈ `number of products × 1440 / interval_minutes`. A few examples:

| Products | Interval | Requests/day |
|---|---|---|
| 5 | 20 min | ~360 |
| 10 | 20 min | ~720 |
| 20 | 30 min | ~960 |
| 30 | 60 min | ~720 |

There's no official number. A rough guideline from community experience, not from Amazon:
staying **below ~1,000 requests a day from a home IP** is usually fine, while a data-center IP
can get CAPTCHAs at a fraction of that. Start small, watch the logs, and lengthen the interval
when you add more products. Good deals on Warehouse items often last hours, so a 20–30 minute
interval rarely makes you miss one.

**When Amazon blocks the watcher:** it detects the CAPTCHA page, stops the current round and
sends you one ntfy warning. It then waits 30 minutes, doubling the wait each time it's still
blocked (up to 6 hours), and continues normally once requests succeed again.

**Official alternatives, if you need more scale:**
- The Amazon Product Advertising API requires an Amazon Associates account with recent qualifying
  sales, and its request allowance depends on that revenue.
- [Keepa](https://keepa.com) offers a paid API, and its free website can already send price alerts
  for New and Warehouse offers.

## Limitations

- **Scraped markup changes.** Both parsers were verified against live amazon.de and apple.com/de on
  2026-10-14 (Amazon's offer list now lives at `/gp/product/ajax/aodAjaxMain`). If the log shows
  `no offers parsed` (Amazon) or `no tiles parsed` (Apple), the markup has changed again and
  `parse_offers()` / `parse_refurb()` need adjusting. Run `--once --dry-run -v` after any change.
- **Shipping costs are ignored.** Only the item price is compared with your limit.
- **Only the first page of offers is read** (about 10 offers, cheapest first). For products with
  many sellers, a Warehouse offer could in theory sit beyond that first page.
- **Warehouse sellers are matched by name or seller ID.** If Amazon renames Warehouse again, update
  `warehouse_sellers` / `warehouse_seller_ids` in the config.

## Tests

```bash
python3 -m unittest discover -s tests
```
