# Price Watch

A small Python service that sends push notifications through [ntfy](https://ntfy.sh) and/or Telegram for:

- **Amazon products**, with two price limits per product:
  - **New**: the cheapest offer in "New" condition, from any seller
  - **Warehouse**: the cheapest offer sold by Amazon Warehouse (also called Amazon Resale / Retourenkauf)
- **Apple Certified Refurbished**: new listings on a refurbished category page whose title
  matches your keywords, with an optional price limit (see [Apple refurbished](#apple-refurbished))

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
sudo git clone -b claude/epic-mayer-wtcn80 https://github.com/s1r-mar71n/AmazonBot /opt/price-watch
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
  (15 min, doubling up to 2 h) and sends you one warning.

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
