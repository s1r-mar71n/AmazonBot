# Amazon Watch

A small Python service that watches Amazon products and sends a push notification through
[ntfy](https://ntfy.sh) when an offer reaches your price limit. It tracks two limits per product:

- **New**: the cheapest offer in "New" condition, from any seller
- **Warehouse**: the cheapest offer sold by Amazon Warehouse (also called Amazon Resale / Retourenkauf)

It only notifies you and never buys anything. The old Chrome extension that auto-bought
items is still in the git history (commit `7922bcd`).

## How it works

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
sudo useradd --system --home /opt/amazon-watch --shell /usr/sbin/nologin amazonwatch
sudo git clone -b claude/epic-mayer-wtcn80 https://github.com/s1r-mar71n/AmazonBot /opt/amazon-watch
cd /opt/amazon-watch
sudo python3 -m venv .venv                       # Python 3.11+ (needs tomllib)
sudo .venv/bin/pip install -r requirements.txt
sudo cp config.example.toml config.toml && sudo nano config.toml
sudo chown -R amazonwatch:amazonwatch /opt/amazon-watch

# test it
sudo -u amazonwatch .venv/bin/python amazon_watch.py --test-notify     # should buzz your phone
sudo -u amazonwatch .venv/bin/python amazon_watch.py --once --dry-run  # one round, logs alerts only

# run as a service
sudo cp deploy/amazon-watch.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now amazon-watch
journalctl -u amazon-watch -f
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

## Configuration

See [`config.example.toml`](config.example.toml). Example product entry:

```toml
[[product]]
url = "https://amzn.eu/d/01xSbc0F"
max_new = 300
max_warehouse = 250
```

Leave out `max_new` or `max_warehouse` to ignore that kind of offer.

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

- **Untested against live Amazon.** The page parsing was written for Amazon's current offer-list
  markup but not checked against live pages, because Amazon was unreachable from the environment
  where this was built. Run `--once --dry-run -v` first. If it logs `no offers parsed` for a
  product that's in stock, the HTML selectors in `parse_offers()` need adjusting.
- **Shipping costs are ignored.** Only the item price is compared with your limit.
- **Only the first page of offers is read** (about 10 offers, cheapest first). For products with
  many sellers, a Warehouse offer could in theory sit beyond that first page.
- **Warehouse sellers are matched by name or seller ID.** If Amazon renames Warehouse again, update
  `warehouse_sellers` / `warehouse_seller_ids` in the config.

## Tests

```bash
python3 -m unittest discover -s tests
```
