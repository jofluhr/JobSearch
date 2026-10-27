# Implementation plan

How JobSearch gets built. The domain language is in [CONTEXT.md](../CONTEXT.md) and the main decisions are in [docs/adr](adr/). This plan is ordered so that every phase ends with something that runs and is useful on its own.

## Progress

- **Phase 1 done** (2026-10-15)
- **Phase 2 done** (2026-10-15). See "Phase 2 decisions".
- **Phase 3 done** (2026-10-15). The live daily run scored 40 Jobs in one call. The user reviewed the first Digest and found the scores and reasoning sound. There was no formal tally against their own ratings yet. See "Phase 3 decisions".
- **Phase 4 done** (2026-10-15). Fixture tests cover the four cases, and a live scoring run decided real candidate pairs correctly. See "Phase 4 decisions".
- **Phase 5 done** (2026-10-18). A live run parsed six real alert mails, stored their Jobs and moved the mails. See "Phase 5 decisions".
- **Phase 6 done** (2026-10-19). Tests cover once per day, the lock, the network retry and the 4-day catch-up, and a headless `pythonw -m jobsearch run` on a day with a successful run did nothing twice, as it should. On the next day `install-task` registered the task, and a run started by Task Scheduler collected, scored with `claude -p` (so the Claude login is visible there) and wrote the Digest; a second start the same day did nothing. See "Phase 6 decisions".
- **Phase 7 built** (2026-10-18). Tests go through the Flask test client, including the "Done when": a status with a note set in the dashboard appears in the next scoring prompt. On a copy of the live database every page rendered in well under a second. Waiting for the user's first look in the browser. See "Phase 7 decisions".
- **Phase 8 built** (2026-10-18). Setup, scan, Personio feed, browser fallback and the two-scans report run live. A repeat scan on the same day sent one page out of more than twenty to the AI. Coverage depends on the setup's URLs: after the setup and one recheck, about three quarters of the Target companies had a URL, and about half of those delivered Postings. Roughly half of the pages needed the browser. The rest needs URLs checked by hand, see "Phase 8 decisions".
- **Open for the user (Phase 8):** check the URLs in `private/companies.csv`. Replace landing pages and broken links with the page that actually lists the jobs; `jobsearch status` and the Digest warnings name the pages that fail or list nothing. Phase 8 counts as done once most Target companies deliver Postings.
- **Phase 9 done** (2026-10-18). A live daily run derived a full set of terms from the Profile in one call and searched them together with the manual ones; a second run made no call. An edited copy of the Profile (the real file stays untouched) gave a different set in one call, and existing Fit scores stayed as they were. See "Phase 9 decisions".
- **All phases built.** Still open: the Phase 8 URL check above.
- **Eval Part 1 done** (2026-10-20, `docs/issues/eval.md`). Each Daily run stores a settings snapshot and each Fit score its prompt hash. `eval --changes` on the live database showed the start of recording after one collect-only run. Fit scores from before have no prompt hash; `git log` of `scoring.md` dates them.
- **Eval Part 2 done** (2026-10-20). `jobsearch eval` shows metrics per stage and per Search term, for a period or split at a date. First findings on the live database: most ineligible Jobs come from the nationwide query and are dropped as not fully remote; derived terms that differ only in word order find almost the same Jobs, so their "nur hier" count is near zero; the share of Low-confidence scores rose once alert mails and career pages came in, probably because of their shorter texts (not checked yet).
- **Eval Part 3 done** (2026-10-20). `jobsearch eval --scores` puts each tier against the Job status, with precision of ★★★, positives hidden in ★ and the labeled share per tier, grouped by model, Profile, prompt version or month. On the live database only a small share of scored Jobs has a label yet, almost all of them negative and in ★, so the numbers say little so far; the report says so itself.
- **Eval Part 4 done** (2026-10-20). `jobsearch eval --funnel` traces each entry of `private/found_elsewhere.csv` by URL or by company and similar title, and names the stage where it got lost together with the lever. A not-collected entry tells a company never seen from one whose other openings were collected, and finds from before the first Daily run are kept apart. Checked live with a temporary file of real and made-up entries; the user's own file starts empty.
- **Guardrails Part 1 done** (2026-10-20, `docs/issues/claude-code-guardrails.md`). A `PreToolUse` hook in `.claude/settings.json` runs `jobsearch.guard profile` and blocks file writes in the Profile's folder and shell commands naming it. Live check in a fresh session with a temporary `PROFILE_PATH` in the environment: an `Edit` on the test Profile and a PowerShell `Set-Content` through `$env:TEMP` were both blocked with the German reason, and a new file in the repo went through. The real Profile was not involved. Findings while building:
  - The hook uses exec form (`command` = the venv's `python.exe`, `args` = the module), so no shell is involved and the same entry works whether Claude Code runs hooks through Git Bash or PowerShell.
  - A `PROFILE_PATH` already in the environment wins over `.env` (`load_dotenv` does not override). The hook inherits Claude Code's environment, so for the live check start a session with a temporary `PROFILE_PATH` set in the environment.
  - Fail closed works only once Python runs. If `.venv` is missing, the docs suggest Claude Code treats the failed start as a non-blocking error and lets the write through (not tried).
  - A crash on a shell command reports with exit 1 instead of blocking, so a malformed `.env` value does not lock out every command. The Bash check misses aliases such as Git Bash's `/tmp` mount.
  - First live check: Claude, asked to edit a test Profile written by PowerShell 5.1's `Out-File` (UTF-16), used a shell command instead of `Edit` and reached the folder through `$env:TEMP`, so the string check missed it. The check now expands `$X`, `${X}`, `%X%`, `$env:X` and `~` before comparing. Commands that `cd` into the folder and then use relative paths still get through.
  - Windows writes stderr in the console code page; the module switches stdin and stderr to UTF-8 so umlauts in the reason come through.
- **Guardrails Part 2 done** (2026-10-20). Tracked `.githooks/pre-commit` and `commit-msg` run `jobsearch.guard staged` and `message`; `init` sets `core.hooksPath`. Live check in a throwaway repo with a synthetic value in a temporary `private/` (via `JOBSEARCH_HOME`): a file and a message with the value were refused, the same commit without it went through, `--no-verify` passed, and a missing venv refused the commit. Findings:
  - A dry run on this branch with the real private files refused the commit: a common technical word that is also a manual Search term appears in the hook scripts and in code. Whole-word matching cannot tell the two meanings apart; such words go into `private/allowed_terms.txt`.
  - The hook scripts must stay LF, or `sh` fails on the shebang; `.gitattributes` pins `.githooks/*` to `eol=lf`.
  - The scan uses `--no-renames`, so a renamed file counts as fully added and is scanned again. Safer, but a moved file with an accepted term needs the allowlist.
  - The `PROFILE_PATH` value is matched with both slash directions and in Git Bash form (`/c/...`), and its folder too.
  - From the review: `git commit -m` keeps lines starting with `#`, so the message check scans them too (only the scissors line of `git commit -v` ends the scan). Git runs in the hook's own working directory, not in `JOBSEARCH_HOME`, so a worktree checks its own index. File names come from `git diff --name-only`, which also lists empty and binary files. Comment lines in `private.example/` no longer count as generic values. `companies.csv` is read as `utf-8-sig`.
  - The same BOM problem hit the Career scan, the career setup and `found_elsewhere.csv`: a CSV saved from a spreadsheet lost its first column name. All three now read past a BOM, and the setup writes `companies.csv` back with the BOM it found.
- **Career grounding done** (2026-10-20, `docs/issues/career-grounding.md`). `career.grounded()` drops every extracted item whose title (whitespace collapsed, casefolded) or link is not in the page text the AI saw; a link must appear whole, not as the prefix of a longer one. Test fixtures now link the job the fake AI returns. Part 2 makes drops visible: `ScanCounts.ungrounded` (kept in `runs.counts_json`), one INFO log line per drop with company, reason, title and link, and "verworfen (nicht im Seitentext): n" in the career summary. A page whose items are all dropped counts as empty for the two-scans warning.
  - Live check (2026-10-20) on a copy of the database with all page hashes cleared, so every career page went to the extraction model again: on no page was an item dropped. The model invented nothing in this run, and the normalization caused no false drop. The check stays useful as a guard against a page that tries to inject Postings; a drop now shows up in the summary and the log.
  - From the review: `ungrounded_reason()` looks for the title only in the visible text without the `<links>`, where any URL slug would match, and accepts a link only when it is closed (`>` or whitespace after it), so a link that `MAX_PAGE_CHARS` cut off is dropped. Follow-ups if drops show up: a link containing a space lets a prefix up to the space through; soft hyphens, dash variants and typographic quotes in titles are not normalized and could cause false drops.
- **Prompt injection Part 1 done** (2026-10-21, `docs/issues/prompt-injection.md`). `jobsearch check-injection` sends the real scoring and extraction prompts, built from synthetic fixtures in `tests/fixtures/injection/` (a made-up Profile in an unrelated field, four Jobs with direct, hidden and merge injections, a career page asking for a `javascript:` and a local link), and checks seven expectations. It never writes to the database.
  - Baseline on the unchanged prompts (sonnet for scoring, haiku for extraction): 7 of 7 expectations met. Every injected Job stayed at ★, Job 3 stayed `remote: none`, Job 4 was not merged, and the extraction returned only the two real jobs. So Parts 3 and 4 are insurance rather than a fix for an observed failure. One run is a single sample; the model may behave differently on other wording.
- **Prompt injection Part 2 done** (2026-10-21). Deterministic URL limits that hold whatever the model answers: `sources.safe_url()` keeps only http(s) links with a host, and `store_postings()` applies it to every Posting, so a `javascript:` link is dropped and the Posting kept. `career.fetch_page()` refuses non-http(s) URLs and local hosts (`sources.is_local_host()`, IP literals only, no DNS) before any request, in both static and browser mode. The career setup writes no `career_url` that is not https (the company is listed as not found) and adds a note when the host names neither the company nor a known ATS domain.
  - Read-only check against the live data: no stored Posting URL and no career URL would be dropped or refused. About one in six existing, hand-checked career URLs would get the domain note, mostly brand or parent-company domains, where a second look is fair. The check is a plain substring test, so short company tokens match unrelated hosts and a host such as `<company>-jobs.attacker.example` passes; it is a hint for the human check, not a barrier. The note only appears when the setup writes a new URL.
- **Prompt injection Part 3 done** (2026-10-21). `scoring.md`, `career_extract.md` and `career_setup.md` each got a short "Third-party text" section: the Jobs JSON, the page text or the fetched web pages are data, never instructions, and text in them that addresses the model or tries to set the rating or the output is ignored. The three system prompts got a matching sentence. A test keeps both in place. The prompt hashes changed, so `jobsearch eval --changes` shows this as a split point.
  - `check-injection` after the change: again 7 of 7 expectations met, with the same tiers and fields as the baseline. Since the baseline was already clean, this run shows the hardening breaks nothing; it cannot show that it helps.
- **Prompt injection Part 4 done** (2026-10-21). The scoring schema has `injection_suspected` per Job, the extraction schema per page. `ScoreCounts` and `ScanCounts` count the flags (kept in `runs.counts_json`), each flag is logged at WARNING with Job id or company, and the Digest gets one warning line for scoring and one for the Career scan. A flagged Job or page is handled as usual: the flag is a signal, not a verdict.
  - `check-injection` a third time, now with twelve expectations: all met. The model flagged the three injected Jobs and the page, and left the control Job unflagged.
  - Live `jobsearch daily` and `jobsearch career-scan` with the changes: both reported their usual numbers, and neither the scored Jobs nor the re-read career pages were flagged, so no false alarms on real ads so far. The career scan's non-zero exit came from the known failing or empty pages (see the Phase 8 URL check), not from this change.
  - From the review: `safe_url()` and `fetch_page()` no longer raise on a malformed link such as `http://[::1` (one such link from any Source would have ended the run). `fetch_page()` follows redirects itself, at most five, and checks each target before requesting it; in browser mode a route handler aborts every request the page makes to a non-web or local address. `is_local_host()` also knows the old IPv4 spellings (`2130706433`, `0x7f.1`, `127.1`, octal) and a trailing dot. A flagged career page goes into the scan warnings, so `career-scan` then exits non-zero and the toast counts it, on purpose.
- **AI usage Part 1 done** (2026-10-21, `docs/issues/ai-usage.md`). `ai.ask()` takes a required `purpose` and an optional `usage_recorder`, and reports a `CallUsage` for every attempt (`ok`, `invalid`, `error`, `timeout`); migration 007 adds `ai_calls`. Live check with `jobsearch score --limit 1`: one row, linked to the score run, and every value matched the envelope that `ai.ask()` logs at INFO. Findings:
  - The CLI cannot bind the run id as the issue first planned, since `run_scoring` and `run_career_scan` start their runs themselves. They build the recorder; `career-setup` and `check-injection` get it from the CLI without a run.
  - In a one-Job scoring call almost all input tokens were a cache write: the prompt, Profile and schema dominate a small call, which supports batching.
  - The package is installed editable, so any `jobsearch` process (dashboard, scheduled task) runs the checked-out branch: the live database had migration 007 before the live check started.
- **AI usage Part 2 done** (2026-10-21). `status` shows the last 7 days, the Digest the day, and `jobsearch eval --usage` a table per day, purpose and model with the API equivalent per scored Job, per ★★★/★★ Job and per career page, the retry share and the cache tokens per call. The fields are named once in the new module `usage.py` (`CallUsage`, purposes with German labels, where each field is in the envelope, what reports show); `ai.py`, `db.py` and the eval import it, so `db.py` no longer depends on `ai.py`. A test keeps the `ai_calls` columns in line with `CallUsage`. Findings:
  - The live check caught what the tests missed: the start of recording was compared by day, so the morning's Fit scores and Career scan counted as free and the API equivalent per scored Job came out far too low. Scores and scans now count from the exact time of the first recorded call (a scan by its end, since its run starts before its AI call). The costs themselves are filtered by day, as every eval period is.
  - Live `jobsearch daily` (the issue's "Done when"): the scoring call got its row with the score run's id, every value matched the logged envelope, and the Digest showed the "KI-Verbrauch heute" line.
  - Neither recorded call read anything from the cache; both wrote the whole prompt to it. The cache lasts at most an hour, so with one run a day every call pays the full write. Batching still pays off within a call, but nothing carries over between runs.
  - From the review: usage falls back to the envelope's call-wide `usage` and `total_cost_usd` when `modelUsage` is missing; a row with only one of the two cache counts no longer breaks the overhead; a call answered by several models is stored under their joined names and shows as its own row per model (not seen so far).
- **Weekly report Part 1 done** (2026-10-21, `docs/issues/weekly-report.md`). `report.report_input(conn, today, found_path, last_report_at)` builds the AI's input from the eval functions: stage metrics, metrics per Search term, Fit score against Job status and found-elsewhere stages, each for the last 7 days and the whole history, as `Metric(id, value, n, label)`, plus the settings changes since the last report. Values are counts (the precision of ★★★ a whole percent), `n` is the base. Until the `reports` table exists (Part 2) the date of the last report is a parameter. Findings from a read-only run on the live database:
  - A few hundred metrics, most of them per Search term (14 each). Metrics with `n = 0` are left out, except a term's Job count, since a term that finds nothing is a finding. Together with compact JSON this cut the input by about a third, to a size that is fine for one call a week.
  - All IDs were unique; two terms that slug alike get `-2`.
  - The funnel never sends the titles or companies of the found-elsewhere entries, and no metric carries text from job ads; a test checks this.
- **Weekly report Part 2 done** (2026-10-21). Prompt `weekly_report.md`, schema built from `LEVERS`, `report.check_answer()` (unknown metric IDs, evidence below `MIN_EVIDENCE_N`, numbers not backed by the item's own evidence, targets that do not fit their Lever, more than five Recommendations or two Lever ideas), migration 008 (`reports`, `recommendations`), `data/reports/YYYY-Www.md` and `jobsearch report [--dry-run]`. Findings from two live reports on the real database:
  - The first one dropped most Recommendations and the summary. Two causes, both in the design, not in the model: the input carried metrics of Search terms that are no longer active (old derived sets, switched-off terms), and the model was told to switch two of them off, which the target check rightly refused; and the number rule caught tier names ("Stufe 3"), which the metric labels themselves had modelled ("1 Sterne").
  - Fixes: metrics per Search term only for active terms; labels and prompt name tiers ★, ★★, ★★★; tier names and "7 Tage" are not counted as numbers.
  - The second report dropped nothing: a summary and five Recommendations, every one backed by metrics with a solid `n`, and their texts without numbers. With the database younger than a week, the 7-day and whole-history metrics were still the same, which the summary said itself.
- **Weekly report Part 3 done** (2026-10-21). `REPORT_INTERVAL_DAYS` (default 7) and `report.report_due()`: due when the newest finished report looked at a week that long ago; failed or interrupted reports do not count. `cmd_daily` makes the report after scoring and only when it scores, prints a failure and leaves the run's outcome alone, and shows a toast that opens the Markdown file. The dashboard has a page "Wochenbericht" with the newest report, the evidence tables and a list of all reports; the header links to it. The settings snapshot carries `prompt_weekly_report`. Live check on the real database (read-only, through the Flask test client): the page rendered the report from Part 2's live run; two reports of the same week looked alike in the list, so each entry now also shows when it was made.
- **Weekly report Part 4 built** (2026-10-21). `report.decide()` stores the candidate's decision; accepting `search_term_off`, `search_term_new` or `exclusion` appends one line to the private file, under the run lock, and refuses a target that would break the file. The report page has "übernehmen" / "erledigt (von Hand)" / "aufnehmen" and "ablehnen" / "verwerfen", with the dashboard token; decisions are final. The next input carries the Recommendations of the last four reports and, for accepted ones 7 to 56 days old, their evidence metrics over equal windows before and after the decision. Read-only live check: the dry run carried the open Recommendations of today's reports as history, and the page showed the buttons. The issue's "Done when" (an accepted Recommendation in `eval --changes`) needs the candidate's own decision and the next Daily run.
  - The slugs of the before/after metrics follow the current input's mapping; a switched-off term gets the plain slug of its name. Two terms that slug alike would only mix up there if one of them was switched off, which is unlikely enough to leave.
- **Weekly report review** (2026-10-21). From the two-axis review of the branch: the before/after windows now have the same length (the after window includes today, so the before window got one day more); any error in the report, not only an AI error, leaves the Daily run's outcome alone; migration 008 has its own test; `report.py` collects the four metric builders in one helper, builds the input once for the report and `--dry-run`, and owns the report list, the ISO week and the applied file, so the dashboard runs no SQL of its own for them; both dashboard forms check the token the same way.
- **Open for the user (Weekly report):** accept a Recommendation that changes a file (a Search term or an exclusion word) on the dashboard page "Wochenbericht"; the next Daily run should then show the change in `jobsearch eval --changes`. That is the issue's last "Done when" check, still open at the merge.
- **Market report Part 1 done** (2026-10-21, `docs/issues/market-report.md`). `market.active_companies()` counts eligible, non-generic Jobs per `normalize_company` key over the last 90 days (`first_seen`, today included) and lists a company with 3 or more of them or a Reopening, with its most frequent spelling, best tier of the newest Fit scores, Reopenings and the note "Kandidat für companies.csv" when no Job of it is from a Target company. `jobsearch market` prints the table. Live: a little over a dozen active companies, about half of them not on the Target company list, and no Reopening in the window yet.
- **Market report Part 2 done** (2026-10-21). Migration `009` adds `skill_groups`. `market.group_skills()` collects the distinct skill texts (whitespace collapsed) of the newest Fit score of every ★★/★★★ Job that have no group, and sends at most 400 per call, at most 3 calls per run, to `MODEL_EXTRACTION` with the existing group names (prompt `skill_groups.md`, with a "Third-party text" section). The answer refers to indexes; the code requires every index once and names of 1 to 60 characters, and a group name that differs from an existing one only in case takes the stored spelling. A failed call leaves the texts ungrouped, the report is printed anyway and `jobsearch market` exits with 1. `--regroup` deletes all groups first.
  - Live: all skill texts of the good Jobs (a few hundred) fit into one Haiku call of about 40 seconds and an API equivalent of about a cent. A second run made no call. The result has roughly two groups for every three texts, most groups hold a single text: variants of the same skill ended up together, and most single-text groups are skills that only one ad named. With few Jobs per field this is expected; whether the counts say something will show in Part 3.
- **Market report Part 3 done** (2026-10-21). `market.skill_table()` counts per Skill group the ★★/★★★ Jobs (newest Fit score, first seen in the period) that miss or show it, each once per Job and side, for the last 90 days within the period and for the whole period. "davon positiv" and "davon geringe Sicherheit" count the Jobs that name the group on either side. `jobsearch market [--since] [--until]` prints the top 20 by "fehlt" and "ohne Gruppe: n" (all ungrouped texts, not only the period's).
  - Live: all scored Jobs are younger than 90 days, so both time columns are the same for now. The top missing groups each rest on a handful of Jobs; one group the Profile shows sits on the "vorhanden" side for about half of the good Jobs, and a few groups are on both sides.
- **Market report Part 4 done** (2026-10-21). Dashboard page "Markt" (`/markt`, linked in the header) with the Active companies and the full skill table, computed live without AI, and a note on how many texts are still ungrouped. `/markt/gruppe?name=...` lists a group's original texts with how many good Jobs name each as missing or present; the name goes in the query string since group names can contain a slash. `CONTEXT.md` has Market report, Skill group and Active company. On the live database both pages render in well under a second.
- **Market report review** (2026-10-21). From the two-axis review of the branch:
  - `--regroup` no longer deletes first. It groups all texts from scratch and replaces the old groups in one transaction only when every text got a new group; a failed call or more texts than fit into one run (3 × 400) keep the old groups, and the console says so. So above 1,200 texts `--regroup` cannot finish; raise the limits for that run if it ever comes to that.
  - "davon positiv" and "davon geringe Sicherheit" now count only the Jobs that miss the group (the candidate's decision) and stand next to "fehlt". "ohne Gruppe" says it counts texts over all periods. `--since` after `--until` is refused before any AI call.
  - `group_skills()` looks up `ai.ask` per call, so a patched `ai.ask` in tests is seen. `evaluation.table()` is public and shared with `market`, which also reuses `evaluation.STARS` and one query for the ★★/★★★ Jobs. Test examples use a made-up tool name instead of a real product.
  - `check-injection` was not run: the issue keeps the skill grouping out of it, since the worst outcome is a wrong group and the code checks the mapping. `test_injection.py` checks that `skill_groups.md` and its system prompt carry the "Third-party text" rule.
- **Market report done** (2026-10-21). The candidate found the top Skill groups and the Active companies plausible, the last "Done when" point.
- **Sorted out Part 1 done** (2026-10-21, `docs/issues/sorted-out.md`). The scoring schema has `sorted_out` per Job, and `check_answer()` refuses it with a tier other than 1. Migration `010` adds `fit_scores.sorted_out` (existing rows 0), `ScoreCounts.sorted_out` counts it (kept in `runs.counts_json`), and `scoring.md` describes the strict criterion. The Digest leaves Sorted out Jobs out of the ★ list and adds "Von der KI aussortiert: n"; `score` and `check-injection` print the flag.
  - `check-injection` after the prompt change (run twice by accident, both alike): 12 of 12 expectations met. All four Jobs, the control Job included, were sorted out: the synthetic Profile is in another field than all four ads, so that is the expected judgment. The check therefore cannot tell whether an injection could keep a Job from being sorted out; it only shows that the flag does not change the tiers or the injection flags.
- **Sorted out Part 2 done** (2026-10-21). The dashboard list hides Jobs whose newest Fit score is Sorted out while their Job status is new; the tier filter "aussortiert" shows exactly those, every other value keeps hiding them. A shown Sorted out Job (status set) has the badge "aussortiert", the detail page "Von der KI aussortiert" next to the score. `market.active_companies()` leaves Sorted out Jobs out, whatever their status. Live check on a copy of the database with five ★ Jobs marked by hand (the live data has none yet): the list lost exactly those five, the filter showed them, the detail note and the page "Markt" rendered, all well under a second.
- **Sorted out Part 3 done** (2026-10-21). `evaluation.NEWEST_SCORE` joins each Job's newest Fit score; stage and term metrics, the funnel, `score_tables()` without `--by` and the tier counts in `status` use it, so a rescored Job counts once. `score_tables()` with `--by` keeps counting every row in its group. `jobsearch eval` has "davon aussortiert" under ★ (a share of ★), the Weekly report the metric `stage.sorted_out.<key>` (a share of the scored Jobs, also named in `weekly_report.md`), and `eval --funnel` the stage "aussortiert" with the lever of ★. Read-only live check: every Job still has one Fit score, so `eval`, `eval --scores --by prompt` and `status` show the same numbers as before, with "davon aussortiert 0".
  - Open for Part 4: `eval --usage` divides the cost of `scoring` calls by all Fit scores of the period, so rescored rows (paid by `rescoring` calls) will make the API equivalent per scored Job look lower.
- **Sorted out Part 4 done** (2026-10-21). `jobsearch rescore` (`scoring.run_rescoring()`) rates all ★ Jobs with status new, not ineligible, whose newest Fit score has another or no prompt hash, newest first, in batches of `MAX_SCORED_PER_RUN` with `purpose="rescoring"` ("Neubewertung"), under the run lock. It adds a Fit score per Job (shared `insert_score()`), sets but never clears `is_generic`, merges nothing and leaves eligibility and `remote_type` alone. A failing batch stops the call; the console names the error and what is left. The Digest leaves out Jobs that had a Fit score before that day. `eval --usage` counts only a Job's first Fit score as scored, so rescored rows do not lower the API equivalent per scored Job.
  - No `runs` row: `runs.kind` has a `CHECK` with three values, and a rebuild was not worth it. The calls are in `ai_calls` without a run id, like `check-injection`.
  - Live: one call worked through all ★ Jobs with status new in a few batches, each on the first attempt, at an API equivalent under two dollars. About two thirds of them were sorted out, a handful moved up to ★★, none to ★★★. A second call made no AI call. `eval` shows "davon aussortiert" at about three fifths of ★, and `status` and `eval` still count each Job once. A read-only `collect_day()` for today held no Job with a Fit score from before today.
- **Sorted out review** (2026-10-21). From the two-axis review of the branch: the newest Fit score per Job is defined once, `db.NEWEST_SCORE_ID` (`max(id)`); the dashboard, the Digest, the evaluation, `status`, rescoring and the Market report (which had ordered by `scored_at`) build their joins from it. The tier filter takes its tier values from `dashboard.TIERS`; `cmd_rescore` looks up `ai.ask` per call. `CONTEXT.md` has Sorted out and Rescoring.
- **Sorted out done** (2026-10-21). The candidate reviewed a sample of the Sorted out Jobs in the dashboard filter and the Jobs that moved up, and found the judgment sound.
- **Open for the user (Sorted out):** after the next Daily run the Digest should show the "aussortiert" line and no rescored Job. That is the issue's last "Done when" check, still open at the merge.
- **Indeed full text Part 1 built** (2026-10-27, `docs/issues/indeed-full-text.md`). `daily.lookup_full_text()` runs after `collect_mail` and looks up unscored, snippet-only Indeed Jobs (no Target company, with a company) at the Bundesagentur through the new `BundesagenturClient.lookup()` (one request, first page only, after the delay). A clear hit (same `normalize_company()` key, title similarity at least 0.8, place check by Job kind) is stored with `matching.add_posting()` and `"found_by": "lookup"` in `raw_json`, without `posting_terms`. `scoring.py` counts a Job as snippet-only unless a non-snippet Posting has a description; `evaluation.py` and `market.py` read the stored flag and needed no change.
  - Live check of the API: the `arbeitgeber` filter matches only the exact employer name (the name without its legal form found nothing), so the lookup searches by title and compares companies in Python. A title search near the ad's place found the same ad again in all of ten samples.
  - The first live run showed that a third of the Indeed Jobs were never scored: their alert mail named only the country (they come in through the alert's remote filter) or only a region. Decided with the candidate: a country means remote (`indeed_mail` sets `homeoffice_possible`, migration `011` moves stored Jobs from "Ort unbekannt" to home office). A remote Job is looked up nationwide and takes only a hit with home office. Without a full text, `settle_remote_jobs` keeps it on the hint "Remote-Filter des Indeed-Alerts" instead of dropping it for a snippet without a remote phrase, and scoring keeps it eligible when the AI answers "unknown"; "hybrid" or "none" still drop it. A Job with an unknown place is looked up at that place without a radius and takes the hit's place, and its Eligibility is decided again; without a hit it stays "Ort unbekannt".
  - `settle_remote_jobs` decides on the longest full text of a Job. Before, a Job with a snippet and a full text was decided by whichever row came last.
  - Live: the remote Jobs found no clear hit at the Bundesagentur, were kept on the alert's remote filter and scored as Low-confidence scores; the AI rejected about half of them as not fully remote. The two hits for region-only Jobs carry only the region at the Bundesagentur as well (no place, no description), so those Jobs stay "Ort unbekannt". **Open:** the issue's "Done when" (a hit's full text scored without the Low-confidence flag) needs new alert mails.
- **Indeed full text Part 2 built** (2026-10-27). After the Daily run the CLI prints "Volltext gesucht: n, gefunden: n, schon bekannt: n, verworfen: n, fehlgeschlagen: n" when there were lookups; the counts are in `runs.counts_json` as before. `jobsearch eval` has "Indeed-Jobs" with "Volltext per Lookup" (a full-text Posting marked `found_by: lookup`) and "Volltext anderweitig" as shares of all Indeed Jobs of the period; a Posting without description does not count. The Weekly report input has no metric for it yet. Live: no Indeed Job has a full text so far. **Open:** the hit rate after a few Daily runs with new alert mails, and whether the thresholds (0.8 title similarity, 30 km between the places; the candidate raised it from the issue's 15 km) need tuning.
- **Indeed full text Part 3 done** (2026-10-27). The German label of the Low-confidence score is "geringe Evidenz" instead of "geringe Sicherheit" in the CLI, the Digest, `eval`, `market`, the Weekly report input and the dashboard; `CONTEXT.md` names it. The code name `low_confidence` stays. "Sicherheit" in the Weekly report is the confidence of a Recommendation and stays as well.
- **Indeed full text checked on the existing alert mail Jobs** (2026-10-27). Instead of waiting for new mails, a one-off dry run applied the lookup rule to all Indeed Jobs without a full text, scored ones included (read-only, no AI), and a copy of the database attached the hits and scored them again.
  - Hit rate: about one in seven Indeed Jobs with a known place got a clear hit with a full text, a few more had a hit that was already stored or had no text. None of the remote Jobs got one: one had a BA Posting with the same title but without home office, the rest had no BA Posting of the same company. Overall about one in ten Indeed Jobs gains a full text, so the lookup is worth keeping, but most Indeed Jobs stay Low-confidence scores.
  - Thresholds: one near miss with a title similarity of about two thirds at the same place. Too few cases to lower 0.8; 30 km had no case near the limit.
  - On the copy, the Jobs with a hit were scored without the Low-confidence flag in one call, and `eval` showed them as "Volltext per Lookup". All of them stayed ★; with the full text the AI sorted out one more of them. That checks the "Done when" of Parts 1 and 2 on old mails; a run with new mails will show it in the live database.
- Phase 0 is the user's job: IMAP access + `verarbeitet` folder, and Indeed alerts (sample mails needed for Phase 5).

## Verified facts (2026-10-15)

Checked live against the Bundesagentur API before planning:

- The current API is **v6**: `https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs` with header `X-API-Key: jobboerse-jobsuche`. v4 and v5 answer 403.
- Results are in `ergebnisliste`. Each entry has `referenznummer` (stable source ID), `stellenangebotsTitel`, `firma`, `stellenlokationen` with coordinates, `entfernung` (km from the search location), `homeofficemoeglich`, `datumErsteVeroeffentlichung`, part-time flags and sometimes a salary range.
- `wo=<city>&umkreis=30` works on the server, and `entfernung` comes back with each result. **No postal code table is needed for Bundesagentur Postings.**
- `veroeffentlichtseit=<days>` limits results to recent ones, which is what catch-up after missed days needs.
- `arbeitszeit=tz` (part-time) works. **There is no working home office filter**: `arbeitszeit=ho` returns 0, and `homeofficemoeglich` exists only as a result field.
- The details endpoint answers 403. **The full description is in the public HTML page** `https://www.arbeitsagentur.de/jobsuche/jobdetail/<referenznummer>`, inside the embedded `ng-state` JSON (`stellenangebotsBeschreibung`).

Consequence for remote Jobs: there is no server filter for home office, so a nationwide query per Search term returns thousands of results for a common term. Phase 2 includes a short spike to pick a strategy. The current favorite: a nationwide query with remote words in `was` (for example "<term> Homeoffice"), restricted to recent results, keep only `homeofficemoeglich = true`, and let the scoring step decide "fully remote or hybrid".

## Phase 2 decisions (2026-10-15, from the first live runs)

- **Radius query without date limit.** Per Search term the radius query asks for everything currently online within `RADIUS_TARGET_COMPANY_KM` (a few hundred results in total). Every Job in reach is therefore seen again each day and `last_seen` stays accurate, which the Reopening rule depends on.
- **Remote strategy.** A nationwide query per Search term, limited by `veroeffentlichtseit` to the days since the last successful run (at most `REMOTE_MAX_WINDOW_DAYS`), keeping only `homeofficemoeglich = true`. Measured: dozens of new nationwide offers per term and day, a small share with home office.
- **Home office is usually hybrid.** In the first run only about 2% of the home office Jobs outside the radius had a full-remote phrase in their description. So Eligibility got a second, rule-based step: after the description is fetched, a home office Job outside the radius stays `unclear` only with such a phrase, otherwise it is ineligible with the reason stored. This keeps the AI quota for real candidates. The trade-off is that fully remote ads without one of the phrases are dropped. Check the stored reasons now and then, and extend the phrase list in `eligibility.py` if needed.
- **Distances are straight-line**, so they come out shorter than road distance.
- **Untitled offers.** A few Bundesagentur offers have no ad title. The occupation (`hauptberuf`) is used instead.
- Remote Jobs are only seen while they are inside the nationwide date window, so their `last_seen` is not accurate. Phase 4 has to keep this in mind for Reopenings of remote Jobs.

## Phase 3 decisions (2026-10-15)

- **How `claude -p` is called** (`ai.py`): prompt on stdin, `--json-schema` so the CLI itself enforces the answer shape and returns it in `structured_output`, `--tools ""`, `--strict-mcp-config`, `--disable-slash-commands`, a short `--system-prompt` instead of the coding one, `--no-session-persistence`, and the temp directory as working directory so no project CLAUDE.md is loaded. The model that actually answered is read from `modelUsage` and stored with each score.
- **`--bare` is not usable.** It would skip even more, but it only accepts an API key and never reads the subscription login.
- **Two layers of checking.** The schema catches malformed answers. `scoring.py` also checks that every Job in the batch was scored exactly once. Either failure gets one retry, then the run records the error and the Jobs stay unscored. Quota and login errors are recognized from the CLI's message and are not retried.
- **Remote candidates are settled in the scoring call.** Unclear Jobs with a full-remote phrase are scored like the rest. Only an AI `remote = full` makes them eligible, anything else makes them ineligible with the reason stored. Their score is kept either way.
- **Low confidence** is set when a Job has no description at all or only snippets. Two Jobs in the first run had no description, because their offers live on partner portals.
- **Descriptions are cut at 6,000 characters** to keep a 40-Job batch at a predictable size. A full batch took about one minute with Sonnet.
- **`daily` scores by default** (`--no-score` skips it), and `jobsearch score [--limit N]` scores alone. A small `--limit` is the cheap way to test prompt changes.
- **The Digest covers the whole day.** It lists every Job scored that day, so a second run rewrites it with everything. ★ Jobs are a plain link list, ★★★ and ★★ get the reasoning and skills.
- The toast (`win11toast`, non-blocking `notify`) was confirmed live. It also fires when scoring failed, so a quota problem does not go unnoticed.
- Unclear Jobs with an unknown location are not scored yet. They stay unclear until a later phase decides what to do with them.

## Phase 4 decisions (2026-10-15)

- **Provisional Job instead of `job_id = null`.** A Posting with a recent candidate becomes a Job right away, with `jobs.match_candidate` pointing at the candidate (migration 002). It needs a Job anyway: Eligibility and placement belong to it, and the scoring call has to rate it. When the AI says "same", `matching.merge` moves its Postings to the candidate and deletes it, before the Digest is written. So no duplicate is ever shown, and nothing waits on anything.
- **Candidate rule:** same `normalize_company` key and a Jaccard overlap of at least 0.5 between title tokens, with gender markers ((m/w/d), :in, all genders, ...) and filler words removed. On the live data, about 10% of the Jobs in reach got a candidate. Several pairs were clearly different roles at large employers, which is cheap to let the AI decide (about 1,500 characters per candidate in the prompt).
- **Only Jobs in reach are matched.** Ineligible Postings never look for candidates and are never candidates, since a duplicate nobody sees costs nothing.
- **Reopening needs a reliable `last_seen`.** It only applies to candidates within `RADIUS_TARGET_COMPANY_KM`, which the daily radius query sees every day. For remote Jobs the AI always decides "same Job?", whatever the age. The trade-off: a real reopening of a remote Job is merged into the old one.
- **The same ad coming back** (same source ID) after `REOPENING_DAYS` of absence is also a Reopening: its Posting moves to the new Job, the old Job keeps its history.
- **One call, three answers.** The scoring schema gained `generic` and `same_as_candidate`. A merged Job gets no score of its own. A chain of same Jobs in one batch collapses into the oldest. Generic jobs are never merged and never candidates.
- **Most Generic jobs are staffing agency ads.** In the live run, 11 of 40 Jobs were flagged generic, 10 of them from staffing agencies, which matches the definition in CONTEXT.md. The Digest marks them as "Sammelanzeige".
- Jobs collected before Phase 4 got their candidates once through a local script, so the live run had real pairs to decide.

## Phase 5 decisions (2026-10-16 and 2026-10-18)

- **Built without sample mails:** IMAP settings (`IMAP_*`, optional, so the Daily run skips mail when they are empty), `Mailbox` with login, search by sender, `BODY.PEEK` so mails stay unread, and MOVE with a COPY + delete fallback. In `run_daily`, a mail is moved only after its Postings are committed, and a mail without Postings stays in the inbox and shows up as a warning in the Digest. A mailbox failure is recorded as a collection error and does not stop the run.
- **The parser reads the plain-text part**, not the HTML. Each job is one block: title, "Company - Place", optional salary and marker lines ("Schnellbewerbung", "Aktiver Arbeitgeber"), snippet, age, tracking link. Header and footer blocks are skipped because their last line is not a job link. The HTML part is layout only.
- **Two kinds of results.** Organic ones link to `/rc/clk` with the job key `jk`, which becomes the source ID and a neutral `viewjob?jk=` URL. Sponsored ones (`/pagead/clk`) carry no key, so their source ID is a hash of title, company and place: the same ad in a second mail is recognized. Personalized tracking links are not kept for organic results, as the mail asks not to pass them on. In the samples, more than half of the results were sponsored, and many of them were off-topic or outside the alert's radius.
- **Tests use a synthetic copy** of the real block structure (`tests/fixtures/indeed_alert.eml`). The real mails stay in `data/` and never go into git, since they contain personalized links.
- **Places are looked up in the GeoNames postcode table**, which moved here from Phase 8: `jobsearch init` downloads DE and CH (CC BY 4.0) into `data/geo/places.tsv`. Postcodes of one place and district are averaged. A short or ambiguous name resolves to the candidate nearest home, which is safe because alerts are limited to a radius around home. Country-only places ("Deutschland", typical for sponsored ads) get no location and stay `unclear`, so they are never scored.
- **A "Remote" place sets home office**, so such a Job goes through the same full-remote check as a Bundesagentur Job. No remote alert was in the samples yet, so this path is only covered by the synthetic fixture.
- **The processed folder is resolved by name** before any mail is touched. A server may nest it under the inbox (`INBOX/verarbeitet`), so a folder whose last path part matches is accepted. A missing folder fails the mail step before anything is stored.
- **A missing Profile no longer aborts the run.** It is recorded like an AI failure, the Digest is still written, and the Jobs stay unscored.
- **Runtime grows with Search terms.** Every new home office Job outside the radius gets its description fetched for the full-remote check. After a few days without a successful run and with more Search terms, one run fetched several hundred descriptions and took about six minutes. Daily runs stay small, since the window only covers the days since the last successful run.
- **Indeed also sends account mails** (login codes, alert confirmations, profile changes), from the same domain. They are recognized by whole subject phrases and left alone, so they are not reported every day. Single words are not enough, since words like "Account" also appear in job titles.
- **A new alert only starts after its confirmation mail is clicked.** Worth noting in Phase 0.

## Changed from the design session

Career pages that need JavaScript are rendered by Python with Playwright (headless Chromium, free) and not handed to an agent. A Claude agent's web fetch does not run JavaScript either, so the agent would have had the same problem. The AI in the Career scan now only *extracts* Postings from already rendered page text and never browses. This makes ADR 0001 hold more strictly.

## Tech stack

- Python 3.11, packaged with `pyproject.toml`, installed in a local `.venv`
- `requests`, `beautifulsoup4`, `python-dotenv`, `flask` (dashboard), `win11toast` (toasts with click action), `playwright` (Career scan only), `pytest`
- SQLite through the standard library `sqlite3`, schema versioned with plain numbered SQL files
- AI through `claude -p` (Claude Code headless, Pro subscription), called as a subprocess with input on stdin, `--model` from config, `--output-format json`, and no tools allowed for scoring and extraction
- Windows Task Scheduler for the trigger, running `pythonw` so no console window appears

## Repository layout

```
JobSearch/
├── CONTEXT.md
├── README.md
├── pyproject.toml
├── .gitignore                 # .env, private/, data/, .venv/
├── .env.example
├── private.example/           # dummy versions of the private files
├── private/                   # gitignored: real company list, exclusions, Search terms
├── data/                      # gitignored: jobsearch.sqlite, digests/, postcodes, logs
├── docs/adr/, docs/implementation-plan.md
├── src/jobsearch/
│   ├── cli.py                 # jobsearch init | run | daily | career-scan | score | dashboard | status
│   ├── config.py              # loads .env and private/ files, fails clearly when something is missing
│   ├── db.py, migrations/
│   ├── sources/bundesagentur.py, sources/indeed_mail.py, sources/career_pages.py
│   ├── eligibility.py         # distance, Obvious mismatch rule
│   ├── matching.py            # Posting → Job, Reopening, Generic job flag
│   ├── ai.py                  # the only module that calls claude -p
│   ├── scoring.py, prompts/
│   ├── digest.py, notify.py
│   ├── scheduler.py           # what is due, lock file, catch-up window
│   └── dashboard/             # Flask app + templates (German labels)
└── tests/ (with fixtures/: saved API JSON, detail HTML, Indeed .eml, career pages)
```

## Configuration

`.env` (private):
`PROFILE_PATH`, `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_PROCESSED_FOLDER=verarbeitet`, `MODEL_SCORING=sonnet`, `MODEL_EXTRACTION=haiku`, `MAX_SCORED_PER_RUN=40`, `FEEDBACK_NOTES_IN_PROMPT=20`, `HOME_LOCATION`, `HOME_LAT`, `HOME_LON` (required, no defaults), `RADIUS_KM=30`, `RADIUS_TARGET_COMPANY_KM=70`, `REOPENING_DAYS=21`, `CAREER_SCAN_INTERVAL_DAYS=7`

`private/` (gitignored, with dummies in `private.example/`):
- `companies.csv`: name, locations, career URL, ATS type, render mode (filled in by the Career scan setup)
- `exclusions.txt`: one title pattern per line (Praktikum, Werkstudent, Abschlussarbeit, ...)
- `search_terms.txt`: manual Search terms. Derived terms are stored in the database with the Profile hash.

## Database schema (first version)

- **jobs**: id, company, title, location, lat, lon, distance_km, is_target_company, is_generic, remote_type (unknown/hybrid/full), eligibility (eligible/ineligible/unclear), ineligible_reason, first_seen, last_seen, reopening_of (→ jobs.id), status (new/interesting/not_interesting/applied/rejected), status_changed_at, applied_on
- **postings**: id, job_id (null while a match is pending), source (bundesagentur/indeed/career_page), source_id, url, title, company, location_raw, description, is_snippet, raw_json, first_seen, last_seen. Unique on (source, source_id).
- **fit_scores**: job_id, tier (3/2/1), reasoning, matching_skills, missing_skills, part_time_signal, remote_signal, low_confidence, profile_hash, model, scored_at. One row per scoring, so history is kept.
- **feedback_notes**: id, job_id, status, note, created_at
- **search_terms**: term, origin (derived/manual), profile_hash, active
- **career_pages**: company, url, content_hash, last_checked, last_changed
- **runs**: id, kind (daily/career_scan/score), started_at, finished_at, ok, counts_json, error

## Phases

### Phase 0: Prerequisites (by hand)

1. Mailbox: enable IMAP in the settings, create the folder `verarbeitet`, note the password for `.env`.
2. Indeed: create alerts around the home location with a radius of 30 km and a few remote alerts, all sent to the new address, and click the confirmation mail of each. Let 2–3 mails arrive, since Phase 5 needs real samples.
3. Check that `claude -p "say ok"` works in a fresh terminal with the Pro login.

### Phase 1: Skeleton

- `pyproject.toml`, `.gitignore`, `.env.example`, `private.example/`, README with setup steps
- `config.py` with clear errors for missing values, and `db.py` with migration 001 (schema above)
- CLI `jobsearch init` (creates `data/`, the database and `private/` from the examples) and `jobsearch status` (counts per table, last runs)
- **Done when** `jobsearch init && jobsearch status` works on a fresh clone, and `git status` shows no private file.

### Phase 2: Bundesagentur collection and Eligibility (no AI yet)

- `sources/bundesagentur.py`: radius query per Search term (manual terms for now) with `veroeffentlichtseit` = days since the last successful run (at least 1, at most 100). Paging, and a polite delay between requests.
- Fetch the detail HTML for each *new* `referenznummer` only, and extract `stellenangebotsBeschreibung` from `ng-state`.
- Remote strategy spike (see Verified facts). Decide, and record the result in this file.
- `eligibility.py`: Obvious mismatch from `exclusions.txt` (case-insensitive word match on the title), distance from `entfernung` or coordinates, Target company radius when the company matches the list.
- `matching.py`, minimal version: one Posting per Job, deduplicated only by source ID, and `last_seen` updated on every sighting.
- **Done when** `jobsearch daily` fills the database with real Jobs, a second run on the same day adds nothing new, and the tests pass on saved API fixtures.

### Phase 3: Fit scoring, Digest, toast

- `ai.py`: one function that runs `claude -p` with prompt + JSON input on stdin, parses the JSON answer, retries once on invalid output, and raises a clear error on quota or login problems.
- `scoring.py`: picks up to `MAX_SCORED_PER_RUN` unscored eligible Jobs (newest first), batches them into one call with the Profile text, the last Feedback notes and the scoring rubric (tiers, signals, German reasoning). Snippet-only Jobs are marked `low_confidence`. Writes `fit_scores` with the Profile hash.
- Failure handling: when the AI step fails, the run still counts as collected, Jobs stay unscored, and the error lands in `runs`.
- `digest.py`: German Markdown in `data/digests/YYYY-MM-DD.md` with new Jobs by tier, Reopenings and "x Jobs waiting for scoring".
- `notify.py`: a toast only when there are new ★★★/★★ Jobs or a scoring backlog. Clicking it opens the dashboard (a Digest file until Phase 7).
- **Done when** one real daily run scores at most 40 Jobs and the reasoning holds up when you read it. Check this against 10 Jobs you would rate yourself.

### Phase 4: Job matching across Sources, Generic jobs, Reopenings

- Candidate search for a new Posting: same normalized company (legal forms and "(m/w/d)" stripped) and similar title (token overlap above a threshold).
- If the candidate was last seen less than `REOPENING_DAYS` ago, the AI decides "same Job?" in the same daily call as scoring. Until then the Posting has `job_id = null`, and it never blocks anything else.
- If the candidate was last seen 21+ days ago, create a new Job with `reopening_of`.
- Generic job flag: the AI sets it during scoring, and matching skips flagged Jobs.
- **Done when** fixture tests cover: same Job on two Sources, same title for two different Jobs, a repost within 21 days, and a Reopening after 30 days.

### Phase 5: Indeed alert mail

- `sources/indeed_mail.py`: IMAP over SSL to `IMAP_HOST`, only mails from Indeed senders in the inbox. Parse each job block (title, company, location, snippet, link), resolve the `jk` job key from the tracking link as the source ID, and move the mail to `verarbeitet` only after its Postings are stored.
- The parser is built and tested against the real sample mails from Phase 0. A mail that yields zero Postings stays in the inbox and is reported in the Digest.
- Snippet rule: a Low-confidence score is shown normally. If the snippet already looks like a mismatch, no lookup for a fuller description happens.
- **Done when** the sample mails parse in tests and a live run moves them.

### Phase 6: Scheduling

- `scheduler.py` and `jobsearch run`: take a lock file, run the Daily run if none succeeded today, run the Career scan if the last one is `CAREER_SCAN_INTERVAL_DAYS` old or older, then score, then send the Digest and toast. If the network is not up yet, retry 3 times with backoff and then give up quietly with a toast.
- `jobsearch install-task`: registers a Task Scheduler task with a logon trigger and 5-minute delay, running `pythonw -m jobsearch run` as the current user (so the Claude login is available). `jobsearch uninstall-task` removes it.
- Logging to `data/logs/` with rotation.
- **Done when** two logons on one day give exactly one run, and a simulated 4-day gap catches up 4 days.

## Phase 6 decisions (2026-10-18)

- **"Done today" means a Daily run finished today, with or without errors.** Otherwise a lasting error (a wrong mail password) would repeat the whole run and its AI call at every logon. The errors are in the Digest and the toast, and the next day's remote window still reaches back to the last run without errors.
- **The lock is an OS file lock** (`data/run.lock`), not a PID file. Windows drops it when the process dies, so a crash never blocks later runs. Manual `daily` and `score` take the same lock, so they never overlap the scheduled run.
- **Network check:** a TCP connect to the Bundesagentur host, retried after 30, 60 and 120 seconds. Then a toast, and the next logon tries again.
- **The task is defined as XML** and registered with `schtasks /XML`, since plain `schtasks` options cannot set a logon delay together with "run on battery" and "only in the user's session". It runs with the user's interactive token and least privilege, so `claude -p` sees the user's login.
- **Headless means no console.** Under `pythonw` there is no stderr, so the console log handler is skipped, every failure ends in the log and a toast, and `claude -p` is started without a window (`CREATE_NO_WINDOW`).
- **Logs rotate** at about 1 MB, five files kept.
- **The Career scan is not part of `run` yet.** It joins in Phase 8, using `CAREER_SCAN_INTERVAL_DAYS`.

### Phase 7: Dashboard

- `jobsearch dashboard`: Flask on `127.0.0.1`, opens the browser.
- Job list with filters (Fit score, Job status, Eligibility, date, Source), "new since last visit" marker, low-confidence and Generic job badges.
- Detail view: full description, all Postings with links, score reasoning with matching and missing skills, Reopening chain.
- Status buttons with an optional Feedback note. "applied" sets `applied_on` to today.
- German labels, read and write on the same SQLite file.
- **Done when** a status change in the browser shows up in the next scoring prompt's Feedback notes.

## Phase 7 decisions (2026-10-18)

- **Server-rendered Flask with Jinja templates**, no JavaScript build. The filters submit on change; without JavaScript there is a button.
- **The list hides ineligible Jobs by default** ("in Reichweite oder unklar"). "alle" and "außerhalb" make them visible, to find wrong filter decisions.
- **"New since last visit" uses Job ids**, not dates: the dashboard remembers the highest Job id of the previous visit in a small `meta` table (migration 003). Page views less than an hour apart count as one visit, detail pages included, so filtering and reading keep the markers. A visit ends after eight hours at the latest. The very first visit marks everything as new. If a run holds the write lock, the visit is just not recorded and the page still loads.
- **Local only:** the server listens on `127.0.0.1`, refuses requests with another host name (against DNS rebinding), and status changes need a token created at each start. Any web page could otherwise send a form to `127.0.0.1` and change a status, and with it the scoring prompt.
- **The status can also be set right in the list**, for Jobs whose title already rules them out. The page reloads at the same row with the same filters; a note needs the detail page.
- **Search terms are recorded per Posting** (`posting_terms`, migration 004), so the list can be filtered by the term that found a Job, with a Job count per term. This helps to judge which terms bring mostly noise. Only Bundesagentur Postings have terms. Older Postings get theirs when a run sees them again, which the radius query does daily for everything in reach.
- **"applied" sets `applied_on` only once.** Choosing it again later keeps the first date.
- **A Feedback note is stored only when text is entered**, together with the status chosen at that moment. The scoring prompt already reads the most recent notes (`FEEDBACK_NOTES_IN_PROMPT`).
- **The Reopening chain** follows `reopening_of` in both directions, so every advertisement of the same opening links to the others.

### Phase 8: Career scan

- **Setup** (`jobsearch career-setup`, one time and then on demand): for each company in `private/companies.csv` without a URL, a `claude -p` call with web search finds the career page and guesses the ATS type. The result is written back to the CSV for you to check. This is the only step where the AI browses.
- Postcode table: already there since Phase 5 (`geo.py`), reuse it for career page locations.
- **Weekly scan:**
  1. Known ATS feeds (for example Personio XML) are parsed directly, without AI.
  2. Other pages are fetched with `requests`, or Playwright when the render mode says so. Visible text is normalized and hashed.
  3. Only pages whose hash changed go to `MODEL_EXTRACTION` (Haiku), which turns the text into Posting JSON.
  4. Postings then run through the normal Eligibility, matching and scoring.
- Report companies whose page failed or returned no Postings for two scans in a row.
- **Done when** the first full scan covers all companies and reports how many needed Playwright, and the second scan in the same week sends close to nothing to the AI.

## Phase 8 decisions (2026-10-18)

- **Setup answers by number, not by name.** The first live run failed because the model tidied up company names ("XY GmbH" became "XY"), so the check never matched. Each company now gets a number in the prompt.
- **The setup finds landing pages more often than job lists.** About two thirds of the companies got a URL in the first pass, and most of those were general "Karriere" pages without the list itself. `career-setup --recheck` asks again for the companies whose page failed or listed nothing, and passes on what went wrong. Entries without problems are never overwritten. `companies.csv.bak` keeps the state from before the first setup.
- **Static HTML first, browser only when needed.** Almost every German job title carries a gender marker like "(m/w/d)". A page without any is rendered with Playwright instead of being sent to the AI, which in the first scan would have saved most of the extraction calls. After rendering the page goes to the AI either way, since English pages have no marker.
- **Playwright is optional** (`pip install -e ".[browser]"`, then `playwright install chromium`). Without it, such pages are reported as needing a browser and cost no AI call.
- **The hash covers the visible text without URLs**, so tracking parameters in links do not count as a change. Rendered pages turned out stable between scans.
- **An unchanged page keeps its Postings fresh.** Their `last_seen` moves to the scan date, otherwise a weekly page would look absent and later produce false Reopenings.
- **Feeds before AI.** Personio's XML feed gives title, offices and full description without any AI call. Other ATS types were rare in the setup result (a couple of Workday and SuccessFactors sites), so they go through the page path for now.
- **A Posting without a place gets the company's locations** from `companies.csv`. Many job lists show no place per job, and without one the Job would stay unclear and unscored.
- **Descriptions without AI.** For new career Postings in reach, the linked job page is fetched and its visible text stored as the description, so the scoring sees more than the title.
- **Reported after two scans in a row:** a page that fails (with the error) or lists no Postings. A changed URL starts counting from zero.
- **The scan runs inside the Daily run when due** (`CAREER_SCAN_INTERVAL_DAYS`, default 7), before scoring, so its Jobs are scored in the same run. A scan whose AI step failed does not count as done. `jobsearch career-scan` runs it alone.

### Phase 9: Search terms derived from the Profile

- When the Profile hash changes, one `claude -p` call proposes 5–8 Search terms from the Profile. They are stored as `derived` and used together with `private/search_terms.txt`.
- Shown in `jobsearch status`, so you can see which terms are active.
- **Done when** editing the Profile file changes the derived terms in the next run, and existing Fit scores are left alone (as decided, no re-scoring).

## Phase 9 decisions (2026-10-18)

- **Only derived terms live in the `search_terms` table**, each with the Profile hash it came from. Manual terms stay in `private/search_terms.txt`, so the file remains the one place to edit by hand.
- **A changed Profile replaces the whole derived set.** In the live check a small addition to the Profile kept only about a quarter of the terms, so the set can shift noticeably on an edit. Terms found earlier stay recorded on their Postings.
- **Opt-out with `-term`** in `private/search_terms.txt`, for derived and manual terms alike. It survives a new derivation, since the AI does not see it.
- **A failed derivation keeps the old terms** and is tried again at the next run. It becomes a warning in the Digest. Without a Profile, the daily run searches the manual terms only; the scoring step already reports the missing Profile.
- **Plain terms only.** An answer with empty or duplicate terms, more than four words, or parentheses and quotes is asked for again, since each term goes into the Bundesagentur `was` field as it is.
- **More terms, more Jobs to score.** The first run with derived terms brought a few dozen new Jobs. `MAX_SCORED_PER_RUN` still caps each scoring call, so the extra Jobs are spread over the next runs.
- No re-scoring when the Profile changes, as decided earlier.

## Testing approach

- Every Source parser is tested against saved real responses in `tests/fixtures/`, using anonymized copies and never personal data.
- Eligibility, matching and Reopening logic are pure functions with table-driven tests.
- `ai.py` is replaced by a fake in tests, so the test suite never uses quota.
- One manual end-to-end check per phase against the live Sources, as noted under "Done when".

## Risks and open points

- **Unofficial API:** the Bundesagentur API is public but not formally supported, and it already moved from v4 to v6. Keep the client small and fail loudly on schema changes.
- **Indeed mail format** can change without notice. The parser fails visibly, since mails stay in the inbox.
- **Pro quota:** the per-run cap and the Haiku extraction keep usage low. Watch the first week and tune `MAX_SCORED_PER_RUN`.
- **Claude login inside Task Scheduler:** the task runs in the user's session, which should be enough. Confirm with the first run started by the task (see Progress).
- **Career pages** change layout and URLs. The "no Postings twice in a row" report catches this.
