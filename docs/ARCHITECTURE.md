# Architecture

**English** · [Deutsch](ARCHITECTURE.de.md)

How the system is put together: the code layout, the valet and shuttle flows, the field apps, consistency and performance.

Back to the [README](../README.md).

## Contents

1. [Project structure](#project-structure)
2. [Valet flow: state machine](#valet-flow-state-machine)
3. [Shuttle: route ordering and arrival estimates](#shuttle-route-ordering-and-arrival-estimates)
4. [Field conditions: PWA and offline queue](#field-conditions-pwa-and-offline-queue)
5. [Consistency: idempotency instead of atomicity](#consistency-idempotency-instead-of-atomicity)
6. [Performance: cutting Redis traffic](#performance-cutting-redis-traffic)

---

## Project structure

```
main.py                The whole backend: endpoints, authentication, helpers
requirements.in        Direct dependencies
requirements.txt       Lock file compiled from requirements.in (pip-tools)
requirements-dev.in    Development tools: pytest, ruff, fakeredis, anyio
requirements-dev.txt   Lock file compiled from requirements-dev.in
tests/                 Unit tests and endpoint tests (in-memory database, fakeredis)
frontend/
  <page>.html          One page per role
  js/<page>.js         That page's only script
  js/surum-kontrol.js  "New version" notice on the non-PWA panels
  config.js            Shared settings (API address, logout)
  sw.js                Service Worker; the single source of the version number
  manifest.json        PWA manifest
  libs/purify.min.js
  fonts/
db/                    Base schema, migrations and scheduled jobs (SQL)
scripts/load_test.py   Small load test (see docs/PERFORMANCE.md)
docs/                  Performance notes and screenshots
```

Pages are split by role: super admin, company admin, operations, desk agent, driver, valet,
customer. The driver and valet screens are PWAs; the others are plain web panels.

---

## Valet flow: state machine

A valet task has two types, and the order of states differs by type. In both types the task waits
in `BEKLIYOR_KONUM` until the customer marks their location through the link.

| Type | Order |
|---|---|
| Pickup (door to service center) | `KONUM_ALINDI_VALE` → `VALE_YOLDA` → `ARAC_ALINDI` → `TAMAM_SERVIS` |
| Delivery (service center to door) | `KONUM_ALINDI_VALE` → `ARAC_ALINDI` → `VALE_YOLDA` → `TAMAM_MUSTERI` |

`VALE_YOLDA` means the same in both types: the valet is on the way to the customer. The arrival
estimate the customer sees starts at exactly that moment.

At first there was one shared order. For deliveries this meant the arrival estimate started before
the car had even left the service center: the customer saw, say, "valet at your door in 12
minutes" while the car was still in the service car park. The meaning of each state was kept fixed
and the order was split by type.

The transitions are defined in a dictionary; a transition that is not allowed is rejected by the
server with `409`. The buttons on the valet screen follow the same order.

### Cancellation depends on the type

A task can only be cancelled before the car is picked up. The cancellable states were first a flat
list that included `VALE_YOLDA`. For pickups that was right: the car had not been picked up yet.
For deliveries, though, `VALE_YOLDA` meant the customer's car was in the valet's hands and on the
road. The task could still be cancelled, and since cancelling puts the car back on the "waiting at
the service center" list, the panel showed the car in the car park while it was on the road.

This was the same class of bug as the arrival estimate: the flat list did not see the reversed
order of deliveries. The cancellation rules were also turned into a per-type dictionary. The two
tables reference each other in the code, so that changing one prompts a look at the other.

### One task per valet at a time

The valet screen always shows a single task. While a second task could be assigned, that task never
appeared on the screen and waited in an invisible queue. Now, trying to open a new task for a valet
who already has an active one gets `409` from the server. The check has no date filter: an
unfinished task carried over from yesterday also counts as busy.

### Why a valet is a "vehicle"

The data model is vehicle-centric; on the shuttle side everything hangs off a vehicle. So whenever
a valet staff member is created, a virtual vehicle record is created behind the scenes. This is an
implementation detail and never appears on any screen; the valet is shown by name everywhere. When
a valet with history is deleted as staff, their virtual vehicle is not deleted, because past tasks
point to it; with no user left, it drops out of the operational lists.

---

## Shuttle: route ordering and arrival estimates

### The shape of a route

In stop mode, each route is classified when the admin finishes it. The distance of the middle stop
and of the last stop from the headquarters are compared:

- If the last stop is farther than the middle one, the route is linear (out and back).
- If the last stop is closer than the middle one, the route is a half moon (a loop).
- A route with two stops or fewer counts as linear.
- Older routes that were never classified keep working with the old behaviour; backward
  compatibility is preserved.

### Ordering

- **Linear:** first drop-offs from near to far, then pickups from far to near. The same physical
  stop is shown as two separate cards, one on the way out and one on the way back. With one card
  the driver would try to pick up on the way out a passenger meant for the way back.
- **Half moon:** all stops in their defined order, regardless of task type.
- **Door to door:** there are no stops; passengers are ordered by straight-line distance from the
  headquarters. First drop-offs from near to far, then pickups from far to near.

These rules are an own heuristic based on Haversine (great-circle) distances to the headquarters. No
solver or optimization algorithm is involved.

The route the driver sees and the order the passenger sees are computed with the same rules in two
separate places. In the code they reference each other; if one changes, the other must change too.

### Arrival estimate

From the vehicle's last known location, a cumulative travel time with traffic data is computed to
each upcoming stop, plus a fixed allowance per stop. The estimate is recomputed from scratch on
every request, not counted down from the previous value. Travel times between stops are cached for
15 minutes: keeping them longer would make the estimate stale as traffic changes, keeping them
shorter would raise the map API cost.

"Last known location" is a deliberate phrase. There is no continuous GPS tracking (see
[What it does not do](DECISIONS.md#what-it-does-not-do)); the location is updated with each of the driver's
actions.

---

## Field conditions: PWA and offline queue

The driver and valet screens are PWAs. A vehicle may be in an underground car park or out of
coverage, and the work goes on meanwhile.

### The queue

Write operations go through a single wrapper. If there is no connection or a network error occurs,
the operation is queued and sent when the connection comes back; the driver screen also flushes the
queue when the app opens. Operations that can only be done online (such as starting a route) are not
accepted at all while offline.

### The queue's one rule

When flushing the queue, both screens apply the same rule:

> **Only operations that could succeed later stay in the queue.**

- `401` stays: it will succeed once the user logs in again.
- `5xx` and network errors stay: they are temporary.
- Other `4xx` responses are dropped: they are a permanent refusal by the server. A cancelled task,
  a state that has already moved on, or a closed route will not become valid by retrying.

The two screens were wrong in opposite directions of this rule. One retried every `4xx` forever; an
update for a cancelled task got stuck in the queue. The other counted every response other than
`401` as "delivered" when flushing; on a temporary `500`, the queued operation was silently lost.
Flushing was brought into line with this rule on both.

The same rule applies to the first send of an operation: a temporary server error while online
queues passenger and stop operations. The driver screen has one deliberate exception: requests to
finish the route or change the vehicle status are not queued if their first send fails; the driver
is shown an error instead. A "finish route" request coming out of the queue late could close a new
route the driver had started in the meantime.

There was also an ordering problem with the last passenger: the passenger's operation and the
request to finish the route went out at the same time. If the route closed first, the passenger's
operation was rejected and the passenger was left unmarked. Now the passenger's operation is sent
and awaited first, and the route closes after it.

### Keeping state

The driver's route list is kept in the browser's persistent storage. It used to be in session
storage, and the list was lost when the app was closed and reopened or reloaded offline. The list
is deleted on logout and when the route ends, so no personal data is left on the device.

### Getting updates onto the device

The Service Worker serves from cache first; the app opens fast and works offline. The price is that
a new version does not reach the device on its own. The fix has three parts:

- The Service Worker file is served with headers telling the browser not to cache it; the browser
  (iOS included) asks the server for a new version every time.
- When a new version takes over, the page reloads itself.
- The version number is shown at the bottom of the screen; its source is a single constant in the
  Service Worker.

The non-PWA panels have no Service Worker, so a tab left open never picks up a new version. There, a
small script reads the version number periodically and, if it has changed, shows a "new version
released, reload" bar at the top. There is deliberately no automatic reload: someone filling in a
form should not lose what they typed.

---

## Consistency: idempotency instead of atomicity

Three flows that update more than one table (finishing a route, changing a vehicle's status,
completing a stop operation) do not run in a transaction. Supabase's client side does not offer
native transactions; that would have required a separate stored procedure for each flow.

Idempotency was provided instead: when the same request arrives twice, the second has no effect.
This is a weighed choice. Because of the offline queue, the same operation arriving twice is normal
in these flows; one stopping halfway is rare and can be fixed by hand. The frequent case was closed
first. The code states this limit explicitly in all three flows.

There is one exception: adding and deleting stops is done atomically by a function defined in the
database. There the situation was reversed: inserting a stop in the middle requires shifting the
sequence numbers of the stops after it, and two concurrent requests could corrupt that shift.
Inserting and shifting are done together in one database function, closing the race condition;
deletion also renumbers in the same place.

### Deletion and foreign keys

Anywhere a vehicle record is deleted, the task records tied to that vehicle must be considered:
either they are deleted first, or the operation is refused with a clear error message. This rule was
introduced after a `500` in production: deleting a valet with history ran into the database's foreign
key constraint.

---

## Performance: cutting Redis traffic

On every request, whether a token had been revoked was checked with three separate keys: the token
itself, all of the user's tokens, and the user's company. Since the screens poll the server at
intervals, the request count was high, and every request meant three Redis commands.

It was brought down in two steps:

1. The three keys are fetched with a single `MGET` command.
2. On top of that, an in-process memory cache: a token verified in the last 30 seconds never goes to
   Redis.

The second step raises a question: doesn't a 30-second cache delay revocation by 30 seconds? It does
not, because logout, password change and company deactivation clear the cache immediately. The cache
only speeds up the case where nothing has changed.

In a five-minute heavy test scenario, the number of Redis commands dropped from about 600 to 230.
