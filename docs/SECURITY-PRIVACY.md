# Security and privacy

**English** · [Deutsch](SECURITY-PRIVACY.de.md)

Identity and company isolation, the security measures, what happens when a dependency fails, and how personal data is handled under KVKK.

Back to the [README](../README.md).

## Contents

1. [Identity, authorization and isolation](#identity-authorization-and-isolation)
2. [Security](#security)
3. [Failure handling: what fails open and what fails closed](#failure-handling-what-fails-open-and-what-fails-closed)
4. [KVKK: data hygiene from day one](#kvkk-data-hygiene-from-day-one)

---

## Identity, authorization and isolation

### Supabase, but not Supabase Auth

The database is Supabase; authentication is our own JWT system. The reason is a six-role
hierarchy and company isolation: every request has its role and its company checked separately,
and we needed full control over token revocation.

The backend connects to the database with the most privileged key; row level security does not
apply and all authorization checks live in application code. This is deliberate: the
authorization logic is not scattered across database policies but sits, readable, in the
application code.

### Role hierarchy

```
SUPER ADMIN  >  ADMIN  >  OPERATIONS  >  DESK AGENT  >  DRIVER = VALET
                                                        CUSTOMER (no login, token only)
```

A user can only manage someone below their own level. The one exception is delegation: an admin
can create another admin whose scope is narrower than their own (tied to a branch or a brand).
That is how headquarters creates a branch admin, and a branch admin creates a brand admin.

Driver and valet are field roles at the same level. Viewing, starting and finishing a route, stop
operations and passenger operations are only allowed on their own vehicle.

This rule was added later: an audit found that a valet could fetch the route list of the shuttle
vehicles in their own company and see passengers' personal data. Both field roles were tied to the
same "is this your vehicle" check. A later review found that the three passenger endpoints (picked
up, dropped off, no-show) still checked only the company, so a driver who knew a passenger's token
could mark a passenger of another vehicle; they now use the same check.

### Company isolation

For every role except super admin, a request is rejected if the company of the requested resource
does not match the company in the token. The check is not done in a single middleware but in each
endpoint, because the resources differ (request, vehicle, stop, route) and each is tied to its
company in a different way. The price is that it can be forgotten in one endpoint. That is exactly
what the audit found: a few endpoints were missing the check, and a user who knew the ID of a
record belonging to another company could read or change it (IDOR). All were brought into the same
pattern.

There are limits inside a company too. The desk agent and operations roles only see records of
their own brand and cannot create records for another brand. A user tied to a branch can only
change their own branch's records.

### Token lifetime and revocation

Tokens for the field roles (driver, valet) are valid for 7 days, all others for 24 hours. The field
lifetime is long because a driver being logged out mid-week stops the work, and operations waiting
in the offline queue can only be sent after logging in again.

Revocation covers three cases: revoking a single token (logout), invalidating all of a user's
tokens (password change, staff deletion) and deactivating a company. When a user or company is
deleted or deactivated, or a password is reset, their tokens are revoked too.

At first, revocation after a password change or staff deletion was kept only in Redis; if Redis
was flushed, revoked tokens would become valid again. Revocation is now also stored in the
database, and Redis is only an accelerator. Tokens of users in a deleted company also stayed valid
at first; a deleted company now counts as inactive.

Two places decoded the token by hand instead of going through the common check, and so skipped
part of it. The WebSocket checked only the company status, so a logged-out token could still
connect. A stop list endpoint that staff share with the customer page missed the per-user cutoff,
so a token issued before a password reset could still read it. Both now call the same check as
every other endpoint.

How these checks are made cheap on every request is covered under
[Performance](ARCHITECTURE.md#performance-cutting-redis-traffic).

### Identity and display name are separate fields

The username is the identity: login, token revocation and every technical match use it. The display
name is for display: the name shown on screens and in reports, which may contain Turkish letters
and spaces.

Turkish letters are deliberately banned from usernames. Uniqueness is checked after lowercasing,
and Turkish case conversion makes that unreliable: lowercasing `İ` gives `i` followed by a
combining dot, while `I` becomes `i`. The result can be two different-looking usernames colliding
into the same identity, or two identical-looking names counted as different identities, and
neither raises an error.

So usernames accept ASCII only and are converted automatically as they are typed into forms.
Display names are not unique either: one company can have two people called "Mehmet Yılmaz".

### Time

All timestamps are stored in UTC. The panels do not take the time zone from the browser; they
always display in `Europe/Istanbul`, so a panel opened abroad shows the same time.

---

## Security

The code base was audited end to end twice. Findings were closed in order of priority; two were
deferred with reasons (see [Known limitations](DECISIONS.md#known-limitations)). The decisions below come from
those audits or from a problem met in production. Company isolation and token revocation are covered
above, under [Identity, authorization and isolation](#identity-authorization-and-isolation).

### Unused endpoints were removed

Endpoints that no screen called were removed rather than fixed: four at first, and later a fifth,
left over from an abandoned continuous location tracking design (see
[What it does not do](DECISIONS.md#what-it-does-not-do)).

### Rules live on the server

A button disabled on screen can be bypassed with a direct API request. That is why rules such as "no
passenger operations before the route is started" are also enforced on the server. Password rules
(length, letters and digits) are also checked on the server; the check in the browser is only for
the user's convenience.

### Passwords

Passwords are stored with bcrypt; new users set their own password through an invitation link. The
link is generated by the server, not the interface: the interface only sends the username, and the
target address is read from the database. Invitation links are valid for 7 days.

There was a Turkish-specific trap here: bcrypt does not accept input longer than 72 bytes, and
Turkish letters take 2 bytes in UTF-8. A form limiting by character count could let through a
password over the byte limit, and the server returned `500`. The limit is now checked in bytes.

### Error messages

Internal error text from the database is not passed to the client. The user sees a generic message
and the details go to error monitoring. This rule was introduced after errors containing schema
details were seen going straight to the client from two endpoints.

Server-side messages go through Python's `logging` module, never `print`, and contain no personal
data: an email that was sent is logged as sent, without the address. Records at ERROR level also
reach error monitoring. Where an exception is deliberately ignored (a failed cache write, for
example), the code says why that is safe.

### Audit log

Critical operations such as deletion, password reset, adding staff, assigning vehicles and
deactivating a company are logged together with who did them; so are failed login attempts. The log
records events, not content: for a password reset the password itself, and for a contact details
update the new values, are not written; only which field was filled in is recorded.

The IP address in the audit and consent logs is the one the reverse proxy reports to the
application server. The `X-Forwarded-For` header is not read directly: its first entry is whatever
the client sent, which would let anyone write a fake address into the consent log.

### Other protections

Automatic API documentation is off in production. CORS only allows the application's own address.
If one of the critical environment variables is missing, the server stops at startup. Request bodies
are received through typed models; fields such as role, task type, vehicle type and status are
validated against fixed lists, and names and plates against allowed character lists. Coordinates go
through range checks; (0, 0) is also rejected for chosen locations, because in practice it means
"location could not be obtained". At most 100 passengers can be processed at a single stop.

The WebSocket has total and per-identity connection limits; a connection that sends no token is
closed within 10 seconds. A staff token goes through the same revocation checks as on the REST
endpoints, so a logged-out or revoked token, or a user of a deactivated company, cannot connect.

On every page the content security policy forbids inline scripts; the relaxation required by the map
library is only enabled on pages with a map. User content passes through DOMPurify before it is
written to the screen, and email bodies are HTML-escaped.

The login and password-setting endpoints are protected with per-minute request limits; so are the
main endpoints of the customer page. Tokens in customer links are 128-bit random values.

---

## Failure handling: what fails open and what fails closed

When Redis or the database cannot be reached, some checks let the request through (fail open)
and some stop it (fail closed). The choice depends on what is lost in each direction.

| Check | When its data cannot be read | Reason |
|---|---|---|
| Token revoked by logout | Open: the token is accepted | Field staff must not all be locked out by an outage |
| User cutoff after a password reset or deletion | Open: the token is accepted | Same |
| Cutoff time that cannot be parsed | Open: the token is accepted | Same |
| Logout | Open: answers "logged out" even if the revocation could not be stored | The app discards the token anyway |
| Company active or inactive | Redis down: the database is read. Database down too: the request fails with an error. A company that no longer exists counts as inactive | No outage reason to serve an unpaid company |
| Customer link lifetime with an unreadable date | Open: the link works | A wrong date must not lock the customer out of their own task |
| Consent record before a location is stored | Closed: no location is stored | The data controller must be able to prove consent |
| Whether a company requires consent | Closed: consent is asked for | The safe default is to ask |
| Invitation link with an unreadable date | Closed: the link is refused | A new invitation is cheap |
| Refusal and withdrawal of consent | Open: the refusal or the erasure goes ahead | A logging error must not block the customer's right |
| Audit log, punctuality milestones | Open: the action goes ahead | Records, not gates |

**Why the token checks fail open.** The people who would be locked out are drivers and valets in
the middle of a job; an infrastructure outage would stop every company's operation at once.
The exposure is limited: every token is signed and expires (24 hours for office roles, 7 days for
field roles), role and company checks do not depend on these stores, and the gap only exists while
the database is unreachable and Redis holds no cached answer for that token. The risk is that a
logged-out or stolen token keeps working during that time.

**Why consent fails closed.** Under KVKK the burden of proof is on the data controller. A location
stored without a consent record counts as processing without consent, so the worst allowed outcome
is "consent recorded, no location", never the other way round.

**In a domain such as banking** the token checks would fail closed: an unknown revocation status
would mean "deny", tokens would live minutes rather than days and be renewed with refresh tokens,
revocation would sit in a highly available store with alerting, and logout would report a failure
instead of claiming success. Locking users out during an outage is an accepted cost there, because
a misused session moves money.

---

## KVKK: data hygiene from day one

KVKK is Turkey's personal data protection law.

In a valet task, the customer's location is deleted the moment it stops being useful:

- For pickups, as soon as the valet takes the car from the customer. The location is no longer
  needed.
- For deliveries, as soon as the car is handed over to the customer.

If the customer withdraws location permission, the location is deleted at that moment. The location
of shuttle passengers, along with name, phone and plate, is kept because it is needed while the work
is ongoing and in reports; a database job that runs every day deletes all request records older than
30 days.

Customer links have a limited lifetime: 12 hours for shuttle (the trip ends the same day), 72 hours
for valet (the car may stay at the service center for days). This is only a safety net; the link is
mainly invalidated when the work is done. There are two exceptions. For a valet pickup, the link
lives until the delivery task is opened, so the customer can see that the car is at the service
center. After a valet delivery, the link lives for 24 more hours, but it only opens the satisfaction
survey and returns no name, phone, plate or location; the link dies when the survey is sent or the
time runs out.

Every endpoint a customer link can reach enforces the lifetime: the tracking page, the queue
position, the stop list, the WebSocket and both location submissions. At first only the tracking
page did, so an expired link could still read its place in the queue or even submit a location.
Refusing and withdrawing consent stay possible after expiry on purpose: they only reduce the data
held, and the right to withdraw consent should not depend on a link's age.

Consent records: every time the privacy notice or consent text changes, the text version is bumped,
and that version is stored with each consent. That way the question "which text did this person
consent to" can still be answered years later. Forgetting to bump the version makes old consents look
as if they were given to the new text.
