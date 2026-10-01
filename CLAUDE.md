# ebay-sniper

Passive watcher for one specific, rare item on eBay. It searches several eBay
marketplaces through the official Browse API, filters out listings that are
damaged or the wrong variant, and sends a Telegram notification with photos and
a link. **The purchase is always made by the user, by hand, in the eBay app.**
Despite the repository name, this project does not snipe, bid or buy.

## Working with the user

- The user writes in Italian. Reply in Italian, with correct grammar and accents.
- Code, comments, docstrings, log messages, commit messages: English.
- No emoji and no non-ASCII characters in code, comments or log messages.
- Be direct: point out mistakes, weak assumptions and better alternatives.
- The user is an experienced Python developer (ML / data engineering). Prefer
  modern tooling and best practices over beginner explanations.

## Target item

- Vintage **Futura Quartz** wristwatch, "spider web" dial ("Spider Nest").
- Asymmetric, "melting" hexagonal case with pointed curved lugs; about
  32 x 39 mm lug to lug; base metal case with steel back; dial marked
  "FUTURA QUARTZ" and "JAPAN MOVT"; white mother-of-pearl dial with a black web
  and a small black spider. Some listings call it "moving spider" (the spider
  may be on a rotating disc).
- **Wanted variant**: the one in `reference_images/positive/` (silver-tone case,
  gold-tone inner bezel ring and crown, white MOP web dial).
- **Known other variant**: full gold-tone case (seen as "Gold Tone Moving Spider
  MOP"). Which variants are unwanted is still **TO CONFIRM with the user**.
- Prices vary wildly: sold for about USD 20 on eBay under a generic title,
  listed around USD 280 by vintage resellers. Generic titles are the main
  opportunity, so searches must be broad and the filtering must be visual.
- Names seen in the wild: "Futura Spider Nest", "Futura Spiderwebs",
  "Moving Spider MOP", "Halloween Spider Web". Sellers who do not know the brand
  use generic terms: "spider web watch", "orologio ragnatela",
  "Spinnennetz Uhr", "montre araignee", "asymmetrical / melting / Dali watch",
  plus typos such as "Futtura".

## Hard constraints (do not work around these)

1. **No automated purchasing, bidding, offers or checkout.** Not through the
   Buy Order API or Offer API (Limited Release, partner approval and contracts
   required) and not through browser automation (Playwright, Selenium, etc.).
   Since 2026-02-20 the eBay User Agreement prohibits, without eBay's
   permission, "buy-for-me agents, LLM-driven bots, or any end-to-end flow that
   attempts to place orders without human review". The risk is suspension of
   the user's eBay account. The notification links to the listing; the user
   buys or places a maximum bid manually (eBay proxy bidding handles the rest).
2. **No HTML scraping of eBay pages.** Only the official Browse API with the
   user's own keyset. Do not use the Finding API or the `ebaysdk` library: the
   Finding API was decommissioned in 2025.
3. **Keep all classification local.** The eBay API License Agreement restricts
   feeding data from "Restricted APIs" into third-party AI tools. Browse is
   probably not in that category, but to stay clearly within bounds do not send
   listing text or images to hosted LLM/vision APIs. Use local models only.
4. **Do not persist seller usernames or other user-identifying data.** The
   production keyset relies on the exemption from the Marketplace Account
   Deletion notifications, which is valid only if the app stores no eBay user
   data.
5. **Respect the rate limit**: the default Browse API budget is 5,000 calls per
   day per application. `check-config` must compute the expected daily calls
   and fail if the configuration exceeds a safety margin (target: at most 60%
   of the budget).
6. **Secrets live only in `.env`** (never in `config.toml`, code, logs, tests
   or fixtures).

## Architecture

One poll cycle:

1. **Search** every query on every marketplace, `sort=newlyListed`.
2. **Deduplicate** by `legacyItemId` against SQLite (the same listing shows up
   on several marketplaces and in several queries).
3. **Rules** on title, condition and price: `drop`, `flag` or `pass`.
4. **Details** via `getItem` only for new, non-dropped items (all photos,
   condition description, full description).
5. **Vision score** on all photos of the listing with a local image-embedding
   model compared against the reference sets.
6. **Notify** via Telegram if the score is above threshold; persist everything,
   including items below threshold, so thresholds can be recalibrated.

Modules (`src/ebay_sniper/`; M1 and M2 are implemented, the others are planned):

| Module | Responsibility |
| --- | --- |
| `config.py` | Load `config.toml` (tomllib) and `.env` (pydantic-settings); validate (unknown keys rejected, paths resolved against the config directory); compute API budget |
| `ebay/endpoints.py` | API roots for production and sandbox; environment of a keyset from its App ID |
| `ebay/auth.py` | OAuth client-credentials token, cached in memory until shortly before expiry; `httpx.Auth` flow with one refresh on 401 |
| `ebay/models.py` | Pydantic models for the Browse API fields used (no `seller`) |
| `ebay/browse.py` | `search`, `search_raw` (sanitized, for fixtures) and `get_item`; retries with backoff on 429/5xx |
| `models.py` | Domain objects: `Money`, `CurrencyConverter`, `Verdict`, `Listing` (built from an `ItemSummary`, merged with `getItem`, `total` = item or current bid + shipping + import charges) |
| `store.py` | SQLite (stdlib `sqlite3`, migrations via `PRAGMA user_version`): listings with details, verdict and notification state, established searches, runs with API usage |
| `rules.py` | Keyword/condition/price rules returning `drop` / `flag` / `pass` with reasons |
| `vision.py` | (M3) Lazy model load, image download with on-disk cache, embeddings, scoring |
| `notify/base.py` | `Notifier` protocol and `NotificationError`, so the pipeline does not depend on Telegram |
| `notify/telegram.py` | Bot API via httpx: photo with caption or silent album plus details message, URL button, fallback to text |
| `pipeline.py` | One poll cycle wiring the steps above |
| `app.py` | Composition root: HTTP clients, store and pipeline with their lifetimes |
| `logsetup.py` | Logging to stderr with redaction of registered secrets (tracebacks included) |
| `retry.py` | Backoff with jitter and `Retry-After` parsing |
| `cli.py` | `run-once`, `watch`, `check-config [--live]`, `search`; later `calibrate`, `digest` |

Behaviour implemented in M1 and M2 worth knowing before changing it:

- **Rules** run on every new listing, seeded ones included (for later
  review). `drop` goes to status `dropped` (never notified, kept with its
  reasons), otherwise the listing is a candidate. Candidates, newest first, get
  one `getItem` each up to `runtime.max_details_per_cycle`; the rules then run
  again with the condition notes and the precise total. Beyond the cap, on a
  `getItem` error or on a 404 (possibly a listing not yet visible to getItem),
  the listing is notified with search data (and a warning for the 404):
  never trade a notification for completeness.
- **Keyword matching**: NFKD + casefold, accents stripped, punctuation
  ignored, whole words, trailing `*` for prefixes. A drop keyword with a
  negation among the three preceding words only flags. Only the title and
  `conditionDescription` are checked, never the full description.
- **Price cap**: `Listing.total` in the marketplace currency, converted with
  `price.exchange_rates`; without a rate the listing is flagged, not dropped.
  Auctions use the current bid.

- **Seeding**: the first successful run of a (query, marketplace) pair stores
  its results as `seeded` without notifying them; only later cycles notify.
  This also applies when a query or marketplace is added
  (`runtime.seed_new_searches`).
- **Dedup order**: marketplaces are searched in configuration order and the
  first occurrence of a legacy id wins, so the first marketplace provides the
  URL and the currency.
- **Notification state**: `pending` until sent; a failure keeps it `pending`
  (retried next cycle, failed ones sorted last), `failed` after 10 attempts;
  above `max_notifications_per_cycle` the rest become `suppressed` and are
  listed in one message. Two consecutive failures end the notification phase.
- **Overlapping runs**: `runs` rows act as a lock; a `running` row younger than
  30 minutes makes a new cycle skip.
- **Secrets**: the Telegram token is in every Bot API URL. `TelegramClient`
  never includes httpx exception texts in its errors, httpx loggers are set to
  WARNING, and `logsetup` redacts registered secrets in every record.

Design choices:

- Synchronous `httpx.Client` is enough (a few dozen calls per cycle). One client
  per service, explicit timeouts.
- `run-once` must be idempotent and safe to call from an external scheduler
  (cron, systemd timer, Windows Task Scheduler). `watch` is a thin loop around
  it.
- The vision stack is an optional extra (`uv sync --extra vision`) so that M1
  and M2 run without torch.
- Log with the stdlib `logging` module, never `print` outside the CLI.
- The CLI reconfigures stdout/stderr with `errors="backslashreplace"`: on
  Windows, redirected output (scheduled task, log file) uses the ANSI code page
  (cp1252) and an emoji in an eBay title would otherwise crash `print` and
  lose log lines.

## eBay Browse API notes

Verify field names and behaviour against live responses and save sanitized
responses as test fixtures. The eBay sandbox has almost no real listings, so
develop against production with low call volume.

- **Sandbox**: `EBAY_ENVIRONMENT=sandbox` in `.env` switches the API root to
  `https://api.sandbox.ebay.com` (same paths, same OAuth scope URI). App IDs
  contain `-SBX-` or `-PRD-`, and `Secrets` rejects a keyset that does not
  match `EBAY_ENVIRONMENT`. `check-config --live` and `search` work in the
  sandbox; `run-once` and `watch` refuse it, because test listings in the
  database would also mark the searches as seeded and the first production
  run would notify everything already online. Verified live on 2026-10-01:
  token, search and response parsing work; sandbox items have no `image`, so
  their responses are not useful as fixtures.

- Token: `POST https://api.ebay.com/identity/v1/oauth2/token`, HTTP Basic auth
  with `client_id:client_secret`, body
  `grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope`.
  The token lasts about 2 hours.
- Search: `GET https://api.ebay.com/buy/browse/v1/item_summary/search` with
  `q`, `filter`, `sort=newlyListed`, `limit` (max 200).
  - Header `X-EBAY-C-MARKETPLACE-ID`: `EBAY_IT`, `EBAY_DE`, `EBAY_FR`,
    `EBAY_GB`, `EBAY_US`, ...
  - Header `X-EBAY-C-ENDUSERCTX: contextualLocation=country%3DIT%2Czip%3D<zip>`
    improves shipping cost estimates.
  - **By default only fixed-price items are returned.** Always pass
    `filter=buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER}`.
  - Useful filters: `deliveryCountry:IT`, `price:[..400],priceCurrency:EUR`,
    `conditionIds:{...}` (7000 = for parts or not working).
  - Keywords are ANDed; `(a, b)` means OR, which keeps the number of queries
    (and calls) low. Check that the OR syntax behaves as expected in M1.
  - Do not filter by category: category IDs differ per marketplace and this
    item is often listed in the wrong category.
- Details: `GET https://api.ebay.com/buy/browse/v1/item/{item_id}` returns
  `additionalImages`, `conditionDescription`, `description`, `itemEndDate`,
  `currentBidPrice`, `shippingOptions`, `itemWebUrl`.
- Search by image (`search_by_image`) exists but is an experimental method for
  approved developers only. Do not depend on it.
- Budget: `queries x marketplaces x (1440 / poll_minutes)` plus one `getItem`
  per new candidate. Example: 4 queries x 5 marketplaces every 20 minutes =
  1,440 calls per day.

Verified against the Browse API OpenAPI contract v1.20.4 (not yet against live
responses):

- `q` is truncated beyond **100 characters** (a lost closing parenthesis would
  change the query): `config.py` rejects longer queries, `*` and nested or
  unbalanced parentheses. The documented OR form is "comma-separated keywords
  surrounded by a single pair of parentheses"; queries with **two OR groups**
  are not documented and must be checked live with `ebay-sniper search`.
- `limit` max 200 (default 50); `offset` must be a multiple of `limit`.
- `sort=newlyListed` sorts by `itemOriginDate`, which is **kept when a listing
  is relisted**: on broad queries a relist can fall beyond the first page.
- An invalid `X-EBAY-C-MARKETPLACE-ID` silently falls back to `EBAY_US`:
  marketplaces are validated against a fixed list in `config.py`.
- `price` is a `ConvertedAmount`: `value`/`currency` in the marketplace
  currency, `convertedFromValue`/`convertedFromCurrency` with the seller's
  original amount when eBay converted it.
- `shippingOptions[].shippingCost` can be missing (`CALCULATED` shipping);
  `getItem` adds `importCharges` for eBay's international shipping programs.
- `seller.username` and `itemLocation` (street, city, postal code) are
  returned: never store them. `getItem` also echoes the buyer's postal code in
  `shipToLocationUsedForEstimate`; `sanitize_response` strips all of these.
- `getItems` (batch of 20) is Limited Release: use single `getItem` calls.
- HTTP 429 comes with `errorId` 2001; users report bursts of 429 well below the
  daily limit, so keep retries with backoff and give up on long `Retry-After`.

## Filtering

**Rules** (configured in `config.toml`, section `[rules]`):

- `drop`: clear physical damage in title or condition description (cracked
  crystal, missing hands, broken case or lugs) and prices above the cap.
- `flag` (notify with a warning, do not drop): "not working", "for parts",
  "needs battery" and condition 7000. On a quartz watch this is often just a
  dead battery.
- Rules must be case- and accent-insensitive and multilingual (IT, EN, DE, FR, ES).

**Vision** (optional extra `vision`):

- Default model: SigLIP via `open_clip` (`ViT-B-16-SigLIP`, pretrained
  `webli`), confirmed in `open_clip.list_pretrained()` with open_clip 3.3.0
  and torch 2.14 (CPU build on Windows). SigLIP 2 models (for example
  `ViT-B-16-SigLIP2`) are listed too and are a possible upgrade.
- Embed every photo of the listing, L2-normalize, and score as
  `max_sim(positives) - max_sim(negatives)` over all listing photos. When the
  labelled set grows (roughly 30 or more per class) switch to logistic
  regression or kNN on embeddings.
- Reference sets: `reference_images/positive/` (wanted variant, any angle) and
  `reference_images/negative/` (other colours, damaged pieces, similar but
  different watches). Cache the reference embeddings keyed by model name and
  file hash.
- The eBay image URL size suffix (for example `s-l225`) can be replaced with a
  larger one (`s-l1600`) for better embeddings; fall back to the original URL.
- Thresholds are **calibrated**, not guessed: a `calibrate` command scores the
  labelled set and reports precision/recall at candidate thresholds. Missing
  the item (false negative) is much worse than a useless notification, so bias
  toward recall.
- A daily `digest` of near misses (below threshold, not dropped by rules) is a
  safety net against false negatives.

## Notifications

- Telegram Bot API through httpx (no heavy bot framework needed for sending).
- Message: best photo(s), title, total price (item + shipping, with currency),
  buying format, auction end time when relevant, vision score, rule flags, and a
  URL button to `itemWebUrl` (on a phone it opens the eBay app).
- Later (M4): inline buttons "target" / "not target" to collect labels, which
  requires polling `getUpdates`.

## Milestones

- **M1** (done): config, auth, search, SQLite dedup, Telegram notification for
  every new result, `run-once` and `check-config`, tests with respx fixtures.
  `check-config --live` verified with a sandbox keyset. Still to do with a
  production keyset: verify the OR syntax with `search`, replace the
  synthetic fixtures with live ones.
- **M2** (done): rules (`drop` / `flag`), total price with shipping and import
  charges, auction details (bids, next minimum bid, reserve, end time),
  `getItem` details for candidates. Possible follow-up: re-evaluate listings
  dropped for their price when a later search shows a lower price (today a
  price drop below the cap goes unnoticed).
- **M3**: vision scoring, `calibrate`, thresholds, near-miss `digest`.
- **M4**: Telegram feedback buttons feeding the labelled set.
- **M5**: deployment on an always-on machine (Linux systemd timer, Docker, or
  Windows Task Scheduler), daily heartbeat and alert after repeated failures
  (expired keys, API errors).

## Commands

```bash
uv sync                      # core + dev dependencies
uv sync --extra vision       # adds torch, open_clip, pillow (M3)
uv run ebay-sniper --help
uv run ebay-sniper check-config [--live]
uv run ebay-sniper search "<query>" -m EBAY_IT [--save-json tests/fixtures/x.json]
uv run pytest
uv run ruff check . && uv run ruff format .
```

`uv sync` resolves the whole project, `vision` extra included, so it needs
`download.pytorch.org`. Where that host is blocked (for example a sandbox
with restricted egress), install core and dev tools without the lock:
`uv venv && uv pip install -e . pytest respx ruff`. `uv.lock` is committed and
universal (torch `+cpu` from the PyTorch index on Linux, from PyPI elsewhere,
`tzdata` on Windows only); after changing dependencies, rerun `uv lock` on a
machine that reaches the PyTorch index.

On Linux, `pyproject.toml` pulls torch and torchvision from the PyTorch
CPU-only index to avoid multi-gigabyte CUDA wheels. CPU is enough: only a few
images per new listing.

## Testing

- No network access in tests: mock HTTP with `respx`, using sanitized fixtures
  of real responses in `tests/fixtures/`. The current fixtures are synthetic
  (written from the OpenAPI contract, see `tests/fixtures/README.md`); capture
  live ones with `search --save-json`.
- Warnings are errors (`filterwarnings = ["error"]`). Test data builders live
  in `tests/factories.py`; all credentials in tests are fake.
- The suite passes on Python 3.12 (`.python-version`) and 3.14, on Linux and
  Windows. From 3.13 `sqlite3` emits `ResourceWarning` for connections that
  are never closed, which fails the run: always close a `Store` (use `with`,
  also in fixtures). To test another version without touching `.venv`:
  `UV_PROJECT_ENVIRONMENT=<scratch dir> uv run --python 3.14 pytest`.
- Unit-test rules and scoring logic with synthetic inputs; the vision model is
  not loaded in the default test run (mark those tests and skip them unless the
  `vision` extra is installed).

## Open questions for the user

- Which variants are unwanted (gold-tone case? other dials?).
- Maximum total price.
- Which machine will run the bot, and at what poll interval.
- Which marketplaces to include (default: IT, DE, FR, GB, US).
- The keyword lists in `config.toml` are a first multilingual draft: review
  them, especially the drop list (a wrong drop can cost the item).
- `buyer_postal_code` is committed with `config.toml`: a generic postal code
  of the area is enough for shipping estimates if the repository is shared.
- `price.exchange_rates` are approximate values written in September 2026.
