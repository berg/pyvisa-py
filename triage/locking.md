# VISA locking across the transports

**Status** findings only, no code. Supersedes the reasoning in `Q-21`, which
was wrong: it proposed refusing a shared lock, and VPP-4.3 forbids that.

Read from `../vxi11-stress/docs/vpp43_2024-01-04.pdf` and
`../vxi11-stress/docs/VXI-11/vxi-11.pdf`, not from memory.

## What the spec requires

**RULE 3.6.3** — *Every VISA resource SHALL support both exclusive and shared
locks.* There is no qualification and no escape clause. `viLock` does list
`VI_ERROR_INV_LOCK_TYPE`, "the specified type of lock is not supported by this
resource", which sits oddly beside a SHALL that leaves nothing for it to
describe. Whatever that code is for, RULE 3.6.3 rules out using it as a
standing refusal of `VI_SHARED_LOCK`.

**RULE 3.6.2** — every resource SHALL support `VI_ATTR_RSRC_LOCK_STATE`.

**Locking is a VISA-level mechanism, not a wire mechanism.** Section 3.6.1.6
says the protocol need not carry it, and gives VXI-11 as its own example:

> when using the VXI-11 protocol, exclusive lock requests can be sent to a
> device, but shared locks can only be handled locally

RULE 3.6.8 then requires enforcement "for all sessions, processes, and
resources on the same computer". So the question is not whether VXI-11 can
express a shared lock — it cannot — but whether we keep the books locally.

**Section 3.6.1.2** defines sharing: a shared `viLock` returns an `accessKey`,
the holder passes it to other sessions, and they join by supplying it as
`requestedKey`. An exclusive lock deliberately yields no key, so it can never
be shared.

**Section 3.6.1.3** defines access privileges, and it is the expensive part.
With an exclusive lock held elsewhere, other sessions "cannot modify global
attributes or invoke operations, but can still get attributes". Conformance
therefore means *gating the operations*, not merely recording a state.

**The bookkeeping**, RULES 3.6.9 through 3.6.22:

| rule | requirement |
|---|---|
| 3.6.9 | per-session exclusive count and shared count |
| 3.6.10, 3.6.11 | each successful `viLock` increments the matching count |
| 3.6.12 | shared request from a session already holding exclusive → `VI_ERROR_RSRC_LOCKED` |
| 3.6.13, 3.6.14 | exclusive ignores `requestedKey` and returns a zero-length key |
| 3.6.15, 3.6.16 | VISA generates the key, "guaranteed unique from all other VISA hosts" |
| 3.6.17 | `requestedKey` of 256 characters or more → `VI_ERROR_INV_ACCESS_KEY` |
| 3.6.18 | a `requestedKey` under 256 characters is used as the key when the resource is free |
| 3.6.19, 3.6.20 | nested locking, and a re-lock returns the same key |
| 3.6.21 | `viClose` zeroes both counts before returning |

OBSERVATION 3.6.4 settles a detail we already rely on: a zero-length string
substitutes for `VI_NULL` in a `ViKeyId`.

## What VXI-11 actually offers

`device_lock` (B.6.10) is a single lock with no key and no nesting:

- RULE B.6.77 — locks are tied to the core connection, and a broken connection
  releases them.
- RULE B.6.72 — a link that already holds the lock gets error 11 if it asks
  again. There is no nesting on the wire.
- RULE B.6.74, RULE B.6.75 — `waitlock` decides whether the server blocks, and
  error 11 again once `lock_timeout` elapses.
- B.4.2 — the lock "guarantees exclusive access to the device associated with
  that link to that link only". One holder, no sharing.

So every VISA-level obligation above has to be met locally. That is exactly
what 3.6.1.6 anticipates.

## What HiSLIP offers

The opposite case: both lock kinds are on the wire, and they map onto VISA
almost exactly. IVI-6.1 section 6.5, Table 21 — a null `LockString` in an
`AsyncLock` request asks for an exclusive lock, a non-null one for a shared
lock — so the lock string *is* the VISA `accessKey`. Table 22 completes the
match: another client presenting the right key joins the shared lock, and one
presenting the wrong key fails after the lock timeout.

Section 2.6 states the model: with no exclusive lock outstanding multiple
clients may hold a shared lock, and the state in Table 22 is "the lock state
across all active HiSLIP sessions" — so the *server* arbitrates, across
connections and therefore across processes. Section 2.6 also releases a
client's locks when its connection closes, as VXI-11 RULE B.6.77 does.

Two things still do not come free. HiSLIP has no nesting either — Table 21
makes a redundant request from the holder error 3 — so RULE 3.6.19 remains
local bookkeeping. And section 2.7 notes that common implementations cap the
`AsyncLock` payload at 256 bytes, which happens to agree with RULE 3.6.17
rejecting a `requestedKey` of 256 characters or more.

## Where pyvisa-py stands

| transport | `viLock` | shared lock | `VI_ATTR_RSRC_LOCK_STATE` |
|---|---|---|---|
| serial, usb, gpib | base `Session.lock` → `VI_ERROR_NSUP_OPER` | none | not registered → `VI_ERROR_NSUP_ATTR` |
| VXI-11 | device lock for either kind | granted as exclusive, key always `""` | registered, but reports `VI_EXCLUSIVE_LOCK` for a shared lock |
| HiSLIP | both kinds, via lock strings | real key, correct state | registered |
| TCPIP::SOCKET | base → `VI_ERROR_NSUP_OPER` | none | static `VI_NO_LOCK` |

So the gap is library-wide, not a VXI-11 wart:

- **serial, usb, gpib and TCPIP::SOCKET violate RULE 3.6.3 and RULE 3.6.2
  outright.** They refuse `viLock` altogether. This is a plainer violation
  than anything VXI-11 does.
- **VXI-11 grants the wrong thing quietly.** A shared request takes the device
  lock, returns `VI_SUCCESS` and an empty key that cannot be used to join
  anything, and reports the state as exclusive. Breaks 3.6.15, 3.6.16, 3.6.18
  and 3.6.20, and misreports the attribute.
- **No transport keeps lock counts or supports nesting** (3.6.9–3.6.12, 3.6.19,
  3.6.21).
- **No transport gates operations on the lock state** (3.6.1.3). The only uses
  of `VI_ERROR_RSRC_LOCKED` in the tree are mappings of a device or driver
  error, never a decision this library makes.

## How we might do it

The shape that fixes every transport at once is to put the VISA mechanism in
one place and give each transport a small hook for whatever its protocol can
enforce.

**1. A lock registry, keyed by resource.** A `ResourceLock` per canonical
resource name holding the mode, the access key, per-session exclusive and
shared counts, and a condition variable for waiters. It lives on the
`PyVisaLibrary` instance, which every session already reaches, so all sessions
in the process to one resource share one object.

**2. `Session.lock`/`unlock` in the base class** implement 3.6.9–3.6.22
against the registry — key generation and validation, counts, nesting,
`VI_ERROR_RSRC_LOCKED` for 3.6.12, the timeout wait — and then call:

    _acquire_transport_lock(timeout)   # default: no-op, returns success
    _release_transport_lock()

called only on the transitions that matter: when the resource goes from
unlocked to locked, and when the last count drops to zero.

**3. The transports supply the hook.** VXI-11 uses `device_lock`/
`device_unlock`, HiSLIP `async_lock_request`/`async_lock_release`. Serial, USB,
GPIB and TCPIP::SOCKET inherit the no-op and become conformant at the VISA
level without touching their I/O paths.

**4. `VI_ATTR_RSRC_LOCK_STATE` reads from the registry**, which satisfies RULE
3.6.2 everywhere in one change instead of per transport.

**5. Release on close** (3.6.21) in `Session.close`.

**6. Operation gating** (3.6.1.3) as a guard in the dispatch rather than
sprinkled through each session, so one check covers every operation.

## What this cannot do

RULE 3.6.8 wants enforcement across processes on the same computer, and an
in-process registry does not give that. Much of it is covered anyway by the
instrument: every VXI-11 lock and both kinds of HiSLIP lock are arbitrated by
the server across connections. The residue is VXI-11 *shared* locks, which
3.6.1.6 puts in our hands, and every lock on the transports whose protocol
has no lock at all.

Closing that needs a filesystem lock per resource in a well-known directory,
with stale-holder cleanup — more machinery than the rest of this put together,
and a new failure mode of its own. Recommend accepting the gap, documenting
it, and saying so in the PR rather than leaving a reviewer to find it.

## How it would be staged

One idea, but too large for one reviewable diff. Each of these is testable
without hardware:

1. Registry, base-class `lock`/`unlock`, lock-state attribute, release on
   close. Serial, USB, GPIB and TCPIP::SOCKET gain conformant locking; no
   transport hook is used yet.
2. VXI-11 adopts the hook, replacing its present `lock`.
3. HiSLIP adopts the hook, keeping its lock strings as the transport lock.
4. Operation gating per 3.6.1.3 — separate, because it is the one step that
   makes previously-working code start failing.

## Open questions for upstream

1. Is RULE 3.6.8, cross-process enforcement, in scope, or a documented gap?
2. Should operation gating land at all? It is required by 3.6.1.3 and it is
   the only part that breaks working callers.
3. For transports whose protocol has no lock, is VISA-level-only locking the
   answer the maintainer wants, or does `VI_ERROR_NSUP_OPER` stay? RULE 3.6.3
   says the former.

## Correction

`Q-21` recorded that `VI_ERROR_INV_LOCK_TYPE` "is the honest answer and is
what the spec provides for it", and that a shared lock over VXI-11 "has
nothing to map onto". The first is contradicted by RULE 3.6.3, the second by
section 3.6.1.6, which names VXI-11 and says to handle shared locks locally.
The entry was written from the shape of the API rather than from the text.
