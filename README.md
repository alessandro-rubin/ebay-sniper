# ebay-sniper

Passive watcher for one specific, rare item on eBay (a vintage Futura Quartz
"spider web" watch). It searches several eBay marketplaces through the official
Browse API, filters out damaged or wrong-variant listings using rules and local
image similarity, and sends a Telegram notification with photos and a link.

The purchase is always made manually in the eBay app: automated buying or
bidding is prohibited by the eBay User Agreement without eBay's permission.
See `CLAUDE.md` for design, constraints and milestones.

## Setup

1. **eBay keyset**: create a developer account at
   <https://developer.ebay.com>, then a Production keyset. To activate it, eBay
   asks for an endpoint for Marketplace Account Deletion notifications or an
   exemption: choose the exemption (this app does not store eBay user data).
2. **Telegram bot**: create a bot with @BotFather, send it a message, then read
   your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. **Secrets**: `cp .env.example .env` and fill in the four values.
4. **Configuration**: review `config.toml` (queries, marketplaces, price cap).
5. **Install**: `uv sync` (add `--extra vision` for the image filter).

## Usage

```bash
uv run ebay-sniper check-config
uv run ebay-sniper run-once
uv run ebay-sniper watch
```

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```
