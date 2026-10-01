# Test fixtures

HTTP response bodies used with `respx`.

The files in this directory are **synthetic**: they were written by hand from
the Browse API OpenAPI contract (v1.20.4) because no eBay credentials were
available when they were created. Field names and shapes follow the contract,
but the values are invented (item ids, titles, image URLs). Replace or extend
them with sanitized live responses as soon as possible:

```bash
uv run ebay-sniper search "futura (spider, ragno)" -m EBAY_IT \
    --save-json tests/fixtures/live_search_ebay_it.json
```

`--save-json` removes the `seller` container, the affiliate URL and the item
address except the country. Before committing, still check the file by eye:
no tokens, keys, chat ids or user names may end up in a fixture.

| File | Content |
| --- | --- |
| `token.json` | OAuth client-credentials response |
| `search_ebay_it.json` | Search page: an auction, a converted-currency Best Offer listing with calculated shipping, a "for parts" listing with two shipping options |
| `search_ebay_de.json` | Search page overlapping `search_ebay_it.json` (same legacy id) plus one new listing |
| `search_empty.json` | Search with no results (`itemSummaries` is absent) |
| `item_110000000001.json` | `getItem` response for the auction in `search_ebay_it.json` |
| `error_rate_limit.json` | Error body returned with HTTP 429 |
