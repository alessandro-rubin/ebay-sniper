# ebay-sniper

Passive watcher for one specific, rare item on eBay (a vintage Futura Quartz
"spider web" watch). It searches several eBay marketplaces through the official
Browse API, filters out damaged or wrong-variant listings using rules and local
image similarity, and sends a Telegram notification with photos and a link.

The purchase is always made manually in the eBay app: automated buying or
bidding is prohibited by the eBay User Agreement without eBay's permission.
See `CLAUDE.md` for design, constraints and milestones.

Status: milestones M1 to M3 are done (search, deduplication,
keyword/condition/price rules, item details, Telegram notifications, photo
filter with a local model, `calibrate`, `report` and the daily near-miss
digest). The photo filter is on with provisional thresholds: no real listing
of the watch has been scored yet. Next: Telegram buttons to label listings
(M4) and deployment on an always-on machine (M5), see `CLAUDE.md`.

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
   postal code, photo thresholds) and the images in `reference_images/` (see
   the README there).
5. **Install**: `uv sync --extra vision`. The committed `config.toml` has the
   photo filter on, which needs the extra; plain `uv sync` is enough with
   `[vision] enabled = false`.
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
uv run ebay-sniper digest --dry-run       # near misses that the daily digest would send
uv run ebay-sniper run-once               # one poll cycle, for a scheduler
uv run ebay-sniper watch                  # poll forever at the configured interval
```

Global options go before the command: `--config PATH` (default
`config.toml`), `--env-file PATH` (default: `.env` next to the configuration
file), `--log-file PATH` and `-v` for debug logs. Logs go to stderr, or only
to the `--log-file` (UTF-8, rotated at 1 MB, three old files kept); secrets
are redacted.

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
services. The first use downloads the model (about 800 MB). Three scores
appear in each notification: `match` (how close the best photo is to the
wanted watch), `colour` (higher for silver-tone, lower for gold-tone) and
`web` (higher for a spider web dial; other watches of the brand stay near
zero). The appendix at the end explains how they are computed. With
`filter = false` (shadow mode) they are only shown; with
`filter = true` listings below `match_threshold`, `colour_threshold` or
`web_threshold` are not notified but kept in the database. The negative
reference images filter nothing: `calibrate` and `report` show how close a
photo comes to them, as a diagnostic. `calibrate` scores
each reference image against the others, suggests thresholds that keep every
positive, then scores the stored listings and prints the closest ones, so you
can see what the thresholds would let through. It makes no Browse API call;
photos and embeddings are cached in `data/image_cache`. `report` writes
`data/report.html` with every stored listing, its photos and scores, ranked by
`match`, and opens it in the browser; with `--match`, `--colour` and `--web`
it marks what those thresholds would let through.

**Near-miss digest.** With the filter on, the first cycle after
`digest_hour` (20:00 by default) sends one Telegram message a day with the
listings kept out by the thresholds, closest first, so a threshold that is too
strict cannot silently hide the watch. Each listing appears in one digest
only; `digest_hour = -1` turns it off.

## Scheduling

`run-once` is idempotent and refuses to start while another run is active, so
it is safe to call from cron, a systemd timer or Windows Task Scheduler. Use
absolute paths, since the scheduler's working directory is not the project,
and call the executable in `.venv` rather than `uv run`, which syncs the
environment at every start. `--log-file` keeps the logs in a rotated file
instead of stderr, which a scheduler would discard (Windows) or mail (cron).

Keep the schedule consistent with `poll_interval_minutes`: `run-once` does not
read it, but `check-config` computes the API budget from it. Relative paths
inside `config.toml` (database, image cache, reference images) are resolved
against the directory of `config.toml`. The exit code is 0 on success and 1
when the cycle failed (for example every search failed or the eBay
credentials were rejected). Do not schedule `watch`: it is the alternative to
a scheduler, not a task for one.

**cron** (every 20 minutes):

```cron
*/20 * * * * /path/to/ebay-sniper/.venv/bin/ebay-sniper --config /path/to/ebay-sniper/config.toml --log-file /path/to/ebay-sniper/data/ebay-sniper.log run-once
```

**Windows Task Scheduler** (every hour). Register the task from an elevated
PowerShell; `Register-ScheduledTask -Force` replaces an existing task with the
same name:

```powershell
$root = 'C:\path\to\ebay-sniper'
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\ebay-sniper.exe" `
    -Argument "--config `"$root\config.toml`" --log-file `"$root\data\ebay-sniper.log`" run-once" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 60)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U
Register-ScheduledTask -TaskName ebay-sniper -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force
```

- `S4U` ("run whether user is logged on or not", without storing the
  password) runs the task in the background, with no console window popping
  up, also when nobody is logged on. It cannot use network shares, which the
  bot does not need, and registering it requires an elevated prompt.
- Without `-RepetitionDuration` the trigger repeats indefinitely.
  `-StartWhenAvailable` runs one missed cycle after a shutdown or sleep, but a
  sleeping PC does not poll: disable sleep while plugged in, or move the bot
  to an always-on machine.
- The task is listed in `taskschd.msc` (Task Scheduler Library), with its
  last run time and result.

```powershell
Start-ScheduledTask -TaskName ebay-sniper       # run a cycle now
Get-ScheduledTaskInfo -TaskName ebay-sniper     # LastTaskResult: 0 ok, 1 failed, 267009 running
Get-Content C:\path\to\ebay-sniper\data\ebay-sniper.log -Tail 30 -Wait
# Another interval (update poll_interval_minutes too):
Set-ScheduledTask -TaskName ebay-sniper -Trigger (New-ScheduledTaskTrigger -Once `
    -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30))
Disable-ScheduledTask -TaskName ebay-sniper     # pause; Enable-ScheduledTask resumes
Unregister-ScheduledTask -TaskName ebay-sniper -Confirm:$false
```

A failing task (for example expired eBay keys) is not reported on Telegram
yet: check the log or `LastTaskResult` now and then.

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
- The photo thresholds were calibrated on curated reference images only: no
  listing of the wanted watch was online while the filter was built, so how
  it scores on real eBay photos is unknown. Lots and photos where the watch is
  small are the expected weak spot; the near-miss digest is the safety net.

## Development

```bash
uv run pytest
uv run pytest -m vision      # loads the real image model (needs the vision extra)
uv run ruff check . && uv run ruff format .
```

Tests never touch the network: HTTP is mocked with `respx`. The fixtures in
`tests/fixtures/` are currently synthetic (see the README there).

## Appendix: how the photo scores work

**Two towers.** SigLIP, like CLIP, is a dual-encoder ("two-tower") model: an
image tower (a ViT-B/16) and a text tower (a transformer) map images and
sentences into the same embedding space. The two were trained together on
image-caption pairs so that an image lands close to its description, which
makes an image comparable with a sentence by cosine similarity. Each tower
encodes its input without looking at the other's, so every embedding is
computed once and cached in `data/image_cache`. The text tower needs a
SentencePiece tokenizer loaded through `transformers`, which is why that
package is in the `vision` extra.

**`match`** compares images with images: the highest cosine similarity between
any listing photo and any positive reference. It recognises the watch model,
but not the case colour or the dial: a whole-image embedding weighs the
composition (hand, wrist, background) about as much as the colour, so the
gold-tone variant on a wrist can score higher than the silver references, and
the same case with a plain dial scores close to them.

**`colour` and `web`** compare images with text. Each comes from four prompt
pairs in `vision.py`: `COLOUR_PROMPTS` (silver-tone versus gold-tone) and
`WEB_PROMPTS` (spider web dial versus plain dial). The text tower embeds the
prompts (L2-normalized, like the photos), the axis is the mean of the pair
differences, and the score of a photo is its dot product with the axis:

```text
axis  = mean_i( t_wanted[i] - t_unwanted[i] )
score = dot(photo, axis)
      = mean_i( cos(photo, t_wanted[i]) - cos(photo, t_unwanted[i]) )
```

A positive score means the photo is, on average, closer to the wanted
descriptions than to the unwanted ones. The `colour` of a listing is the
maximum over the three photos closest to the positives; its `web` is the
maximum over all photos, because a close-up of the dial often ranks low on
`match`. Taking the maximum favours recall: one good photo is enough.

**Reading the numbers.**

- The values are a few hundredths: image-text cosine similarities are small
  in SigLIP, and the score is a difference of two of them.
- Zero is not a natural boundary: the prompts carry their own bias (for
  example "gold plated" versus "stainless steel"), so the thresholds come from
  `calibrate` on the reference images, not from the sign.
- The axis is not normalized: its length depends on how similar the two
  prompts of each pair are. Changing, adding or removing a prompt rescales
  every score, so after editing the prompts run `calibrate` again and revisit
  the thresholds.
- Text probes were the only option without labelled listing photos. Once the
  Telegram feedback buttons (M4) have collected labels, a direction learned
  from the photos themselves (difference of class means, or logistic
  regression on the embeddings) should replace them.
