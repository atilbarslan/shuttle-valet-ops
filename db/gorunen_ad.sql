-- ============================================================
-- Display name: kullanicilar.gorunen_ad
-- ============================================================
-- Usernames are ASCII only and serve as the login identity, so they cannot hold a person's real
-- name with Turkish letters. The display name is what the panels show instead.
--
-- Two fields, two jobs:
--   - kullanici_adi: identity. ASCII, unique, used for login, token revocation and Redis keys.
--                    It never changes and is not meant for display (admin screens show it only
--                    as a technical field).
--   - gorunen_ad:    display. Turkish letters and spaces allowed. Never used for matching;
--                    only printed on screen.
--
-- People enter their display name when they set their password. Older rows without one (such
-- as the first superadmin) fall back to the username in the UI.
--
-- Only ADMIN and SUPERADMIN can edit display names; drivers and valets cannot rename
-- themselves, so the names customers see stay under management control.
-- ============================================================


-- Step 1: add the column (safe to run again)
ALTER TABLE public.kullanicilar
  ADD COLUMN IF NOT EXISTS gorunen_ad VARCHAR(60);


-- Step 2: check
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'kullanicilar' AND column_name IN ('kullanici_adi', 'gorunen_ad');


-- Step 3 (optional): list users without a display name.
-- They appear under their username until an admin fills it in.
SELECT kullanici_adi, rol, gorunen_ad
FROM public.kullanicilar
WHERE gorunen_ad IS NULL OR btrim(gorunen_ad) = ''
ORDER BY rol, kullanici_adi;

-- This column is not UNIQUE and must not be: two people in one company can have the same
-- name. Uniqueness only applies to kullanici_adi.
