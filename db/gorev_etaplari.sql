-- ============================================================
-- Task milestones (gorev_etaplari): promised time vs actual time
-- ============================================================
-- One table, two uses:
--
-- 1) Auditing. Requests only recorded when a task was opened (kayit_tarihi) and when it ended
--    (tamamlanma_tarihi), not the steps in between, so a manager could not see when the valet
--    set off, when they arrived, or whether they kept the promised time. Continuous GPS tracking
--    is not possible from a PWA, but auditing does not need it: timestamps are enough, and this
--    table holds them.
--
-- 2) A labelled dataset. hedef_varis is the predicted arrival (Mapbox, live traffic) and
--    gercek_varis the actual one, which allows studying how much of the variance comes from
--    the driver.
--
-- ============================================================
-- Design decisions: read before changing
-- ============================================================
--
-- 1) No coordinates are stored. This is the most important rule here.
--    The destination of a valet pickup leg is the customer's door. Storing it here would keep
--    a copy of the location forever after it is erased from talepler when the customer withdraws
--    consent, which would breach KVKK and defeat our own deletion design. What is needed is not
--    the location but measures derived from it: distance (km) and duration (min). That keeps the
--    table free of personal data by design, so it can be kept indefinitely.
--    Research that needs real route traces would need its own legal basis and a native app; do
--    not add coordinates here.
--
-- 2) No foreign key, for the same reason as riza_kayitlari: talepler rows are deleted after 30
--    days. With CASCADE the performance history would go too; with RESTRICT the deletion job
--    would fail (silently, nobody would notice). This table is a log and its ids are free-standing.
--
-- 3) vale_kullanici_adi is stored. Once the request is deleted, talep_id is a meaningless uuid,
--    and without the valet's name "this valet is consistently late" could not be established.
--    This is staff data and must be covered by the staff privacy notice
--    (frontend/kvkk/aydinlatma-personel.html; NOT included in this repository, add your own).
--
-- 4) No automatic deletion: analysis and performance comparisons rely on history, and the
--    table holds no personal data. A retention limit is a single pg_cron job (prepared,
--    commented out, at the bottom).
--
-- 5) basma_mesafe_m is a data-quality flag.
--    The "actual arrival" is really the moment the valet pressed the button. A valet who arrives
--    at 14:35 and presses at 14:41 adds six minutes of human delay to the label. That is not
--    ordinary noise: pressing habits differ per person, so it mixes with exactly the per-driver
--    difference we want to measure, and a model would learn who presses late rather than who
--    drives slowly. The remedy is the distance from the target when the button was pressed;
--    records pressed far away are excluded from analysis. Only the distance in metres is stored,
--    not coordinates (see decision 1).
-- ============================================================


-- Step 1: create the table (safe to run again)
CREATE TABLE IF NOT EXISTS public.gorev_etaplari (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    talep_id            uuid,          -- no FK (see above); loses its parent after 30 days, which is expected
    firma_id            uuid,
    sube_id             uuid,          -- per-branch reports in companies with branches
    arac_id             uuid,          -- the valet's virtual vehicle
    vale_kullanici_adi  text,          -- who did the task, kept after the request is deleted

    gorev_tipi          text NOT NULL, -- VALE_ALIM | VALE_TESLIM
    etap                text NOT NULL, -- see the CHECK below

    baslangic           timestamptz NOT NULL,   -- when the leg started (status change)
    hedef_varis         timestamptz,            -- frozen promise: arrival computed at the start, never updated
    hedef_dakika        integer,                -- the same promise in minutes (Mapbox estimate)
    gercek_varis        timestamptz,            -- written when the leg closes
    sapma_dk            integer,                -- actual - promised (+ late, - early), for convenience in reports

    mesafe_km           numeric(6,2),           -- route distance at the start (not coordinates)
    basma_mesafe_m      integer,                -- data-quality flag (decision 5)

    iptal_edildi        boolean NOT NULL DEFAULT false,  -- an open leg is closed this way when its task is cancelled
    kayit_tarihi        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT etap_gorev_tipi_gecerli CHECK (gorev_tipi IN ('VALE_ALIM', 'VALE_TESLIM')),
    CONSTRAINT etap_gecerli CHECK (etap IN (
        'MUSTERIYE_GIDIS',   -- pickup:   VALE_YOLDA -> ARAC_ALINDI         (has a promise)
        'SERVISE_DONUS',     -- pickup:   ARAC_ALINDI -> TAMAM_SERVIS       (has a promise)
        'SERVIS_HAZIRLIK',   -- delivery: KONUM_ALINDI_VALE -> ARAC_ALINDI  (no promise; time spent at the service center)
        'MUSTERIYE_TESLIM'   -- delivery: VALE_YOLDA -> TAMAM_MUSTERI       (has a promise)
    ))
);


-- Step 1b: did the valet report a position?
-- basma_mesafe_m can be NULL for two different reasons that could not be told apart:
--   (a) the valet gave no location permission or GPS failed  -> worth following up
--   (b) there was no target location (the customer withdrew consent and it was erased) -> normal
-- This column marks (a) explicitly: false = the valet's device reported no position when the
-- button was pressed. The admin panel shows it as a separate notice, because refusing location
-- permission should be visible. NULL = rows created before this column existed (unknown).
ALTER TABLE public.gorev_etaplari
  ADD COLUMN IF NOT EXISTS konum_bildirildi boolean;


-- Step 1c: how reliable was the position reading?
-- basma_mesafe_m alone is not evidence. On a button press the valet app does not read GPS; it
-- sends its cached reading, because navigation must open inside the click and cannot wait for
-- an await (otherwise the map opens inside the PWA). While navigation runs the app is in the
-- background and the position is not refreshed, so right after reopening the app the reading
-- can be minutes old and the distance kilometres off for a valet who is at the door. Without
-- these two fields a "pressed remotely" verdict would be unfair.
--   basma_konum_yasi_sn : how many seconds old the reading was (from the browser's pos.timestamp)
--   basma_dogruluk_m    : the reading's accuracy radius (pos.coords.accuracy)
-- The panel does not show a distance warning unless the reading was fresh and accurate; it
-- says "position old/uncertain" instead.
ALTER TABLE public.gorev_etaplari
  ADD COLUMN IF NOT EXISTS basma_konum_yasi_sn integer,
  ADD COLUMN IF NOT EXISTS basma_dogruluk_m    integer;

-- Step 1d: brand
-- talepler rows are deleted after 30 days while this table is kept. Without the brand here it
-- could never be recovered which brand a leg belonged to, and for a dealer selling several
-- brands the report would collapse into one meaningless average.
-- Stored as text, like talepler.marka: renaming a brand later does not rewrite history (same
-- idea as vale_kullanici_adi). NULL = rows created before this column existed.
ALTER TABLE public.gorev_etaplari
  ADD COLUMN IF NOT EXISTS marka text;

CREATE INDEX IF NOT EXISTS idx_etap_marka ON public.gorev_etaplari (firma_id, marka, baslangic DESC);


-- Step 2: indexes
-- Reports: a valet's latest legs, and a company's report for a period.
CREATE INDEX IF NOT EXISTS idx_etap_vale   ON public.gorev_etaplari (vale_kullanici_adi, baslangic DESC);
CREATE INDEX IF NOT EXISTS idx_etap_firma  ON public.gorev_etaplari (firma_id, baslangic DESC);
-- Panel: a task's timeline, and finding its open leg (gercek_varis IS NULL).
CREATE INDEX IF NOT EXISTS idx_etap_talep  ON public.gorev_etaplari (talep_id, baslangic);


-- Step 3: checks
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'gorev_etaplari'
ORDER BY ordinal_position;

SELECT indexname FROM pg_indexes WHERE tablename = 'gorev_etaplari' ORDER BY indexname;


-- ============================================================
-- Report queries
-- The punctuality report itself is GET /dakiklik-raporu in main.py, which feeds the
-- "punctuality" table on the admin panel. The aggregation is done in Python; no view or
-- function is needed in the database.
-- The thresholds (on time within 10 min, remote press beyond 300 m, minimum 5 legs) live only
-- in main.py (the DAKIKLIK_* constants). The queries below are for manual inspection. If you
-- change a threshold, review both.
-- Differences from the backend: the backend counts "on time" symmetrically (|sapma_dk| <= 10,
-- so very early also counts as off), and it does not count a remote press when the reading's
-- age or accuracy is unknown. The raw queries below do neither.
-- ============================================================

-- Punctuality per valet (only closed legs that had a promise)
-- SELECT vale_kullanici_adi,
--        count(*)                                              AS etap_sayisi,
--        round(avg(sapma_dk), 1)                               AS ort_sapma_dk,
--        count(*) FILTER (WHERE sapma_dk <= 10)                AS zamaninda,
--        round(100.0 * count(*) FILTER (WHERE sapma_dk <= 10) / count(*), 1) AS zamaninda_yuzde
-- FROM public.gorev_etaplari
-- WHERE gercek_varis IS NOT NULL AND hedef_varis IS NOT NULL AND NOT iptal_edildi
--   AND baslangic >= now() - interval '30 days'
-- GROUP BY vale_kullanici_adi
-- ORDER BY ort_sapma_dk DESC;

-- Labelled dataset: predicted vs actual.
-- Records with a large basma_mesafe_m should be excluded from analysis (decision 5).
-- SELECT vale_kullanici_adi, etap, mesafe_km, hedef_dakika,
--        EXTRACT(EPOCH FROM (gercek_varis - baslangic))/60 AS gercek_dakika,
--        sapma_dk, basma_mesafe_m,
--        EXTRACT(HOUR FROM baslangic AT TIME ZONE 'Europe/Istanbul') AS saat_dilimi,
--        EXTRACT(DOW  FROM baslangic AT TIME ZONE 'Europe/Istanbul') AS haftanin_gunu
-- FROM public.gorev_etaplari
-- WHERE gercek_varis IS NOT NULL AND hedef_dakika IS NOT NULL AND NOT iptal_edildi;

-- Legs that were never closed (data hygiene: find legs left open unexpectedly)
-- SELECT id, talep_id, vale_kullanici_adi, etap, baslangic
-- FROM public.gorev_etaplari
-- WHERE gercek_varis IS NULL AND NOT iptal_edildi AND baslangic < now() - interval '1 day'
-- ORDER BY baslangic;


-- ============================================================
-- Rollback
-- ============================================================
-- DROP TABLE IF EXISTS public.gorev_etaplari;

-- ============================================================
-- Optional retention limit. Off on purpose; to enable it:
-- SELECT cron.schedule('etap-temizlik', '45 3 * * *',
--   $$DELETE FROM public.gorev_etaplari WHERE baslangic < now() - interval '3 years'$$);
-- ============================================================
