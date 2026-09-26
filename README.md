# ebay-sniper

Passive watcher for one specific, rare item on eBay (a vintage Futura Quartz
"spider web" watch). It searches several eBay marketplaces through the official
Browse API, filters out damaged or wrong-variant listings using rules and local
image similarity, and sends a Telegram notification with photos and a link.

The purchase is always made manually in the eBay app: automated buying or
bidding is prohibited by the eBay User Agreement without eBay's permission.
See `CLAUDE.md` for design, constraints and milestones.

Status: milestone M1 (search, deduplication, Telegram notification of every new
listing). Rules, vision filtering and the other milestones are listed in
`CLAUDE.md`.

## Setup

1. **eBay keyset**: create a developer account at
   <https://developer.ebay.com>, then a Production keyset. To activate it, eBay
   asks for an endpoint for Marketplace Account Deletion notifications or an
   exemption: choose the exemption (this app does not store eBay user data).
2. **Telegram bot**: create a bot with @BotFather, send it a message, then read
   your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. **Secrets**: `cp .env.example .env` and fill in the four values. Variables
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

**Flood protection.** At most `max_notifications_per_cycle` listings are
notified one by one per cycle; the rest are listed with links in a single
message. A notification that fails (for example Telegram unreachable) is
retried in the next cycles.

**Trying queries.** `search` prints the newest results of one query (one
Browse API call per marketplace). Use it to check the keyword syntax, in
particular queries with two OR groups such as `(spider, web) (watch, quartz)`,
which the eBay documentation does not describe explicitly. `--save-json PATH`
saves the response without seller data, to be used as a test fixture.

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
  when a conversion happened.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```

Tests never touch the network: HTTP is mocked with `respx`. The fixtures in
`tests/fixtures/` are currently synthetic (see the README there).
