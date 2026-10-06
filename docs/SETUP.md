# Setup

**English** · [Deutsch](SETUP.de.md)

How to run the application, set up the database and run the tests.

Back to the [README](../README.md).

## Contents

1. [Setup](#setup)

---

## Setup

Requirements: Python 3.12, a Supabase (Postgres) project, Redis, a Mapbox account, and a Resend
account for sending email.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in the values
uvicorn main:app --reload
```

`.env` expects the JWT signing key, the database, Redis, map and email provider keys, and the
application's address (`BASE_URL`); each is explained in `.env.example`. If one is missing, the
server does not start. If `BASE_URL` starts with `https://`, the application runs in production mode
and does not allow local addresses through CORS; use `http://localhost:8000` locally.

The backend also serves the `frontend/` folder itself; the application opens at
`http://localhost:8000`. The frontend calls the API at the address the page was opened from, so no
separate setting is needed.

For the map pages, the browser-side Mapbox token sits as a placeholder in `frontend/js/admin.js` and
`frontend/js/musteri.js`, and the email sender address in `main.py`; replace them with your own
values. The Content-Security-Policy line at the top of each page in `frontend/` allows
`https://YOUR_DOMAIN` and `wss://YOUR_DOMAIN`; replace `YOUR_DOMAIN` with the domain the
application is served from.

Database: run `db/00_temel_sema.sql` once in the Supabase SQL editor; it creates every table and the
stop insert/delete functions. Then enable the `pg_cron` extension and run the scheduled clean-up jobs
in `db/kvkk_temizlik.sql` (two jobs), `db/kvkk_riza.sql` and `db/memnuniyet_anketleri.sql`. The other
files under `db/` are migrations for older databases and are already included in the base schema.

The privacy notice pages are not included in this repository, because they contained a real
company's legal details. The customer page links to `frontend/kvkk/aydinlatma-yolcu.html` and the
password page to `frontend/kvkk/aydinlatma-personel.html`; put your own notices at those paths.

In production, run the application behind a reverse proxy (such as nginx) on the same machine. The
proxy must send `X-Forwarded-For`; uvicorn trusts that header only from 127.0.0.1 by default
(`--forwarded-allow-ips`) and passes the real client address on to the application, where the rate
limits and the audit and consent logs use it.

Error monitoring is optional: it is enabled if `SENTRY_DSN` is set and disabled otherwise.

Tests: `pip install -r requirements-dev.txt`, then `pytest`. They use dummy settings and need no
database, Redis or external service: Supabase is replaced by an in-memory client and Redis by
fakeredis. Unit tests cover distance, input validation, the role hierarchy, customer link lifetimes,
the valet state machine and its cancellation rule, token lifetimes and password hashing. Endpoint
tests cover valet status transitions, company isolation and token revocation.
