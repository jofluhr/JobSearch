# JobSearch

Daily, mostly automated discovery of job openings for one candidate within reach of their home location or fully remote. It collects ads from several sources, merges duplicates into real openings, judges them against the candidate's profile and keeps a lasting record of everything seen.

## Language

### Sources and ads

**Source**:
A place ads come from, such as the Bundesagentur API, the Indeed alert mail or a target company's career page.
_Avoid_: Portal, channel, feed

**Posting**:
One ad as it appears on one Source, identified by that Source's own ID or URL.
_Avoid_: Listing, ad, Stellenanzeige

**Target company**:
A company on the candidate's curated company list whose career page is checked directly.
_Avoid_: Firma, employer

### Openings

**Job**:
The real opening behind one or more Postings. Several Postings can belong to the same Job, and one job title can belong to different Jobs.
_Avoid_: Stelle, position, vacancy

**Generic job**:
A Job without one concrete opening behind it, such as a multi-role ad, an open application call or a recruiter ad for an unnamed client. It is never merged with a specific Job.
_Avoid_: Sammelanzeige, umbrella posting

**Reopening**:
A new Job created when an opening reappears after it was absent from every Source for 21 days or more. It stays linked to the earlier Job.
_Avoid_: Repost, duplicate

### Judging

**Profile**:
The candidate's documented skills, experience and preferences, kept outside this repo and only read here.
_Avoid_: CV, knowhow

**Eligibility**:
A rule-based yes or no on whether a Job is in reach. A Job is eligible when it is within 30 km of the home location, within 70 km for Target companies, or fully remote, and it is not an Obvious mismatch. No judgment involved.
_Avoid_: Filter, fit

**Obvious mismatch**:
A Job whose title alone rules it out, such as an internship or a thesis, based on the candidate's private exclusion list.
_Avoid_: Junk, spam

**Fit score**:
The AI's judgment of how well an eligible Job matches the Profile, in three tiers (★★★, ★★, ★).
_Avoid_: Rating, match, relevance

**Low-confidence score**:
A Fit score based only on a short snippet because no full description was available. It is shown like any other score, never hidden.
_Avoid_: Preliminary score. In German output it is "geringe Evidenz".

**Sorted out** ("aussortiert"):
A ★ Job the AI marked as clearly outside the candidate's field. It stays in the database with its Fit score but is hidden by default until the candidate sets a Job status. In code it is `sorted_out`.
_Avoid_: Rejected, dismissed, junk

**Rescoring** ("Neubewertung"):
Rating ★ Jobs again after the scoring prompt changed, by the manual command `jobsearch rescore`. It adds a newer Fit score and keeps the old one; readers count a Job by its newest Fit score.
_Avoid_: Re-rating, refresh

**Job status**:
The candidate's own progress with a Job, set by hand. It is separate from Eligibility and Fit score. One of new, interesting, not interesting, applied or rejected.
_Avoid_: State, stage

**Not interesting**:
The candidate's own decision that a Job is not worth applying to.
_Avoid_: Dismissed, rejected, declined

**Rejected**:
The company's decision against the candidate after an application.
_Avoid_: Declined, Absage (when the candidate decided)

**Feedback note**:
An optional free-text comment the candidate adds when setting a Job status. Recent notes guide future Fit scores.
_Avoid_: Reason, comment

**Search term**:
A keyword used to query a Source. Each term is either derived from the Profile or added by hand.
_Avoid_: Query, keyword list

### Runs and output

**Daily run**:
The collection of Postings from the Bundesagentur and the alert mail, due at the first logon of each day.
_Avoid_: Sync, crawl

**Career scan**:
The check of all Target company career pages, due once it is 7 or more days since the last one.
_Avoid_: Company search, weekly run

**Digest**:
The summary of newly found Jobs and Reopenings, grouped by Fit score.
_Avoid_: Report, results file

### Evaluation

**Settings snapshot**:
What was in effect for one Daily run: scoring model, prompt and Profile hashes, active Search terms, exclusion list, radii and enabled Sources. A date where the snapshot differs from the one before is a change date, the split point for before/after comparisons.
_Avoid_: Config dump, version

**Stage**:
One step of the pipeline a Job passes or gets lost at: collected, Eligibility, scoring, Fit score tier. Each stage has its own lever, such as Search terms, radius or the scoring prompt.
_Avoid_: Step, phase (phases are build steps in the plan)

**Label**:
The Job status read as ground truth for the Fit score: interesting, applied and rejected are positive, Not interesting is negative, new is unlabeled. A positive Job in ★ is a miss.
_Avoid_: Rating, feedback

**Found elsewhere**:
An opening the candidate found outside the tool and recorded in `private/found_elsewhere.csv`. Tracing it shows the Stage where the pipeline lost it, including openings never collected at all.
_Avoid_: External job. In German output it is "Fremdfund".

**AI usage**:
The measured tokens, duration and API equivalent of every `claude -p` call, recorded per purpose (scoring, career extraction, ...), retries included.
_Avoid_: Cost, quota. In German output it is "KI-Verbrauch".

**API equivalent**:
The list price the same calls would cost through the API. The AI runs on the subscription, so it is neither a real cost nor a share of the quota.
_Avoid_: Cost, price. In German output it is "API-Gegenwert".

**Weekly report**:
The weekly AI reading of the pipeline's own metrics, with a short assessment and up to five Recommendations. Every number in it comes from the database; the AI only cites metric IDs.
_Avoid_: Analysis, summary. In German output it is "Wochenbericht".

**Recommendation**:
One proposed change to a Lever, backed by metric IDs, which the candidate accepts or rejects.
_Avoid_: Suggestion, advice. In German output it is "Empfehlung".

**Lever**:
A setting that changes what a Stage lets through, from a fixed list in code: a Search term, the exclusion list, the radius, remote detection, the scoring prompt, the Profile, a career URL, or labelling more Jobs. The AI can propose new ones; only the candidate adds them.
_Avoid_: Knob, parameter. In German output it is "Stellschraube".

**Market report**:
The view of Skill groups and Active companies across all scored Jobs, from `jobsearch market` and the dashboard page "Markt".
_Avoid_: Market analysis, trends. In German output it is "Marktbericht".

**Skill group**:
A lasting group that skill texts from Fit scores belong to, so that differently worded skills are counted together. The AI assigns each text once; `jobsearch market --regroup` starts over.
_Avoid_: Category, cluster. In German output it is "Skill-Gruppe".

**Active company**:
A company with 3 or more Jobs in reach within 90 days, or a Reopening, not counting Generic jobs.
_Avoid_: Hot company, top employer. In German output it is "aktive Firma".

### Trust

**Third-party text**:
Text written by others that reaches the AI: job ads, career pages and the web pages the career setup reads. It is data to judge, never instructions, and it reaches the model only inside the prompts' JSON structures. Only the prompts and the Profile define the task.
_Avoid_: User input, content

**Injection flag**:
The AI's signal that a Job's ad or a career page contains text addressed to it or trying to steer the Fit score or the output. It is counted, logged and named in the Digest, but it is a signal, not a verdict: the Job is scored and the page read as usual.
_Avoid_: Attack, blocked job. In code it is `injection_suspected`.

**Injection check**:
The measurement of how the real prompts hold up against Third-party text that addresses the model, run with synthetic Jobs and a synthetic career page (`jobsearch check-injection`, two AI calls). Each run is recorded in the plan, so a prompt change can be compared with the runs before.
_Avoid_: Test (tests never call the AI), eval (that reads stored runs)
