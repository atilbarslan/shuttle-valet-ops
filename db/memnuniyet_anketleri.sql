-- ============================================================
-- Satisfaction survey: the customer's rating at the end of a valet task
-- ============================================================
-- gorev_etaplari measures whether the valet kept the promised time; this table records how the
-- customer felt. The two are meant to be read side by side:
--     average deviation +14 min, rating 2.4  -> the problem really is lateness
--     deviation fine, rating 2.4             -> the problem is not punctuality but conduct
-- That is why the admin panel also shows the rating as a column in the punctuality table.
--
-- ============================================================
-- Design decisions: read before changing
-- ============================================================
--
-- 1) Triggered only by TAMAM_MUSTERI (end of a delivery). Deliberately narrow.
--    When a pickup ends it is not known whether a delivery will follow (the advisor opens the
--    delivery hours or days later), so a survey at pickup risks two surveys for one customer.
--    The moment an advisor removes a car from the "waiting at service" list is not a trigger
--    either: that can happen at any time and the customer is not looking at the page, so the
--    survey would go unanswered. Customers who collect their car themselves get no survey.
--    Before widening the scope, measure how many pickups end without a delivery.
--
-- 2) No personal data, same rule as gorev_etaplari. The customer's name, phone and plate are
--    never written here. If identity is needed, look it up through talep_id in talepler; that
--    row is deleted after 30 days and this row is left without a parent. That is expected:
--    the survey itself is anonymous.
--
-- 3) No foreign key, like riza_kayitlari and gorev_etaplari. talepler rows are deleted after 30
--    days: with CASCADE the survey history would go too, with RESTRICT the nightly deletion
--    job would fail on the FK. This table is a log and its ids are free-standing.
--
-- 4) talep_id is UNIQUE: one submission per task, guaranteed by the database. An application
--    check is not enough, because anyone holding the link can submit and two tabs can submit
--    at the same moment. The constraint settles the race; the second insert fails and the
--    backend reports "already received".
--
-- 5) The free-text comment is erased separately. This is the most important rule here.
--    Ratings are numbers, not personal data, and can be kept indefinitely. A comment can contain
--    anything (names, phone numbers, "this staff member did this"), and keeping it forever would
--    break the "no personal data by design" promise of this table.
--    -> The yorum column is set to NULL after 90 days. The row is kept, so ratings survive.
--       The pg_cron job is at the bottom.
--    -> yorum_yazildi is set when the survey is submitted, so "how many people wrote a comment"
--       stays correct after the text itself is erased.
--
-- 6) vale_kullanici_adi is stored for the per-valet report. This is staff data: a customer
--    rating is linked to a named valet, so the staff privacy notice must say so (see
--    frontend/kvkk/aydinlatma-personel.html; NOT included in this repository, add your own).
--
-- 7) Only puan_genel is required; the other four may be NULL (the customer skipped the
--    question). Making all five required makes people give up or tap the same rating
--    everywhere. A missing answer is better than a fake one. NULL is not "0 points" and is
--    left out of averages.
--
-- 8) The survey window (ANKET_PENCERESI_SAAT, 24 hours) is enforced in the backend, not here.
--    After a delivery the customer's link stays alive that long for the survey, and during the
--    window it opens only the survey screen (no location, no plate, no personal data). It dies
--    when the window ends or the survey is submitted.
-- ============================================================


-- Step 1: create the table (safe to run again)
CREATE TABLE IF NOT EXISTS public.memnuniyet_anketleri (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    talep_id            uuid UNIQUE,   -- one submission per task (decision 4); no FK (decision 3)
    firma_id            uuid,
    sube_id             uuid,          -- per-branch reports in companies with branches
    marka               text,          -- per-brand reports (see step 1b)
    arac_id             uuid,          -- the valet's virtual vehicle (stable even if the username changes)
    vale_kullanici_adi  text,          -- who did the task, kept after the request is deleted (decision 6)

    gorev_tipi          text NOT NULL, -- only VALE_TESLIM today; kept so a wider scope needs no schema change

    -- Ratings 1-5. Only puan_genel is required (decision 7). NULL means skipped, not 0.
    puan_genel          smallint NOT NULL,
    puan_dakiklik       smallint,
    puan_ilgi           smallint,
    puan_arac_durumu    smallint,
    puan_bilgilendirme  smallint,

    -- Free text: at most 500 characters, set to NULL after 90 days (decision 5)
    yorum               text,
    yorum_yazildi       boolean NOT NULL DEFAULT false,  -- survives the erasure of the text

    kayit_tarihi        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT anket_gorev_tipi_gecerli CHECK (gorev_tipi IN ('VALE_ALIM', 'VALE_TESLIM')),
    CONSTRAINT anket_puan_genel_araligi CHECK (puan_genel BETWEEN 1 AND 5),
    CONSTRAINT anket_puan_dakiklik_araligi CHECK (puan_dakiklik IS NULL OR puan_dakiklik BETWEEN 1 AND 5),
    CONSTRAINT anket_puan_ilgi_araligi CHECK (puan_ilgi IS NULL OR puan_ilgi BETWEEN 1 AND 5),
    CONSTRAINT anket_puan_arac_araligi CHECK (puan_arac_durumu IS NULL OR puan_arac_durumu BETWEEN 1 AND 5),
    CONSTRAINT anket_puan_bilgi_araligi CHECK (puan_bilgilendirme IS NULL OR puan_bilgilendirme BETWEEN 1 AND 5),
    -- The backend checks the same 500-character limit (ANKET_YORUM_MAKS); this is the last line
    -- of defence. Keep the two numbers equal.
    CONSTRAINT anket_yorum_uzunlugu CHECK (yorum IS NULL OR char_length(yorum) <= 500)
);


-- Step 1b: the brand column, for tables created before it was part of the CREATE above.
-- The brand is copied here because talepler rows are deleted after 30 days while this table is
-- kept; without it, a dealer selling several brands could only see one average for all of them.
-- It is stored as text (like talepler.marka), so renaming a brand later does not rewrite
-- history. A column added later cannot recover the past, which is why it exists from the start.
ALTER TABLE public.memnuniyet_anketleri
  ADD COLUMN IF NOT EXISTS marka text;


-- Step 2: indexes
-- For the report queries: a company's surveys in a period, and one valet's ratings.
CREATE INDEX IF NOT EXISTS idx_anket_firma ON public.memnuniyet_anketleri (firma_id, kayit_tarihi DESC);
CREATE INDEX IF NOT EXISTS idx_anket_vale  ON public.memnuniyet_anketleri (vale_kullanici_adi, kayit_tarihi DESC);
CREATE INDEX IF NOT EXISTS idx_anket_marka ON public.memnuniyet_anketleri (firma_id, marka, kayit_tarihi DESC);
-- talep_id needs no separate index: the UNIQUE constraint already creates one.


-- Step 3: checks
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'memnuniyet_anketleri'
ORDER BY ordinal_position;

SELECT indexname FROM pg_indexes WHERE tablename = 'memnuniyet_anketleri' ORDER BY indexname;


-- ============================================================
-- Step 4: erase free-text comments after 90 days (KVKK, decision 5)
-- ============================================================
-- The row is not deleted; only yorum is set to NULL. Rating statistics remain, the raw text
-- that may contain personal data does not.
-- 03:30 UTC, so it does not overlap with kvkk-talep-temizlik-30gun (03:00).
SELECT cron.schedule(
  'anket-yorum-imha-90gun',
  '30 3 * * *',
  $$UPDATE public.memnuniyet_anketleri
       SET yorum = NULL
     WHERE yorum IS NOT NULL
       AND kayit_tarihi < now() - interval '90 days'$$
);

-- To list scheduled jobs:
-- SELECT jobid, schedule, jobname FROM cron.job ORDER BY jobname;


-- ============================================================
-- Report queries (for manual use; the panel runs equivalent queries)
-- ============================================================

-- Average ratings per valet (the "customer rating" column in the punctuality table)
-- SELECT vale_kullanici_adi,
--        count(*)                              AS anket_sayisi,
--        round(avg(puan_genel), 2)             AS ort_genel,
--        round(avg(puan_dakiklik), 2)          AS ort_dakiklik,
--        round(avg(puan_ilgi), 2)              AS ort_ilgi,
--        round(avg(puan_arac_durumu), 2)       AS ort_arac,
--        round(avg(puan_bilgilendirme), 2)     AS ort_bilgilendirme,
--        count(*) FILTER (WHERE yorum_yazildi) AS yorum_sayisi
-- FROM public.memnuniyet_anketleri
-- WHERE kayit_tarihi >= now() - interval '30 days'
-- GROUP BY vale_kullanici_adi
-- ORDER BY ort_genel ASC;   -- lowest rating first

-- Comment list (shown on the satisfaction tab)
-- SELECT kayit_tarihi, vale_kullanici_adi, gorev_tipi, puan_genel, yorum
-- FROM public.memnuniyet_anketleri
-- WHERE yorum IS NOT NULL
-- ORDER BY kayit_tarihi DESC;

-- Perceived vs measured punctuality: the customer's punctuality rating next to the measured
-- deviation. talep_id loses its parent after 30 days, so this is only meaningful for the last
-- 30 days.
-- SELECT a.vale_kullanici_adi,
--        round(avg(a.puan_dakiklik), 2)  AS musterinin_hissettigi,
--        round(avg(e.sapma_dk), 1)       AS olculen_sapma_dk,
--        count(*)                        AS eslesen_gorev
-- FROM public.memnuniyet_anketleri a
-- JOIN public.gorev_etaplari e ON e.talep_id = a.talep_id
-- WHERE a.puan_dakiklik IS NOT NULL
--   AND e.sapma_dk IS NOT NULL AND NOT e.iptal_edildi
-- GROUP BY a.vale_kullanici_adi;


-- ============================================================
-- Rollback
-- ============================================================
-- SELECT cron.unschedule('anket-yorum-imha-90gun');
-- DROP TABLE IF EXISTS public.memnuniyet_anketleri;
