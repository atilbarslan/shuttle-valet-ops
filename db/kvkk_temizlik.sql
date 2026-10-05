-- ============================================================
-- KVKK automatic deletion: requests (passenger/customer personal data) after 30 days
-- ============================================================
-- Deletes request rows older than 30 days. talepler holds passengers' and customers' personal
-- data (musteri_ad, musteri_tel, konum_lat, konum_lng). Reports cover at most 30 days, so
-- older rows are not needed and are deleted entirely.
-- Runs inside the database with Supabase pg_cron; there is no external script or cron.
-- Run in the Supabase SQL editor, in order: step 0, 0b, 1, 2, then the checks.
-- ============================================================


-- Step 0: check the column type (once).
-- If kayit_tarihi is 'timestamp with time zone' the cast below is unnecessary but harmless.
-- If it is 'text', the ::timestamptz cast is required.
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'talepler' AND column_name = 'kayit_tarihi';


-- Step 0b: dry run. Deletes nothing; shows what would be deleted.
-- How many rows?
SELECT count(*) AS silinecek_talep_sayisi
FROM public.talepler
WHERE kayit_tarihi IS NOT NULL
  AND kayit_tarihi::timestamptz < now() - interval '30 days';

-- The 10 oldest, for a visual check.
SELECT id, musteri_ad, durum, kayit_tarihi
FROM public.talepler
WHERE kayit_tarihi IS NOT NULL
  AND kayit_tarihi::timestamptz < now() - interval '30 days'
ORDER BY kayit_tarihi
LIMIT 10;


-- Step 1: enable the pg_cron extension (once).
-- Recommended: Supabase Dashboard > Database > Extensions > enable "pg_cron".
-- Or with SQL:
CREATE EXTENSION IF NOT EXISTS pg_cron;


-- Step 2: schedule the daily cleanup.
-- Every day at 03:00 UTC (06:00 in Turkey), a low-traffic hour. pg_cron times are UTC.
SELECT cron.schedule(
  'kvkk-talep-temizlik-30gun',
  '0 3 * * *',
  $$DELETE FROM public.talepler
    WHERE kayit_tarihi IS NOT NULL
      AND kayit_tarihi::timestamptz < now() - interval '30 days'$$
);


-- ============================================================
-- Step 3: remove orphaned virtual valet vehicles
-- ============================================================
-- When a valet user is deleted, their virtual vehicle (araclar.tip='VALE') cannot be deleted
-- right away: completed tasks point to it through talepler.arac_id, and those tasks are report
-- history (see personel_sil in main.py). Once step 2 has deleted those tasks after 30 days,
-- the vehicle row is useless, but nothing else removes it. This job does.
-- Together the conditions mean "nothing refers to this row any more":
--   - tip = 'VALE'                 (virtual valet vehicles only; never real shuttle vehicles)
--   - no user assigned             (the valet user was deleted)
--   - no requests                  (removed by the 30-day cleanup), so the FK is safe
--   - no departure time templates  (defensive; valets have none, but there is an FK)
-- Runs at 03:15 UTC, after step 2 (03:00), so it cleans up the same night.
SELECT cron.schedule(
  'vale-oksuz-sanal-arac-temizlik',
  '15 3 * * *',
  $$DELETE FROM public.araclar a
    WHERE a.tip = 'VALE'
      AND NOT EXISTS (SELECT 1 FROM public.kullanicilar k WHERE k.arac_id = a.id)
      AND NOT EXISTS (SELECT 1 FROM public.talepler t WHERE t.arac_id = a.id)
      AND NOT EXISTS (SELECT 1 FROM public.arac_saat_sablonlari s WHERE s.arac_id = a.id)$$
);


-- ============================================================
-- Checks and monitoring (KVKK: deletions must be traceable)
-- ============================================================

-- Scheduled jobs
SELECT jobid, jobname, schedule, command, active FROM cron.job;

-- Run history (a record of every deletion run, which serves as the audit trail)
SELECT jobid, status, return_message, start_time, end_time
FROM cron.job_run_details
ORDER BY start_time DESC
LIMIT 20;


-- ============================================================
-- Rollback (remove the jobs if needed)
-- ============================================================
-- SELECT cron.unschedule('kvkk-talep-temizlik-30gun');
-- SELECT cron.unschedule('vale-oksuz-sanal-arac-temizlik');

-- To change the time or interval: unschedule first, then run step 2 again with the new
-- schedule.

-- ============================================================
-- Notes
-- - Rows with kayit_tarihi NULL are not deleted (safety for old or orphaned rows).
-- - On the Supabase free tier a project pauses after a week of inactivity, and pg_cron stops
--   with it. Keep the project active.
-- - Only talepler is cleaned here. The audit log (audit_log) has its own retention period
--   (2 years under the KVKK policy) and is not included.
-- - The consent log (riza_kayitlari) is separate too: kept for 2 years with its own pg_cron
--   job (db/kvkk_riza.sql, step 4). It holds no personal data and has no foreign key to
--   talepler on purpose: with a foreign key, the job above would either delete the consent
--   record as well or fail with an FK error.
-- ============================================================
