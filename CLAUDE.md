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

Planned modules (`src/ebay_sniper/`):

| Module | Responsibility |
| --- | --- |
| `config.py` | Load `config.toml` (tomllib) and `.env` (pydantic-settings); validate; compute API budget |
| `ebay/auth.py` | OAuth client-credentials token, cached until shortly before expiry |
| `ebay/browse.py` | `search` and `get_item`, typed models for the fields used, retries with backoff on 429/5xx |
| `store.py` | SQLite (stdlib `sqlite3`): seen listings, verdicts, scores, notification state, labels |
| `rules.py` | Keyword/condition/price rules returning `drop` / `flag` / `pass` with reasons |
| `vision.py` | Lazy model load, image download with on-disk cache, embeddings, scoring |
| `notify/telegram.py` | Bot API via httpx: photo or media group, caption, URL button |
| `pipeline.py` | One poll cycle wiring the steps above |
| `cli.py` | `run-once`, `watch`, `check-config`; later `calibrate`, `digest` |

Design choices:

- Synchronous `httpx.Client` is enough (a few dozen calls per cycle). One client
  per service, explicit timeouts.
- `run-once` must be idempotent and safe to call from an external scheduler
  (cron, systemd timer, Windows Task Scheduler). `watch` is a thin loop around
  it.
- The vision stack is an optional extra (`uv sync --extra vision`) so that M1
  and M2 run without torch.
- Log with the stdlib `logging` module, never `print` outside the CLI.

## eBay Browse API notes

Verify field names and behaviour against live responses and save sanitized
responses as test fixtures. The eBay sandbox has almost no real listings, so
develop against production with low call volume.

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
  `webli`). Check the name with `open_clip.list_pretrained()`; SigLIP 2 models
  are a possible upgrade.
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

- **M1**: config, auth, search, SQLite dedup, Telegram notification for every
  new result, `run-once` and `check-config`, tests with respx fixtures.
- **M2**: rules (`drop` / `flag`), total price with shipping, auction details.
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
uv run pytest
uv run ruff check . && uv run ruff format .
```

On Linux, `pyproject.toml` pulls torch and torchvision from the PyTorch
CPU-only index to avoid multi-gigabyte CUDA wheels. CPU is enough: only a few
images per new listing.

## Testing

- No network access in tests: mock HTTP with `respx`, using sanitized fixtures
  of real responses in `tests/fixtures/`.
- Unit-test rules and scoring logic with synthetic inputs; the vision model is
  not loaded in the default test run (mark those tests and skip them unless the
  `vision` extra is installed).

## Open questions for the user

- Which variants are unwanted (gold-tone case? other dials?).
- Maximum total price.
- Which machine will run the bot, and at what poll interval.
- Which marketplaces to include (default: IT, DE, FR, GB, US).
