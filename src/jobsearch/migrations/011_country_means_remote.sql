-- An alert mail Posting with only a country as its place came in through the alert's remote filter.
-- Such Jobs were stored with an unknown place and are now home office Jobs, settled by the next Daily run.
UPDATE jobs SET ineligible_reason = 'Homeoffice möglich, ob vollständig remote ist offen'
WHERE eligibility = 'unclear' AND ineligible_reason = 'Ort unbekannt' AND location IS NULL
  AND EXISTS (SELECT 1 FROM postings p WHERE p.job_id = jobs.id AND p.source = 'indeed'
              AND json_extract(p.raw_json, '$.place') IN
                  ('Deutschland', 'Germany', 'Schweiz', 'Switzerland', 'Österreich', 'Austria'));
