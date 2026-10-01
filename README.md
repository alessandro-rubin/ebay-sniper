# ebay-sniper

Passive watcher for one specific, rare item on eBay (a vintage Futura Quartz
"spider web" watch). It searches several eBay marketplaces through the official
Browse API, filters out damaged or wrong-variant listings using rules and local
image similarity, and sends a Telegram notification with photos and a link.

The purchase is always made manually in the eBay app: automated buying or
bidding is prohibited by the eBay User Agreement without eBay's permission.
See `CLAUDE.md` for design, constraints and milestones.

Status: milestones M1 and M2 (search, deduplication, keyword/condition/price
rules, item details, Telegram notifications) and the first part of M3 (photo
comparison with a local model, in shadow mode, and `calibrate`). The other
milestones are listed in `CLAUDE.md`.

## Setup

1. **eBay keyset**: create a developer account at
   <https://developer.ebay.com>, then a Production keyset. To activate it, eBay
   asks for an endpoint for Marketplace Account Deletion notifications or an
   exemption: choose the exemption (this app does not store eBay user data).
   A Sandbox keyset (`EBAY_ENVIRONMENT=sandbox`) is enough to try the
   credentials and `search`, but the sandbox holds only test listings, so
   `run-once` and `watch` refuse it.
2. **Telegram bot**: create a bot with @BotFather, send it a message, then read
   your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. **Secrets**: `cp .env.example .env` and fill in the values. Variables
   set in the environment take precedence over `.env`.
4. **Configuration**: review `config.toml` (queries, marketplaces, price cap,
   postal code).
5. **Install**: `uv sync` (add `--extra vision` for the image filter).
6. **Check**: `uv run ebay-sniper check-config --live` validates the
   configuration, prints the daily API budget, requests an eBay token and
   checks the Telegram bot and chat (it sends nothing).

## Usage

```bash
uv run ebay-sniper check-config           # configuration, secrets, API budget
uv run ebay-sniper check-config --live    # also verify eBay and Telegram credentials
uv run ebay-sniper search "futura (spider, ragno)" -m EBAY_IT -m EBAY_DE
uv run ebay-sniper notify-test "watch" -m EBAY_US   # send the newest result to Telegram
uv run ebay-sniper calibrate              # score references and stored listings, suggest thresholds
uv run ebay-sniper report --match 0.76    # local HTML page: listings, photos, scores
uv run ebay-sniper run-once               # one poll cycle, for a scheduler
uv run ebay-sniper watch                  # poll forever at the configured interval
```

Global options go before the command: `--config PATH` (default
`config.toml`), `--env-file PATH` (default: `.env` next to the configuration
file) and `-v` for debug logs. Logs go to stderr; secrets are redacted.

**First run.** The first successful run of each (query, marketplace) pair
records the listings that are already online without notifying them, and sends
one summary message. From then on only new listings are notified. The same
happens when a query or a marketplace is added. Set `seed_new_searches = false`
in `config.toml` to be notified of everything once instead.

**Rules.** Every new listing gets a verdict from `[rules]` and `[price]`:
`drop` (clear damage in the title or in the seller's condition notes, or a
total above `max_total`) is recorded but not notified; `flag` (for example
"not working" or condition 7000, often just a dead battery) is notified with a
warning; `pass` is notified. Matching is case- and accent-insensitive on whole
words, with `*` for prefixes; a drop keyword preceded by a negation ("no
cracked crystal") only flags. Listings that pass get one `getItem` call
(at most `max_details_per_cycle` per cycle) for all photos, condition notes,
precise shipping and import charges, and the rules run again on that data.
`search` prints the verdict of each result, which helps tune the keywords.

**Flood protection.** At most `max_notifications_per_cycle` listings are
notified one by one per cycle; the rest are listed with links in a single
message. A notification that fails (for example Telegram unreachable) is
retried in the next cycles.

**Trying queries.** `search` prints the newest results of one query (one
Browse API call per marketplace). Use it to check the keyword syntax, in
particular queries with two OR groups such as `(spider, web) (watch, quartz)`,
which the eBay documentation does not describe explicitly. `--save-json PATH`
saves the response without seller data, to be used as a test fixture.

**Testing notifications.** `notify-test` takes the newest result of a query
(default: the first configured query on the first marketplace), fetches its
details, applies the rules and sends it to Telegram exactly like a real
notification, with `[TEST]` before the title. It is sent even if the rules
would drop it (the verdict is printed), costs two Browse API calls and stores
nothing. It works with a sandbox keyset too, for example
`notify-test "watch" -m EBAY_US`.

**Photo comparison.** With `[vision] enabled = true` (and
`uv sync --extra vision`), the photos of every new listing are compared with
`reference_images/` by a local SigLIP model: nothing is sent to external
services. The first use downloads the model (about 800 MB). Two scores appear
in each notification: `match` (how close the best photo is to the wanted
watch) and `colour` (silver-tone above zero, gold-tone below). With
`filter = false` (shadow mode) they are only shown; with `filter = true`
listings below `match_threshold` or `colour_threshold` are not notified but
kept in the database. `calibrate` scores each reference image against the
others, suggests thresholds that keep every positive, then scores the stored
listings and prints the closest ones, so you can see what the thresholds would
let through. It makes no Browse API call; photos and embeddings are cached in
`data/image_cache`. `report` writes `data/report.html` with every stored listing,
its photos and scores, ranked by `match`, and opens it in the browser; with
`--match` and `--colour` it marks what those thresholds would let through.

## Scheduling

`run-once` is idempotent and refuses to start while another run is active, so
it is safe to call from cron, a systemd timer or Windows Task Scheduler. Use
absolute paths, since the scheduler's working directory is not the project:

```cron
*/20 * * * * /path/to/ebay-sniper/.venv/bin/ebay-sniper --config /path/to/ebay-sniper/config.toml run-once 2>> /path/to/ebay-sniper/data/ebay-sniper.log
```

Keep the schedule consistent with `poll_interval_minutes`: `check-config`
computes the API budget from it. Relative paths inside `config.toml`
(database, image cache, reference images) are resolved against the directory
of `config.toml`. The exit code is 0 on
success and 1 when the cycle failed (for example every search failed or the
eBay credentials were rejected).

## Known limitations

- `sort=newlyListed` orders by `itemOriginDate`, which eBay keeps when a
  listing is relisted. On broad queries with many results, a relisted item can
  sit beyond the first page and go unnoticed.
- Prices are shown in the currency of the marketplace that returned the
  listing (eBay converts them); the seller's own price is shown next to it
  when a conversion happened. The price cap converts other currencies with
  the approximate `exchange_rates` of `config.toml`.
- A listing dropped for its price is not evaluated again if the seller later
  lowers the price.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```

Tests never touch the network: HTTP is mocked with `respx`. The fixtures in
`tests/fixtures/` are currently synthetic (see the README there).
