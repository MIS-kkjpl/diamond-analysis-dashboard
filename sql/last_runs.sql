-- Pipeline run queries (run in pgAdmin against kkj_erp_stock).
-- Days are India dates, so a 21:30 attempt and its 21:40 retry count as one day.
-- Interrupted runs (killed by the Task Scheduler time limit, PC shut down) are
-- marked 'failed' with error 'interrupted (...)' at the start of the next run.
-- Until then they stay 'running', and queries 1 and 2 already count a 'running'
-- row older than 1 hour as a failed attempt.

-- 1. Last 10 runs.
--    recovered_after_failure = a successful run that came after a failed run on
--    the same day (e.g. run_pipeline.bat's retry worked).
SELECT r.run_id,
       r.started_at,
       r.finished_at - r.started_at AS duration,
       r.status,
       r.record_count,
       r.rows_loaded,
       r.error,
       r.dq_warnings,
       (r.status = 'success' AND EXISTS (
            SELECT 1
            FROM erp.etl_run_log f
            WHERE (f.status = 'failed'
                   OR (f.status = 'running' AND f.started_at < now() - interval '1 hour'))
              AND f.started_at < r.started_at
              AND (f.started_at AT TIME ZONE 'Asia/Kolkata')::date
                = (r.started_at AT TIME ZONE 'Asia/Kolkata')::date
       )) AS recovered_after_failure
FROM erp.etl_run_log r
ORDER BY r.run_id DESC
LIMIT 10;

-- 2. One line per day: did it work first time, after a retry, or not at all?
--    "failed" includes interrupted runs, both once marked and while still 'running'.
WITH runs AS (
    SELECT run_id,
           (started_at AT TIME ZONE 'Asia/Kolkata')::date AS run_date,
           CASE
               WHEN status = 'running' AND started_at < now() - interval '1 hour'
                   THEN 'failed'                       -- interrupted, not yet marked
               ELSE status
           END AS status,
           error LIKE 'interrupted%'
               OR (status = 'running' AND started_at < now() - interval '1 hour') AS interrupted
    FROM erp.etl_run_log
)
SELECT run_date,
       count(*)                                              AS attempts,
       count(*) FILTER (WHERE status = 'failed')             AS failed,
       count(*) FILTER (WHERE interrupted)                   AS interrupted,
       count(*) FILTER (WHERE status = 'success')            AS succeeded,
       CASE
           WHEN count(*) FILTER (WHERE status = 'success') = 0
            AND count(*) FILTER (WHERE status = 'running') > 0  THEN 'still running'
           WHEN count(*) FILTER (WHERE status = 'success') = 0  THEN 'FAILED'
           WHEN count(*) FILTER (WHERE status = 'failed') = 0   THEN 'ok first time'
           ELSE 'recovered after failure'
       END                                                   AS outcome,
       string_agg(status || CASE WHEN interrupted THEN ' (interrupted)' ELSE '' END
                  || ' #' || run_id, ' -> ' ORDER BY run_id) AS sequence
FROM runs
GROUP BY run_date
ORDER BY run_date DESC
LIMIT 10;

-- 3. Data-quality warning counts across runs (trend), newest first.
SELECT run_id,
       (started_at AT TIME ZONE 'Asia/Kolkata')::date                  AS run_date,
       status,
       (dq_warnings -> 'null_labeling_date'          ->> 'count')::int AS null_labeling_date,
       (dq_warnings -> 'zero_item_amt_not_customer'  ->> 'count')::int AS zero_item_amt_not_customer,
       (dq_warnings -> 'customer_items'              ->> 'count')::int AS customer_items,
       (dq_warnings -> 'purity_outside_karat_ranges' ->> 'count')::int AS purity_outside_karat_ranges
FROM erp.etl_run_log
WHERE dq_warnings IS NOT NULL
ORDER BY run_id DESC;
