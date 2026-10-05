-- ============================================================
-- Completion time of a request: talepler.tamamlanma_tarihi
-- ============================================================
-- kayit_tarihi only records when a request was opened. This column records when it ended, so
-- the panels can show start and end times, the "completed today" list can go by the real end
-- (a task opened yesterday and finished today belongs there), and durations can be computed.
--
-- Written by: shuttle (picked up, dropped off, no-show), valet (reached the service center,
-- handed over to the customer) and cancelled valet tasks. All use this one column.
--
-- Time zone: TIMESTAMPTZ, stored in UTC like kayit_tarihi. Conversion to Istanbul time happens
-- only for display (saatTR() in the frontend), so the stored value is correct whatever the
-- server's or browser's time zone.
--
-- Existing rows stay NULL and show a dash. There is deliberately no backfill: the real end
-- time of past tasks is unknown, and copying kayit_tarihi would put made-up data in reports.
-- ============================================================


-- Step 1: add the column (safe to run again)
ALTER TABLE public.talepler
  ADD COLUMN IF NOT EXISTS tamamlanma_tarihi TIMESTAMPTZ;


-- Step 2: check that the column exists
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'talepler'
  AND column_name IN ('kayit_tarihi', 'tamamlanma_tarihi');


-- ============================================================
-- Monitoring (optional, for checking after a deploy)
-- ============================================================
-- Requests completed today with their durations, in Istanbul time:
-- SELECT musteri_ad, gorev_tipi, durum,
--        kayit_tarihi      AT TIME ZONE 'Europe/Istanbul' AS acilis,
--        tamamlanma_tarihi AT TIME ZONE 'Europe/Istanbul' AS bitis,
--        tamamlanma_tarihi - kayit_tarihi AS sure
-- FROM public.talepler
-- WHERE tamamlanma_tarihi >= (now() AT TIME ZONE 'Europe/Istanbul')::date
-- ORDER BY tamamlanma_tarihi DESC;

-- This column is not exempt from the KVKK cleanup: talepler rows are deleted entirely after
-- 30 days (db/kvkk_temizlik.sql, step 2), and this column goes with them.
