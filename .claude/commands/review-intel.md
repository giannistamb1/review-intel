---
description: Bad Google reviews + top 3 local competitors for a CSV of dental practice domains (opens a local web app, or runs headless on a CSV path)
argument-hint: "[path/to/leads.csv]  (leave empty to open the web app)"
allowed-tools: Bash(python3 -m review_intel:*), Bash(python3 --version), Bash(ls:*), Bash(test:*), Read
---

# /review-intel

Enrich a list of dental practices (columns `domain, city, state, zip`) with:

- `bad_reviews`: the most recent 3-5 one- or two-star Google reviews, one bullet per line, `• First L. - 4-5 word complaint`
- `competitor_1..3`: the top 3 local competing practices, names cleaned like Part 3
- `status` + `notes`: what happened to the row and why any cell is blank

Arguments: `$ARGUMENTS`

## Steps

All commands run from the repository root, i.e. the folder that contains this `.claude/` folder and the
`review_intel/` package. Use only the commands below. Do not change any files in the repo.

1. Check the setup without spending anything:

   ```bash
   python3 -m review_intel check
   ```

   - If `TREG_API_KEY` is MISSING, stop and tell the user: copy `.env.example` to `.env`, paste a treg
     team API key (from the team dashboard at https://treg.to), then rerun `/review-intel`.
     Never ask the user to paste a key into the chat, and never write a key into any file yourself.
   - If summaries show `NONE`, warn that review bullets will be blank, and continue.
   - Python 3.9 or newer is the only dependency (`python3 --version`).

2. If `$ARGUMENTS` is empty, start the web app (in the background, so the session stays usable):

   ```bash
   python3 -m review_intel serve
   ```

   It prints a `http://127.0.0.1:<port>/` URL and opens it in the browser. Tell the user the URL and
   that they can drop a CSV there, watch every row progress, and download the enriched CSV.
   Results are also saved to `output/<file>_enriched.csv`. Stop the server when the user is done.

3. If `$ARGUMENTS` is a CSV path, run headless:

   ```bash
   python3 -m review_intel run "$ARGUMENTS"
   ```

   The run writes `output/<name>_enriched.csv` and `output/<name>_enriched_summary.json`.
   Exit code 2 means a bad input file: show the error line (missing column, empty file) and stop.

4. Report back in a short table: rows by status (`ok`, `partial`, `no_listing`, `error`,
   `skipped_budget`, `invalid_input`), spend against the cap, time, and the output path. For every
   row that is not `ok`, quote its `notes` cell. Do not fill blanks yourself and do not guess
   reviews or competitors: blanks with a reason are the intended output.

5. If rows show `error` (provider outage after retries) or `skipped_budget`, say so and offer to rerun
   the same command. Reruns hit the local cache, so finished rows cost nothing and only the failed
   rows call the APIs again. A bigger cap is `REVIEW_INTEL_MAX_USD=5 python3 -m review_intel run ...`.

## What the statuses mean

| status | meaning |
|---|---|
| `ok` | listing found, 3+ bad-review bullets, 3 competitors |
| `partial` | listing found, but fewer than 3 bad reviews with text exist, or fewer than 3 competitors pass the rules (notes say which and why) |
| `no_listing` | no Google listing matches the domain (website, homepage phone, or homepage name in the same ZIP/city). Reviews blank; competitors come from the ZIP and are flagged |
| `error` | a provider kept failing after retries; rerun |
| `skipped_budget` | the per-run spend cap was reached before this row |
| `invalid_input` | the row has no usable domain, or neither ZIP nor city |
