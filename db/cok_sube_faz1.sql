-- ============================================================
-- Branches (subeler): schema foundation. Does not change behaviour.
-- ============================================================
-- Adds the columns and table for branch support. Every new field defaults to NULL, false or 0,
-- so companies without branches behave exactly as before.
-- Run in the Supabase SQL editor, in order: step 0, 1, 2, 3, then the checks.
-- Idempotent (IF NOT EXISTS), so running it again is safe.
-- ============================================================


-- Step 0: check the id column types (once) and choose the foreign key type accordingly.
-- If firmalar.id is 'uuid', step 2 works as written.
-- If it is 'text', make subeler.firma_id text in step 2 (see the commented line there).
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('firmalar','kullanicilar','araclar','talepler','guzergahlar','markalar')
  AND column_name = 'id'
ORDER BY table_name;


-- Step 1: firmalar gets the "has branches" switch and the quota fields (set by SUPERADMIN).
ALTER TABLE public.firmalar ADD COLUMN IF NOT EXISTS subeli      boolean DEFAULT false;
ALTER TABLE public.firmalar ADD COLUMN IF NOT EXISTS max_sube    int     DEFAULT 0;
ALTER TABLE public.firmalar ADD COLUMN IF NOT EXISTS toplam_kota int     DEFAULT 1;


-- Step 2: the subeler table.
-- firma_id must have the same type as firmalar.id (see step 0).
CREATE TABLE IF NOT EXISTS public.subeler (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  firma_id    uuid NOT NULL REFERENCES public.firmalar(id),
  -- firma_id text NOT NULL REFERENCES public.firmalar(id),  -- use this line instead if firmalar.id is text
  sube_adi    text NOT NULL,
  aktif_kota  int  NOT NULL DEFAULT 0,   -- handed out by the HQ admin; the sum must not exceed firmalar.toplam_kota
  aktif       boolean DEFAULT true,
  created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_subeler_firma ON public.subeler(firma_id);


-- Step 3: sube_id on the operational tables (NULL = headquarters, or a company without branches).
ALTER TABLE public.kullanicilar ADD COLUMN IF NOT EXISTS sube_id uuid REFERENCES public.subeler(id);
ALTER TABLE public.araclar      ADD COLUMN IF NOT EXISTS sube_id uuid REFERENCES public.subeler(id);
ALTER TABLE public.talepler     ADD COLUMN IF NOT EXISTS sube_id uuid REFERENCES public.subeler(id);
ALTER TABLE public.guzergahlar  ADD COLUMN IF NOT EXISTS sube_id uuid REFERENCES public.subeler(id);
ALTER TABLE public.markalar     ADD COLUMN IF NOT EXISTS sube_id uuid REFERENCES public.subeler(id);


-- ============================================================
-- Checks
-- ============================================================
-- Are the new columns there?
SELECT table_name, column_name FROM information_schema.columns
WHERE column_name IN ('subeli','max_sube','toplam_kota','sube_id')
ORDER BY table_name, column_name;

-- Right after migration subeler is empty and no company has branches (subeli=false).
SELECT count(*) AS sube_sayisi FROM public.subeler;
SELECT count(*) FILTER (WHERE subeli) AS subeli_firma, count(*) AS toplam_firma FROM public.firmalar;


-- ============================================================
-- Rollback (if needed, in this order)
-- ============================================================
-- ALTER TABLE public.kullanicilar DROP COLUMN IF EXISTS sube_id;
-- ALTER TABLE public.araclar      DROP COLUMN IF EXISTS sube_id;
-- ALTER TABLE public.talepler     DROP COLUMN IF EXISTS sube_id;
-- ALTER TABLE public.guzergahlar  DROP COLUMN IF EXISTS sube_id;
-- ALTER TABLE public.markalar     DROP COLUMN IF EXISTS sube_id;
-- DROP TABLE IF EXISTS public.subeler;
-- ALTER TABLE public.firmalar DROP COLUMN IF EXISTS subeli, DROP COLUMN IF EXISTS max_sube, DROP COLUMN IF EXISTS toplam_kota;


-- ============================================================
-- Notes
-- - Schema only. As long as sube_id is NULL and subeli is false, the existing flow
--   (firma_id + marka) works exactly as before.
-- - toplam_kota defaults to 1; the real quota is set from the SUPERADMIN panel.
-- - markalar.sube_id starts as NULL for existing brands.
-- ============================================================
