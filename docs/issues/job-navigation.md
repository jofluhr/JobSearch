# Issue: Previous and next new Job on the dashboard detail page

Status: open. Implement on branch `implement-job-navigation`.

## Goal

To work through the list, the candidate today opens a Job, goes back to the list and opens the next one. The detail page gets links to the previous and the next Job, meaning the Job above and below in the dashboard list. Only Jobs the candidate has not judged yet (Job status new) count, so the links skip every Job that already has a status. A second button "Speichern und weiter" sets the status and opens the next new Job in one step.

No AI calls, no migration.

## Decisions

- **"New" means Job status new**, not the "neu" badge (new since the last visit). The badge depends on the visit and would change while the candidate clicks through.
- **The list's filters apply.** The detail page carries the list's filters (tier, Eligibility, source, Search term, found since) in its query string. The links follow exactly the order the candidate saw, and "← Alle Jobs" returns to the filtered list.
- **The status filter does not apply to the navigation.** It is always Job status new; a list filtered to "interessant" would otherwise have no neighbours at all. The status filter is still carried back to the list.
- **The current Job keeps its place.** Neighbours are taken from the filtered list of new Jobs plus the current Job, so a Job that was just judged still knows its previous and next Job.
- **A Job outside the filtered list has no links** (for example opened from the Reopening chain on another detail page). No error, no guess.
- **"Speichern" stays on the Job** as today. "Speichern und weiter" is a second button; without a next new Job it returns to the filtered list.

## Facts to build on

- `dashboard.job_filter(args)` builds the WHERE clause from the query string and ignores unknown values. The list orders by `f.tier IS NULL, f.tier DESC, j.first_seen DESC, j.id DESC` (inline in `jobs()`).
- `set_status()` already takes a `next` form field and only follows paths of this app. The list uses it to return to the same filtered list.
- Sorted out Jobs with status new are hidden by `job_filter` unless the tier filter is "aussortiert", so the navigation inherits that rule.

## Part 1: Navigation and "Speichern und weiter"

- `dashboard.py`: the list order becomes a constant `JOB_ORDER`, used by the list and the navigation. A function `neighbours(conn, args, job_id)` returns the previous and next Job id: `job_filter` without the status filter, plus `(j.status = 'new' OR j.id = ?)`, in `JOB_ORDER`.
- `job_detail()` passes the filters, the neighbours, the page's own path and the target of "Speichern und weiter" to the template.
- `jobs.html`: the title links carry the chosen filters (the default Eligibility `reach` is left out).
- `job.html`: "← Alle Jobs" goes to the filtered list; next to it "← vorheriger neuer Job" and "nächster neuer Job →" when there is one. The form has two buttons, each sending its own `next` value, so only the clicked one counts.
- Tests in `tests/test_dashboard.py`: order and skipping of judged Jobs, first and last Job, filters carried and applied, the status filter ignored, a judged Job keeps its neighbours, both buttons redirect correctly, a Job outside the list has no links.

## Done when

On the live database (dashboard or Flask test client on a copy), the candidate filters the list, opens a Job, clicks through with "nächster neuer Job" in the list's order, and judges Jobs with "Speichern und weiter"; judged Jobs are skipped. Every page still renders well under a second. Findings go into `docs/implementation-plan.md`.
