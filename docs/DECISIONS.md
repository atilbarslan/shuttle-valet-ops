# Decisions and limitations

**English** · [Deutsch](DECISIONS.de.md)

Why some things were built the way they were, what the product deliberately does not do, and its known limitations.

Back to the [README](../README.md).

## Contents

1. [Why a single-file backend](#why-a-single-file-backend)
2. [Why no build step](#why-no-build-step)
3. [Honest measurement](#honest-measurement)
4. [What it does not do](#what-it-does-not-do)
5. [Known limitations](#known-limitations)

---

## Why a single-file backend

The backend lives in one `main.py`. For a single developer, searching and editing one file was
faster than moving between modules. The main benefit of splitting a file is that several people
can work on it at once; that need never came up.

---

## Why no build step

There is one HTML and one JavaScript file per page; no framework and no bundler. The file that
runs is the file that was written. The price is duplicated code
([Known limitations](#known-limitations)).

---

## Honest measurement

Each leg of a valet task is recorded separately (going to the customer and returning to the service
center for pickups; service preparation and going to the customer for deliveries). For legs with a
target, the target arrival time calculated when the leg starts and the actual arrival time are
stored; the difference makes up the punctuality report.

But "actual arrival" is really the moment the valet presses the button. If the valet arrives and
presses six minutes later, human delay gets into the measurement. Since pressing habits vary from
person to person, that delay mixes with exactly the per-valet difference we want to measure.

So the valet's distance to the target is also recorded at the moment of pressing. What is stored is
not a coordinate but a distance in metres. Since distance alone is not proof, the age and accuracy
of the location reading are stored with it. The report counts presses made from far away
separately; if the reading is stale or uncertain, the record is not counted against the person.

Whether the valet shared their location is flagged separately. An empty distance field means
nothing on its own; the target coordinate may also be missing, for example if the customer withdrew
location permission.

---

## What it does not do

- **No artificial intelligence.** Route ordering is done with geometric and heuristic rules; traffic
  data comes from an external API. Solver-based optimization (OR-Tools and the like) is not in this
  code base.
- **No continuous live GPS tracking.** A browser does not produce location continuously in the
  background; iOS suspends the app, Android freezes it, and a Service Worker has no access to
  location. The location is updated with each of the user's actions. The product does not keep a live
  trail; it produces timestamped records and arrival estimates. Continuous location would only be
  possible with a native app.
- **No SMS.** Links are shared by hand.
- **No native mobile app.** Everything is web and PWA.
- **The customer does not see a vehicle moving on a live map;** they see the arrival estimate.

---

## Known limitations

- **The backend is a single file.** It was not split because there was no second developer.
- **Test coverage is partial.** `tests/` has unit tests for the pure helper functions and rule
  tables, and endpoint tests for three critical flows: valet status transitions (including two
  simultaneous requests), company isolation and token revocation. The other endpoints were tested by
  hand on the running system.
- **The integration tests do not run against Postgres.** They call the real endpoints over HTTP, but
  the database is an in-memory client (`tests/fakes.py`) that imitates the behaviour the code relies
  on: filters, conditional updates and the rows a write returns. Constraints, triggers and SQL types
  are not exercised.
- **No CI/CD.** Deployment was manual: files were copied to the server and the service was restarted.
- **Three flows are not atomic** (see [Consistency](ARCHITECTURE.md#consistency-idempotency-instead-of-atomicity)).
  Full atomicity would need stored procedures.
- **There is duplicated code:** the valet cancellation rules are defined on the server and in the
  scripts of two panels, and the transition order again in the valet screen's buttons; the driver's
  route and the passenger order are computed in two separate places on the server; the time display
  function is identical in four files. Part of this is the price of having no build step. When
  changing any of them, all must change together; these places are marked in the code.
- **A `401` on the first send is not queued.** The queue rule says `401` stays in the queue, and that
  is what happens when the queue is flushed. But if the session has expired when an operation is first
  sent, both field screens redirect the user to the login screen and that operation may be lost.
- **One audit finding was deferred:** the route start endpoint changes state but is called with
  `GET`. Since authentication uses a `Bearer` token rather than a cookie, this does not create a
  CSRF hole; moving to `POST` was deferred because it requires changing client and server together.
  Coordinate validation was added.
- **The database client is synchronous.** Its queries now run in a thread pool, so they no longer
  block the event loop; with 50 requests in flight and a simulated 50 ms per query, throughput rose
  from about 3-5 to about 90-145 requests per second. A single request got about 5% slower because
  of the thread hop. The async Supabase client would remove that hop. Method, conditions and limits
  of the measurement: [docs/PERFORMANCE.md](PERFORMANCE.md).
- **Some check-then-act rules are not enforced by the database.** "One active task per valet" and
  the quota checks read first and write afterwards, so two requests arriving at the same moment can
  both pass. The critical status changes are protected by a condition in the update itself, but
  these are not; a partial unique index or a constraint would be the proper fix.
