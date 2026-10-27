# Issue: Full text for Indeed Jobs from the Bundesagentur

Status: all parts built; the "Done when" of Parts 1 and 2 checked on a copy of the database with the existing alert mail Jobs (see the plan's Progress). Implement on branch `implement-indeed-full-text`.

## Goal

Jobs from the Indeed alert mail only have a snippet of two or three lines, so their Fit score is a Low-confidence score. Many employers publish the same opening at the Bundesagentur as well. Today that only helps by chance: the BA Posting has to be found by one of the Search terms, and even then it becomes its own Job that waits for the AI's same-Job decision.

This issue adds a targeted lookup: for every new Indeed Job that is still unscored and snippet-only, the Daily run asks the Bundesagentur API for the same company and title near the same place. A clear hit is attached to the Indeed Job as a second Posting, its description is fetched by the existing step, and the Job is scored on the full text in the same run.

The lookup uses only the BA API, which ADR 0002 already allows. It makes no `claude` calls and fetches nothing from Indeed.

## Decisions

- **Only BA, no Indeed pages, no search engine.** Fetching `viewjob` pages is ruled out by ADR 0002 (Indeed blocks automated fetching, and its terms forbid it). Finding a career page by web search would need AI calls and is a follow-up at most.
- **Lookup before scoring, only for unscored Jobs.** The lookup runs in `run_daily` after `collect_mail` and before `fetch_missing_descriptions`, so the hit's description is fetched and `settle_remote_jobs` sees it in the same run. Scored Jobs are not looked up here (see follow-ups).
- **Attach directly, only on a clear hit.** The hit becomes a Posting of the Indeed Job without the AI's same-Job decision. Going through the normal path (`attach()` → `pending`) would not help: the BA Job would be merged *into* the Indeed Job, and the Indeed Job would already have been scored on its snippet in the same call. A clear hit therefore needs a stricter rule than the match candidate rule (`SIMILAR_TITLE = 0.5`):
  - same `normalize_company()` key,
  - `title_similarity(title_tokens(...))` at or above a new, stricter threshold (start at 0.8, tune it on live data),
  - and, when both places are known, at most a small distance apart (started at 15 km, raised to 30 km by the candidate during Part 2). A remote Indeed Job (no place) needs a BA hit with home office.
  - More than one clear hit: take the one with the highest similarity, then the nearest. Hits below the threshold are dropped and counted, never stored as Jobs of their own.
- **A BA Posting already in the database is not attached again.** `postings` is unique on `(source, source_id)`. If the hit is already known, it belongs to another Job, and Job matching has already handled that pair. Count it and move on.
- **Which Indeed Jobs are looked up.** Unscored, snippet-only, Eligibility `eligible` or `unclear`, not a Target company (the Career scan covers those), with a company name. Sponsored results are included as long as they have a company. The plan's idea of skipping Jobs whose snippet already looks like a mismatch is dropped: deciding that would need an AI call, and a BA request costs nothing.
- **Cap per run.** At most a fixed number of lookups per Daily run (a constant in code, start at 50, oldest-first by `first_seen` so no Job starves). Only Jobs first seen within the last 7 days are looked up (added in the review): Jobs that are never scored, such as those with an unknown place, would otherwise fill the cap for ever. Each lookup is one search request plus, on a hit, one detail request through the existing step. The client's delay applies.
- **A country as place means remote** (decided during Part 1). An alert mail Posting whose place is only a country came in through the alert's remote filter, so it is a home office Posting. Migration `011` moves stored Jobs of that kind from "Ort unbekannt" to home office. A remote Job is looked up nationwide (the API has no home office filter) and takes only a hit with home office.
- **A remote alert Job without a full text is kept.** Its snippet rarely says "fully remote", so `settle_remote_jobs` keeps it on the hint "Remote-Filter des Indeed-Alerts" instead of dropping it, and scoring keeps it eligible when the AI answers "unknown". With a full text, the full text decides as for any home office Job.
- **A hit gives a Job with an unknown place its place.** Such a Job (a region or nothing usable as place) is looked up at that place without a radius, takes the hit's place, and its Eligibility is decided again. Without a hit it stays "Ort unbekannt".
- **Uncertainty stays visible.** A Job whose only non-snippet Posting has no description (BA offers on partner portals have none) must still count as snippet-only. See facts below; this is a fix in `scoring.py`.

## Facts to build on

- `sources/indeed_mail.py` builds Postings with `source = 'indeed'`, `is_snippet=True` and the snippet as `description`. Organic results have the `jk` key as source ID, sponsored ones a hash.
- `BundesagenturClient.search()` takes `term`, `location`, `radius_km`, `published_within_days` and yields `Posting`s with `source = 'bundesagentur'`, `refnr` as source ID and `homeoffice_possible`. The Jobsuche API is documented to accept an employer filter (`arbeitgeber`). **Check live first** whether it filters reliably and how it treats legal forms; if it does not, search by title near the place and filter on `normalize_company()` in Python.
- `matching.attach()` inserts a new Job for every unknown Posting, so the lookup needs its own small function in `matching.py` that inserts a Posting with a given `job_id` (same columns as in `attach()`), refreshes `jobs.last_seen`, and passes the URL through `sources.safe_url()` like `store_postings()` does.
- `daily.fetch_missing_descriptions()` already fetches the description of every BA Posting without one whose Job is `eligible` or `unclear`. The attached Posting needs nothing extra.
- `daily.settle_remote_jobs()` reads `p.description` of any Posting of an `unclear` home office Job, so a remote Indeed Job gets its full-remote decision from the BA text.
- `scoring.pick_jobs()` takes the longest description of any Posting of the Job, and `snippet_only` is `NOT EXISTS (... p.is_snippet = 0)`. A BA Posting has `is_snippet = 0` even when its description could not be fetched, so the Indeed snippet would then be scored **without** the Low-confidence flag. Change `snippet_only` to "no Posting with `is_snippet = 0` and a description". Check `evaluation.py` and `market.py` for the same pattern.
- `daily.Counts` goes into `runs.counts_json` and is printed by `cli.py` after the Daily run.
- Migrations go up to `010_sorted_out.sql`. This issue should need none.

## Part 1: Lookup and attach

- `daily.lookup_full_text(conn, client, cfg, counts)`: select the Indeed Jobs as described, search the BA for each, pick a clear hit, attach it. A request error is logged and counted and does not stop the run, like `fetch_missing_descriptions()`.
- Call it in `run_daily` after `collect_mail` and before `fetch_missing_descriptions`, only when a mailbox is configured.
- Mark the attached Posting in `raw_json` (for example `"found_by": "lookup"`) so the eval can tell it from Postings found by Search terms. Do not add rows to `posting_terms`.
- Fix `snippet_only` in `scoring.py` (and the same pattern elsewhere) as described above.
- Tests (synthetic data, a fake client, no network): a clear hit is attached and its description fetched; a lower title similarity, another company or a distant place is not; a BA Posting already in the database is skipped; a remote Indeed Job only takes a home office hit; the cap holds; a Job with a BA Posting without description is still Low confidence.
- **Done when** the tests pass and a live Daily run with new alert mails attached hits, fetched their descriptions, and the following scoring call rated those Jobs without the Low-confidence flag.

## Part 2: Make the hit rate visible

- New `Counts` fields: lookups, hits, already known, dropped below threshold, failures. Print them in German after the Daily run (for example "Volltext gesucht: 12, gefunden: 4").
- `jobsearch eval`: in the stage metrics, the share of Indeed Jobs with a full text, split into "per Lookup" and "anderweitig". Postings from the lookup are recognized by the `raw_json` mark.
- After a few days of Daily runs, write the hit rate (rough proportions only) and the chosen thresholds into the plan's Progress section. If the hit rate is very low, record that and consider dropping the lookup.
- **Done when** the counts show up in the CLI output and `eval`, and the plan has the first numbers.

## Part 3: Rename "geringe Sicherheit" to "geringe Evidenz"

The German label says the model was unsure, but the flag only means the score rests on a snippet. "geringe Evidenz" says that. Only user-facing text changes; the code name `low_confidence`, the column and the English term "Low-confidence score" stay.

- `cli.py` (scoring output), `digest.py` ("geringe Evidenz (keine volle Beschreibung)"), `evaluation.py` (stage metrics row), `market.py` and `templates/market.html` ("davon geringe Evidenz"), `report.py` (label of `stage.low_confidence.*` in the Weekly report input), `templates/job.html` and `templates/jobs.html` (badge).
- `tests/test_dashboard.py` checks the badge text; update it and any other test that asserts the old wording.
- `CONTEXT.md`: add the German name to "Low-confidence score" (`In German output it is "geringe Evidenz".`), like the other terms with a German name.
- Older plan entries that quote the old label stay as they are; they describe what was built then.
- **Done when** a search for "geringe Sicherheit" in `src/` and `tests/` finds nothing and the tests pass.

## Follow-ups (not part of this issue)

- **Already scored Indeed Jobs.** A one-off backfill could look up existing snippet-only Jobs, attach hits, and rescore those Jobs with `jobsearch rescore`. `rescore` currently picks Jobs by an older prompt hash, so it would need a second selection.
- **Career page lookup** for companies that are not Target companies, through Playwright and the existing extraction prompt. Costs AI calls, so only for Jobs already scored ★★★ or ★★.
- **CONTEXT.md**: if the lookup becomes a term used in conversation, add it (for example "Full-text lookup", German "Volltextsuche").
