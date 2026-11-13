# review-intel

A Claude Code command that takes a list of dental practice domains (with city, state and ZIP) and adds:

- **bad_reviews**: the most recent 3 to 5 one- or two-star Google reviews, as email-ready bullets: `• Sarah M. - waited two hours past appointment`
- **competitor_1, competitor_2, competitor_3**: the top 3 local competing practices, names cleaned the way Part 3 asks ("Bright Smiles Family Dentistry, LLC | Best Dentist in Austin TX" becomes "Bright Smiles Family Dentistry")
- **status** and **notes**: what happened to the row, and why any cell is blank

Run `/review-intel` in Claude Code to open a local web page: drop a CSV in, watch each row, download the result. Run `/review-intel path/to/leads.csv` to run it without a browser.

## Setup in 3 steps

1. **Python 3.9+** is the only requirement. There is nothing to install. The code uses the standard library only (`pytest` is needed only to run the tests).
2. **Keys.** `cp .env.example .env`, then fill in:
   - `TREG_API_KEY`: a treg team API key. Every Google data call is paid from it.
   - `ANTHROPIC_API_KEY`: used for the 4-5 word complaint summaries. Optional: without it the command uses the signed-in `claude` CLI, and with neither the bullets stay blank (the row says so).
   - `REVIEW_INTEL_MAX_USD`: the spend cap per run (default 2).
3. **Run.** Open this folder in Claude Code and type `/review-intel` (web page) or `/review-intel sample/leads_sample.csv` (headless). Without Claude Code:

   ```bash
   python3 -m review_intel check                         # keys + treg access, spends nothing
   python3 -m review_intel serve                         # web page on http://127.0.0.1:8765
   python3 -m review_intel run sample/leads_sample.csv   # writes output/leads_sample_enriched.csv
   ```

## Input

A CSV with four columns. The header names are matched loosely (`website`/`url` for domain, `zip_code`/`postal_code` for zip, `st` for state). Extra columns are kept and passed through unchanged.

```csv
domain,city,state,zip
brightsmilesdental.com,Austin,TX,78701
```

Domains are reduced to the root (`https://www.x.com/about` becomes `x.com`). ZIPs that lost a leading zero in Excel (`7102`) are padded back (`07102`). A row with no usable domain, or with neither ZIP nor city, gets `status = invalid_input` and the rest of the file still runs. The web page checks the columns before it starts and offers a template download.

## Output

The input columns, then:

| column | content |
|---|---|
| `bad_reviews` | up to 5 bullets, one per line, newest first: `• First L. - 4-5 word complaint` |
| `competitor_1..3` | cleaned names of the top 3 competitors, in Google's ranking order |
| `status` | `ok`, `partial`, `no_listing`, `error`, `skipped_budget`, `invalid_input` |
| `notes` | why something is missing, in plain words |
| `bad_review_count` | number of bullets |
| `google_listing_name`, `google_rating`, `google_review_count`, `google_maps_url` | the listing we matched, so anyone can check it |
| `listing_match` | how we matched it: `website domain`, `phone on website + same ZIP/city`, or `name on website + same ZIP/city` |

Every run also writes `output/<name>_enriched_summary.json` with status counts, time, spend per tool, cache hits and retries.

## The three edge cases

Nothing is ever invented. A blank cell always comes with a reason in `notes`.

**Fewer than 3 bad reviews.** We return the 1 or 2 that exist, and `status` is `partial`. The note gives the count and how many more 1-2 star reviews are star-only (no text, so there is no complaint to summarise), e.g. `only 2 one/two-star review(s) with a written complaint on Google (2 more are star-only with no text)`. A low-star review whose text has no complaint in it ("Wonderful service" under 1 star) is skipped and the next older one takes its place.

**Google listing not found.** The listing is matched on evidence, in this order:

1. the listing's website has the same root domain (or the domain the practice's site redirects to);
2. a listing in the same ZIP or city whose phone number is printed on the practice's homepage (this finds listings that point at an old or vanity domain);
3. a listing in the same ZIP or city, with no website or the same one, whose name contains every distinctive word of the homepage's own name. Generic titles ("PDC", "Home", "Dental Care") are never used for this.

If none of the three holds, `status` is `no_listing` and the review cell is blank. A wrong practice's reviews in a cold email would be worse than none. Competitors are still filled from the Maps results for that ZIP, measured from the ZIP centre and without the "rated higher" rule (there is no rating to compare against), and the note says so.

**Fewer than 3 real competitors.** A competitor must be a dental practice (labs, schools, hospitals, community clinics, insurers and suppliers are excluded), open, within 5 miles, with 10 or more reviews, rated higher than the prospect, not the prospect itself, not on the prospect's website, not at the same address, and not a second listing of a brand already picked. Names must clean to the Part 3 standard. If fewer than 3 pass, the rest stay blank, `status` is `partial`, and the note lists what was excluded, e.g. `only 1 real competitor(s) rated higher than the prospect, with 10+ reviews within 5 mi (excluded: fewer than 10 reviews 4, the prospect itself 2, not a dental practice 2)`.

## How it works

For each row, 8 rows at a time:

1. **Maps search** for "dentist" around `city, ST ZIP` (up to 60 results, in Google's order). Rows in the same ZIP share one call. The prospect is usually in it. This list is also the competitor pool.
2. If the prospect is not in it, the practice's homepage is read (free) for its name, phone numbers and redirect target. Then Maps is searched for the domain, then for the homepage name.
3. **Reviews**, newest first, up to 100. If that page holds fewer than 8 one/two-star reviews with text and the practice has more reviews than we read, one more page sorted lowest-first is added and everything is re-sorted by date.
4. **One LLM call per practice** summarises up to 8 recent complaints in 4-5 words each. The model is told to use only what the review says. Answers of the wrong length are asked again (twice at most). Reviewer names are formatted locally as `First L.`.
5. **Competitors** are taken from the pool in Google's order, through the rules above. If fewer than 3 pass, Google Local Finder results top up the pool.

Speed and cost controls:

- **Retries:** 408/425/429/5xx and network errors are retried 4 times with exponential backoff and jitter, honouring `Retry-After`. Each job has a fallback provider. A row that still fails is marked `error` and the rest of the file continues.
- **Cache:** every successful paid answer is cached on disk (`.cache/`), including summaries keyed by review text. A rerun costs $0 and only retries what failed. Failures are never cached.
- **Spend cap:** each paid call checks `REVIEW_INTEL_MAX_USD` first. treg's real per-call charge (`X-Treg-Cost-Micro`) and the LLM token cost are counted. At the cap the remaining rows get `skipped_budget`, so a bad file cannot run up a bill. Spend is shown live on the page and in the run summary.
- **Auth errors** (bad key, empty balance) stop the run at once instead of failing every row.

## Tool choices

All data calls go to the treg REST API: `POST/GET https://treg.to/call/<tool-id>` with the header `X-Treg-Token: $TREG_API_KEY`. The body is the provider's own JSON, and `X-Treg-Cost-Micro` is the charge. `Authorization: Bearer` is not accepted on `/call` (it returns 401).

| job | primary | fallback | price per call | why |
|---|---|---|---|---|
| Maps search | `crawl4ai.google.serp.maps` | `litescrape.google.serp.maps` | $0.00023 / $0.00015 | crawl4ai worked on 99% of calls in treg's measured stats, with rating, review count, website, phone and coordinates in one row. litescrape is cheaper but worked on 87%. |
| Reviews, newest first | `crawl4ai.google.maps.reviews` | `litescrape.google.maps.reviews` | $0.00015 | 99% vs 90% success at the same price. Up to 100 reviews per call. |
| Reviews, lowest first | `litescrape.google.maps.reviews` (`sort_by=ratingLow`) | none | $0.00015 | Only litescrape can sort by lowest rating. Used only when the newest page is short of complaints. |
| Competitor top-up | `litescrape.google.serp.local` (Local Finder) | none | $0.00015 | Used only when the Maps pool gives fewer than 3. |
| Summaries | Anthropic Messages API, `claude-haiku-5-5` | `claude` CLI | about $0.0005 per practice | treg has no general LLM endpoint. Its only Claude tool is a DataForSEO wrapper at $0.0245+ per call, about 50 times the price. |

The litescrape endpoints were the source of every 502/503 in our earlier list build. That is why they sit behind crawl4ai and not in front.

## Cost and speed (measured)

Test runs on 13 Nov 2026, cold cache, 8 workers:

| run | rows | time | treg | LLM | total |
|---|---|---|---|---|---|
| 10 practices + 2 edge cases | 12 | 37 s | $0.0048 | $0.0060 | $0.0108 |
| 60 finals from our lead list | 60 | 106 s | $0.0228 | $0.0327 | $0.0555 |
| same 60, rerun from cache | 60 | under 1 s | $0 | $0 | $0 |

**About $0.09 per 100 leads**, and about 3 minutes per 100 leads. treg is about $0.04 of that and the LLM about $0.05. The LLM figures come from the `claude` CLI fallback, priced at the list price Claude Code reports for Haiku; the direct API uses the same per-token price. On the CLI path the cost per call varies more, because the model sometimes thinks at length before answering.

## Tests

```bash
python3 -m pytest -q
```

The tests cover name cleaning, reviewer-name formatting, domain parsing, listing matching (domain, phone, name, generic-name refusal), review selection, every competitor rule, CSV parsing, the spend cap, and the three edge cases end to end against a fake treg. They need no network and spend nothing.

## Files

```
.claude/commands/review-intel.md   the Claude Code command
review_intel/
  __main__.py     CLI: run | serve | check
  pipeline.py     per-row flow, CSV in/out, job runner
  providers.py    treg tool ids, fallbacks, response normalising
  logic.py        listing match, review selection, competitor rules (pure, tested)
  names.py        Part 3 name cleaner + validator, reviewer names
  summarize.py    Anthropic API / claude CLI summaries
  http.py         treg client, retries, cache, spend meter
  website.py      root domains, homepage name/phones
  server.py       local web app (127.0.0.1 only)
web/index.html    the page
docs/screenshots/ the page at 1440px and 390px
sample/           leads_sample.csv, the 12-row test, the 60-row test
tests/            pytest
```

Keys are read from the environment or `.env` only. `.env`, `.cache/` and `output/` are git-ignored.
