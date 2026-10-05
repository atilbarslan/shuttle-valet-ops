-- ============================================================
-- Per-user token cutoff, stored in the database
-- ============================================================
-- After a password reset, staff deletion or company deactivation, every token issued to the
-- user before that moment must be rejected. The cutoff is also cached in Redis (24 hours), but
-- Redis alone is not enough: after a Redis restart or flush the cutoff would be lost and an
-- old token could become valid again (driver tokens live 7 days). This table makes the cutoff
-- survive that, like firmalar.is_active does for deactivated companies.
--
-- It is a separate table rather than a column on kullanicilar because personel-sil deletes
-- the user row, which would delete the cutoff with it.
--
-- Run once in the Supabase SQL editor (step 1). The backend reads and writes it with the
-- service_role key (no RLS, like the other tables).
-- ============================================================


-- Step 1: create the table (safe to run again)
CREATE TABLE IF NOT EXISTS kullanici_token_iptal (
    kullanici_adi      text PRIMARY KEY,          -- the user (login key)
    gecersiz_before    timestamptz NOT NULL,      -- tokens issued before this moment are rejected
    sebep              text,                       -- SIFRE_SIFIRLA | PERSONEL_SILINDI | FIRMA_PASIF | FIRMA_SILINDI
    guncelleme_tarihi  timestamptz NOT NULL DEFAULT now()
);


-- Step 2 (optional): periodic cleanup
-- The longest token lifetime is 7 days (drivers), so a cutoff older than 8 days no longer
-- matters (every token it could reject has expired) and can be deleted. Run it daily with
-- pg_cron if wanted (see kvkk_temizlik.sql for the pattern):
--
--   DELETE FROM kullanici_token_iptal
--   WHERE guncelleme_tarihi < now() - interval '8 days';
--
-- Running it by hand now and then is also fine; the table stays small (one row per password
-- reset or staff deletion).


-- Check that the table exists:
-- SELECT * FROM kullanici_token_iptal ORDER BY guncelleme_tarihi DESC;
