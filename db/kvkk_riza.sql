-- ============================================================
-- KVKK privacy notice and explicit consent: data model
-- ============================================================
-- Without this, a customer clicks the link, marks their location on the map and the location
-- is sent to the server with neither a privacy notice shown nor explicit consent asked for.
-- This file sets up the data model that connects the notice and consent texts to that flow.
--
-- Four parts:
--   Step 1 -> firmalar: the company details printed in the privacy notice
--   Step 2 -> riza_kayitlari: an auditable consent log (new table)
--   Step 3 -> talepler.riza_alindi / riza_reddedildi: quick flags for the application flow
--   Step 4 -> 2-year retention for the consent log (pg_cron)
--
-- Two different roles (do not mix them up):
--   - Passengers and valet customers: the data controller is the customer's company and the
--     platform is the processor. So the notice must show that company's legal name and
--     contact channel (step 1).
--   - Staff (admin, advisor, operations, driver, valet): the platform is the data controller.
--     The legal basis there is performance of a contract, so there is a privacy notice but
--     no explicit consent.
--
-- Run in the Supabase SQL editor in order. Everything is idempotent (safe to run again). The
-- backend reads and writes with the service_role key, without RLS (see token_iptal.sql).
-- ============================================================


-- ============================================================
-- Step 1: firmalar, the company's KVKK identity fields
-- ============================================================
-- The notice shown to passengers starts with "Data controller: <company's full legal name>".
-- If these fields are empty the notice has gaps and may not count as a valid notice. Collect
-- them from every company before it goes live.
ALTER TABLE public.firmalar
  ADD COLUMN IF NOT EXISTS kvkk_unvan           text,   -- full legal name as in the trade registry, e.g. "... Otomotiv Ticaret A.Ş."
  ADD COLUMN IF NOT EXISTS kvkk_basvuru_kanali  text,   -- channel for KVKK art. 11 requests: e-mail or registered e-mail (KEP)
  ADD COLUMN IF NOT EXISTS kvkk_adres           text;   -- optional: postal address for written requests


-- ------------------------------------------------------------
-- Step 1b: does this company require explicit consent for location?
-- ------------------------------------------------------------
-- Default TRUE = consent is asked for. If a company's legal review concludes that location
-- processing is covered by performance of the contract (KVKK art. 5/2-c), no code change is
-- needed: turn this flag off and the notice is still shown, without the consent checkbox.
-- Asking for consent that is not needed can itself undermine its "freely given" quality and
-- make it invalid. Per company, because different companies may get different legal advice.
ALTER TABLE public.firmalar
  ADD COLUMN IF NOT EXISTS kvkk_riza_gerekli boolean NOT NULL DEFAULT true;

-- Check
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'firmalar' AND column_name LIKE 'kvkk_%'
ORDER BY column_name;

-- Which companies are missing these fields? (their notice would have gaps)
SELECT id, firma_adi, kvkk_unvan, kvkk_basvuru_kanali
FROM public.firmalar
WHERE kvkk_unvan IS NULL OR btrim(kvkk_unvan) = ''
   OR kvkk_basvuru_kanali IS NULL OR btrim(kvkk_basvuru_kanali) = ''
ORDER BY firma_adi;


-- ============================================================
-- Step 2: riza_kayitlari, the consent log (new table)
-- ============================================================
-- The burden of proving explicit consent lies with the data controller. This table answers
-- "who agreed, when, to which version of which text, through which channel".
--
-- Design decision 1: no personal data in this table.
--   Name, phone and location are never written here. talepler rows are deleted entirely after
--   30 days by pg_cron (kvkk_temizlik.sql); the consent log is kept for 2 years, so personal
--   data in it would defeat that deletion. Once the request is deleted, the consent row is
--   effectively pseudonymised: it does not conflict with deletion and still serves as proof.
--   Exception: kullanici_adi, only for the PERSONEL_HESAP channel (that person is in
--   kullanicilar anyway).
--
-- Design decision 2: no foreign keys on talep_id and firma_id. On purpose.
--   - With ON DELETE CASCADE the 30-day deletion would also delete the consent record, ending
--     the 2-year retention.
--   - With RESTRICT / NO ACTION the pg_cron deletion would fail on the FK. That is much worse:
--     the KVKK deletion would silently stop and nobody would notice.
--   So this table is a log like audit_log: free-standing ids, indexes only.
--
-- Design decision 3: rows are never updated; new rows are appended.
--   A customer who first refuses and then agrees produces two rows; a withdrawal is a third.
--   Nothing is deleted or changed, so a chain like "refused, agreed 10 minutes later" stays
--   visible to an auditor. The current state is the latest row.
CREATE TABLE IF NOT EXISTS public.riza_kayitlari (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    firma_id          uuid,          -- on whose behalf (which data controller); no FK (see above)
    talep_id          uuid,          -- shuttle request / valet task; no FK, loses its parent after 30 days, which is expected
    kullanici_adi     text,          -- only for the PERSONEL_HESAP channel; NULL otherwise

    kanal             text NOT NULL, -- YOLCU_KONUM | VALE_KONUM | PERSONEL_HESAP
    islem             text NOT NULL, -- ONAY (agreed) | RED (refused) | GERI_CEKME (withdrawn)
    metin_kodu        text NOT NULL, -- EK6-1 (location consent) | EK5-1 (passenger notice) | EK5-2 (staff notice)
    metin_versiyonu   text NOT NULL, -- KVKK_METIN_VERSIYONU in main.py; bumped when a text changes, old rows keep the old version

    ip_adresi         text,          -- for proof (same as audit_log)
    user_agent        text,          -- browser/device summary (first 200 characters)
    kayit_tarihi      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT riza_kanal_gecerli CHECK (kanal IN ('YOLCU_KONUM', 'VALE_KONUM', 'PERSONEL_HESAP')),
    CONSTRAINT riza_islem_gecerli CHECK (islem IN ('ONAY', 'RED', 'GERI_CEKME'))
);

-- Indexes for "what is this request's consent state?" and "this company's consent records".
CREATE INDEX IF NOT EXISTS idx_riza_talep  ON public.riza_kayitlari (talep_id, kayit_tarihi DESC);
CREATE INDEX IF NOT EXISTS idx_riza_firma  ON public.riza_kayitlari (firma_id, kayit_tarihi DESC);
CREATE INDEX IF NOT EXISTS idx_riza_tarih  ON public.riza_kayitlari (kayit_tarihi);  -- for the retention job (step 4)

-- Check
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'riza_kayitlari'
ORDER BY ordinal_position;


-- ============================================================
-- Step 3: talepler.riza_alindi, a quick flag
-- ============================================================
-- The two endpoints that store a location (/konum-dogrula and /vale-konum-onay) should not have
-- to query the consent log on every request, so the request carries one flag. The log is the
-- proof; the flag is for flow control. Existing rows start as false, which does not affect old
-- or closed requests.
ALTER TABLE public.talepler
  ADD COLUMN IF NOT EXISTS riza_alindi boolean NOT NULL DEFAULT false;

-- A separate flag is needed because "refused" and "never opened the link" are not the same.
-- Both have riza_alindi = false, but they call for different actions:
--   - never opened the link -> keep waiting, send a reminder
--   - refused               -> no point waiting, call the customer
-- The panel draws its badge from this flag (firma-talepleri already returns select("*"), so no
-- extra query). If the customer later comes back and agrees, the flag goes back to false (the
-- log keeps both rows).
ALTER TABLE public.talepler
  ADD COLUMN IF NOT EXISTS riza_reddedildi boolean NOT NULL DEFAULT false;

-- Check
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'talepler' AND column_name IN ('riza_alindi', 'riza_reddedildi')
ORDER BY column_name;


-- ============================================================
-- Step 4: retention of the consent log (2 years), pg_cron
-- ============================================================
-- Kept for 2 years, the same as audit_log:
--   - 30 days would lose the proof as soon as the request is deleted;
--   - keeping it forever would breach the KVKK principle of limited retention.
-- With no personal data in the table, 2 years is proportionate.
--
-- Dry run first. Right after setup the table is empty, so 0 is normal.
SELECT count(*) AS silinecek_riza_kaydi
FROM public.riza_kayitlari
WHERE kayit_tarihi < now() - interval '2 years';

-- Schedule the job (same pattern as kvkk_temizlik.sql). 03:30 UTC = 06:30 in Turkey, after the
-- request deletion (03:00) and the orphaned valet vehicle cleanup (03:15).
SELECT cron.schedule(
  'kvkk-riza-kutugu-temizlik-2yil',
  '30 3 * * *',
  $$DELETE FROM public.riza_kayitlari
    WHERE kayit_tarihi < now() - interval '2 years'$$
);


-- ============================================================
-- Checks and monitoring
-- ============================================================
-- Scheduled cron jobs (at least: request deletion, orphaned valet vehicles, consent log)
SELECT jobid, jobname, schedule, active FROM cron.job ORDER BY jobname;

-- Latest consent records (to check the flow in production)
SELECT kayit_tarihi, kanal, islem, metin_kodu, metin_versiyonu, firma_id, talep_id
FROM public.riza_kayitlari
ORDER BY kayit_tarihi DESC
LIMIT 20;

-- Current consent state of each request = its latest row
-- SELECT DISTINCT ON (talep_id) talep_id, islem, kayit_tarihi
-- FROM public.riza_kayitlari
-- WHERE talep_id IS NOT NULL
-- ORDER BY talep_id, kayit_tarihi DESC;


-- ============================================================
-- Rollback (if needed)
-- ============================================================
-- SELECT cron.unschedule('kvkk-riza-kutugu-temizlik-2yil');
-- ALTER TABLE public.talepler DROP COLUMN IF EXISTS riza_alindi;
-- DROP TABLE IF EXISTS public.riza_kayitlari;
-- ALTER TABLE public.firmalar
--   DROP COLUMN IF EXISTS kvkk_unvan,
--   DROP COLUMN IF EXISTS kvkk_basvuru_kanali,
--   DROP COLUMN IF EXISTS kvkk_adres;


-- ============================================================
-- Notes
-- ============================================================
-- - This file is the data model only. While the table is empty, no existing behaviour changes.
-- - Supabase Data API grants: tables created after a certain date may need explicit GRANTs. The
--   backend uses service_role, so the risk is low; if PostgREST answers 401/403 anyway:
--     GRANT SELECT, INSERT ON public.riza_kayitlari TO service_role;
-- - The consent text itself is not stored here, only its code and version. The texts are
--   static pages under frontend/kvkk/. When a text changes, its version is bumped and old rows
--   keep pointing to the old version. Those pages are NOT included in this repository; add your
--   own frontend/kvkk/aydinlatma-yolcu.html and frontend/kvkk/aydinlatma-personel.html.
-- - A refusal is not final: talepler.durum is not touched, the link stays alive and the
--   customer can come back through the same link and agree (a new ONAY row is written).
-- - A withdrawal is different: konum_lat/lng are set to NULL and the link is destroyed (the
--   token is rewritten with a "BTT-" prefix). A valet task in progress is not cancelled
--   automatically, because the car may be with the valet; the panel shows a warning and a
--   person closes the task.
-- ============================================================
