# Submission queue

Line numbers are against `network-robustness`. Status is one of `ready`
(fix understood, not written), `written` (on this branch, needs slicing),
`blocked`, `hold` (deliberately not submitting yet).

Waves exist because this is a volunteer-maintained project and a dozen
simultaneous PRs from a new contributor is a way to get none of them read.
Wave 1 is four small, independently obvious fixes. Nothing from wave 2 goes
out until wave 1 has been looked at.

Fixes land on `network-robustness` first so they can be exercised together,
then get cherry-picked onto a branch cut from `main` when they go out. Each
commit is therefore self-contained: fix, test and `CHANGES` entry together,
and clean on its own under `ruff`, `ruff format` and `mypy`.

Waves 1 to 3 are written, apart from `Q-08`, which is blocked. `drafts/`
holds a PR body for each. Against the out-of-tree conformance suite the
branch goes from 10/27 checks passing to 20/27, and the in-tree suite from
186 tests to 260.

| | Commit | State |
| --- | --- | --- |
| Q-01 | `77c2987` | written, draft ready |
| Q-02 | `e37e4a7` | written, draft ready |
| Q-03 | `1abe57d` | written, draft ready |
| Q-04 | `f4fa88b` | written, draft ready |

---

## Submission order

Everything sits on `fix-default-open-timeout`, which is PR #616, so that has
to land first. After it, the order below is the order to submit in, and the
branch is in that order.

    1  keepalive probe timers          all TCPIP, independent of everything
    2  VXI-11 unknown error codes
    3  VXI-11 SO_ERROR on connect
    4  VXI-11 dropped connection
    5  VXI-11 refused link             needs 2
    6  VXI-11 RPC socket deadline      needs 4
    7  VXI-11 viLock                   needs 2 and 6
    8  VXI-11 SEND_END_EN              needs 2
    9  HiSLIP connection lost
    10 HiSLIP parity                   needs 1 and 9

The two chains are independent. Cherry-picking each commit onto
`fix-default-open-timeout` and looking at what conflicts shows no VXI-11
change needing a HiSLIP one or the reverse; the only shared dependency is the
keepalive commit, which is first either way. Everything conflicts on
`CHANGES`, which is not a dependency: each PR writes its own entry.

HiSLIP goes last on purpose. Nine and ten are one 130-line change and one
2100-line change, and the big one is the one likely to sit. Putting it first
would queue seven small fixes behind a review the maintainer has to block out
time for, and would mean rebasing those seven onto it rather than rebasing it
once at the end. If the size prompts a discussion, that discussion does not
hold up anything else.

## Wave 1 — small, self-evident, no dependencies

### Q-01 Unknown VXI-11 error codes raise `KeyError`
**Status** written, `77c2987` · **Reproduced** `conformance.py::error_invalid_address`, `::error_unknown_code`

*Symptom.* `viReadSTB` against an instrument that answers error 21 raises
`KeyError: 21` out of pyvisa-py. Same for any code not in the table.

*Cause.* `tcpip.py:51` `VXI11_ERRORS_TO_VISA` is missing 21, and every user
subscripts it directly (`read_stb`, `clear`, `assert_trigger`, `lock`,
`unlock`).

*Rule.* VXI-11 B.5.2, Table B.2 lists 21 as *Invalid address*. It is a code
the spec defines and pyvisa-py does not know.

*Fix.* Add `21: StatusCode.error_resource_not_found`, and replace the subscripts
with `.get(error, StatusCode.error_io)` so a device-specific code degrades to
`VI_ERROR_IO` instead of a crash. `ErrorCodes` in `protocols/vxi11.py` gains
`invalid_address = 21` to match.

*Test.* `test_vxi11.py`. A fake core-channel server turned out to be
unnecessary: the mapping is testable directly, which keeps the commit small
and independent of the other three.

---

### Q-02 A connection refused by the kernel is reported as `BrokenPipeError`
**Status** written, `e37e4a7` · **Reproduced** `diag_connect_and_spin.py`, `conformance.py::error_dead_port`

*Symptom.* Opening a resource whose instrument is switched off raises
`BrokenPipeError: [Errno 32] Broken pipe` from `viOpen`, not a `VisaIOError`.

*Cause.* `rpc.py:436` treats select() readiness as proof of a connection.
A refused connect leaves the socket both readable and writable with `SO_ERROR`
set, so `_connect` returns `True` in 0.000 s and the failure surfaces on the
first `sendall`. Measured `SO_ERROR: 61 (Connection refused)`.

*Rule.* VPP-4.3 `viOpen` error table: `VI_ERROR_RSRC_NFOUND`, "resource not
present in the system".

*Fix.* After select() reports the socket ready, check
`getsockopt(SOL_SOCKET, SO_ERROR)` and return `False` when it is non-zero;
`Vxi11CoreClient` already turns that into `OpenError`.

*Test.* Bind a port, close it, open a resource against it, expect
`VisaIOError`.

*Note.* Same class as #593 (a raw exception reaching the user), different bug.
Cite as precedent, do not claim to close it.

---

### Q-03 A dropped connection spins at 100% CPU until the timeout expires
**Status** written, `1abe57d` · **Reproduced** `diag_connect_and_spin.py`, 6.01 s wall and 6.01 s CPU

*Symptom.* When an instrument closes the TCP connection mid-operation,
pyvisa-py burns a core for the whole VISA timeout and then reports
`VI_ERROR_IO`. It should report the connection loss immediately.

*Cause.* `rpc.py:360` treats a zero-length `recv` as "no data yet". A closed
socket selects readable forever, so the loop never blocks and never exits
early.

*Rule.* Judgment call, not a spec clause: VXI-11 says nothing about a peer
that disappears. The precedent is in-tree — HiSLIP maps a dropped connection
to `VI_ERROR_CONN_LOST` (`Q-14`), and VPP-4.3 defines that code for exactly
this.

*Fix.* Distinguish "select said readable and `recv` returned nothing" (peer
closed — raise a connection-lost error) from "select timed out" (keep
waiting). Map it to `VI_ERROR_CONN_LOST` at the session layer.

*Test.* Server that closes the socket instead of answering `device_read`;
assert the call returns in well under the timeout.

---

### Q-04 `VI_ATTR_TCPIP_KEEPALIVE` raises `AttributeError` on macOS
**Status** written, `f4fa88b`

*Symptom.* Enabling keepalive on any TCPIP resource raises `AttributeError`
on platforms without `socket.TCP_KEEPIDLE`, which includes macOS.

*Cause.* Three call sites set `TCP_KEEPIDLE`/`TCP_KEEPINTVL`/`TCP_KEEPCNT`
unconditionally.

*Fix.* `common.set_keepalive()`, setting each probe timer only when the
platform defines it. Already written; affects VXI-11, HiSLIP and
`TCPIP::SOCKET` alike.

*Test.* `test_common.py::test_set_keepalive`. The only
existing coverage was in `keysight_assisted_tests`, which needs an instrument
and is skipped in CI, which is why this never showed up there.

*Sliced.* The fix was inside the parity commit. It is now its own commit
sitting ahead of that work, so it cherry-picks on its own. The parity commit
keeps only its own use of `set_keepalive` for the HiSLIP sockets, which is
new code rather than a fix.

*Note.* The one behavioral difference the rebase showed between `main` and
this branch for VXI-11 — the conformance suite goes 9/27 to 10/27 on this
alone.

---

## Wave 2 — VXI-11 semantics

### Q-05 A refused link raises a bare `Exception` and leaks the socket
**Status** written, `3ed9bab` · **Reproduced** `conformance.py::error_create_link` · **Issue** #583

*Symptom.* `Exception: error creating link: 3` — the literal message the
reporter of #583 pasted. Not a `VisaIOError`, so pyvisa cannot map it, and the
TCP connection is never closed.

*Cause.* `tcpip.py:937`.

*Fix.* Raise `OpenError` after closing the interface, and map the VXI-11 error
through `VXI11_ERRORS_TO_VISA` (needs `Q-01`).

*Depends on* Q-01.

---

### Q-06 The RPC socket deadline is shorter than the timeouts it sends
**Status** written, `60c58bd` · **Reproduced** `conformance.py::lock_slow_grant` · **Issue** #583

*Symptom.* `viLock` on a busy instrument raises a raw `TimeoutError`
(`socket.timeout`) after ~5 s, while the request on the wire asked the
instrument to wait 10 s.

*Cause.* `rpc.py:466` maps procedures 11–17 and 22 to their `io_timeout`
argument and everything else to a fixed 4 s + 1 s. `DEVICE_LOCK` (18) is not
in the table. `create_link` (10) is not either, and it carries a
`lock_timeout` too.

*Rule.* VXI-11 B.6.10 / RULE B.6.75 — the server may block up to
`lock_timeout` before answering. The reporter of #583 reached the same fix:
socket deadline = `lock_timeout` + `io_timeout` + margin, per procedure.

*Fix.* Replace the proc-number branches with a table keyed on procedure giving
the argument positions of whichever timeouts that procedure carries, and sum
them.

*Note.* Worth coordinating with #583's reporter before writing, since they
diagnosed it and may want it.

---

### Q-07 `viLock` cannot wait, and does not report the lock state
**Status** written, `e857948` · **Reproduced** `conformance.py::lock_waitlock_flag`, `::lock_timeout_argument`, `::lock_state_attribute` · **Issue** #583

*Symptom.* Three related defects in `viLock`:

1. `tcpip.py:1336` sends `flags = 0`, so the waitlock bit is clear and a
   conformant instrument returns error 11 immediately. `viLock(timeout=30000)`
   never waits.
2. `tcpip.py:1338` sends the session's fixed `lock_timeout` (10 s), discarding
   the caller's `timeout` argument entirely.
3. `VI_ATTR_RSRC_LOCK_STATE` is never registered, so it reads
   `VI_ERROR_NSUP_ATTR` while a lock is held.

*Rule.* VXI-11 B.5.3: with waitlock clear the server "sets the error value to
11 and returns". VPP-4.3 3.6.2.1 defines `viLock`'s `timeout` as how long the
resource waits, and `VI_ERROR_TMO` as the result when it elapses. VPP-4.3
RULE 3.6.2: "Every VISA resource SHALL support the
`VI_ATTR_RSRC_LOCK_STATE` attribute."

*Fix.* Set waitlock, pass the caller's timeout as `lock_timeout`, map error 11
to `VI_ERROR_TMO` when we asked the instrument to wait and
`VI_ERROR_RSRC_LOCKED` when we did not, and track lock state the way
`TCPIPInstrHiSLIP` does (`Q-16`).

*Left out.* `lock_type`. A shared request still takes the device lock and
returns an empty key. VPP-4.3 RULE 3.6.3 requires both lock types and section
3.6.1.6 says VXI-11 keeps shared locks locally, so the fix is a lock registry
for the whole library, not a change to this call. See `locking.md` and `Q-21`.

*Depends on* Q-06 — without it the socket gives up before the lock does.

---

### Q-08 A read stopped by a termination character reports `VI_SUCCESS`
**Status** blocked on #612 · **Reproduced** `conformance.py::read_status_termchar`

*Symptom.* `VI_SUCCESS_TERM_CHAR` is never returned by a VXI-11 read, even
when the instrument reports `RX_CHR`.

*Cause.* `tcpip.py:1130` and the `while` exit above it both set a status
without consulting `reason`.

*Rule.* VPP-4.3 RULE 6.1.2 — no END, termination character read,
`VI_ATTR_TERMCHAR_EN` true, therefore `VI_SUCCESS_TERM_CHAR`. RULE 6.1.1 gives
END priority when both occurred; RULE 6.1.3 covers the count case.

*Status note.* PR #612 fixes the END half of this and conflates the termchar
half. Whatever is left becomes a small follow-up.

---

### Q-09 `VI_ATTR_SEND_END_EN` is ignored on write
**Status** written, `ecabf3f` · **Reproduced** `conformance.py::write_send_end`

*Symptom.* Setting `VI_ATTR_SEND_END_EN` false still asserts END on the last
byte.

*Cause.* `tcpip.py:1156` hard-codes the flags and never reads the attribute.

*Rule.* VPP-4.3 defines the attribute as "whether to assert END during the
transfer of the last byte of the buffer", and RULE 5.1.12 requires a TCPIP
INSTR implementation to support it. VXI-11 B.5.3 carries it as the `end` bit.

*Fix.* Set `OP_FLAG_END` on the final block only when the attribute is true.
The HiSLIP side of this is already done on this branch (`Q-16`).

---

## Wave 3 — HiSLIP, already written on this branch

The queue used to list seven logical changes here, `Q-11` to `Q-17`. That
grouping does not survive contact with the code: four of the seven live
inside one commit and depend on each other. What follows is the slicing the
code actually supports.

**Validated against hardware.** The whole branch passes the out-of-tree
HiSLIP suite against a Keysight M8132A: 100 checks, no failures, one skip
that a native HiSLIP instrument cannot exercise because it has no REN line.
Re-run after every change to this stack.

### The slices

| | Lines | What |
| --- | --- | --- |
| H-0 | 130 | A lost connection is reported as `VI_ERROR_CONN_LOST` rather than escaping as `RuntimeError` or `OSError` |
| H-1 | 2100 | Everything else: async channel demultiplexing, the missing VISA operations, attribute and status-code handling, server error reporting, IVI-6.1 conformance, and the concurrency and resynchronization fixes |

This started as five commits. Four of them were folded into H-1.

### Why they were folded

Checking each defect against `main` rather than against the commit messages,
most of what the later commits fixed was either in H-1's own new code or in
the code H-1 rewrites:

| Defect | Origin |
| --- | --- |
| The status code a read ends on | Pre-existing, but not extractable. `main` returns `VI_SUCCESS_TERM_CHAR` at every message end, breaking VPP-4.3 RULE 6.1.1, which ranks END above the termination character. Nothing observes it: `_read_raw` loops only on `VI_SUCCESS_MAX_CNT`, and `read_bytes` breaks on `VI_SUCCESS` and `VI_SUCCESS_TERM_CHAR` alike. The label only starts to matter once H-1 honors `VI_ATTR_TERMCHAR` and the two codes have to mean different things, so it belongs here. Tried as its own PR and rolled back: against `main` it fixes nothing a caller can see. |
| A lock granted late was stranded | H-1. `viLock` does not exist before it. |
| The RMT flag and message id race | Pre-existing, only reachable once H-1 gives a service request handler its own thread. |
| `viTerminate`'s fixed two second wait | Pre-existing, from PR #566, in the device clear handshake H-1 rewrites. |
| `Error` and `FatalError` discarded | Pre-existing. H-1 rewrites both channels that carry them. |
| IVI-6.1 deviations | Mixed. `AsyncStatusQuery`'s MessageID and the overlap flag are pre-existing; `AsyncInterrupted` completes H-1's own demultiplexer. |

The stranded lock is a regression H-1 introduced, and shipping a fix for it
as a follow-up would read as H-1 being unfinished. The rest are bugs in code
this change rewrites, so separating them is an artificial seam: a
pre-existing fault in the async channel is part of doing the async channel
properly.

The status code is the interesting one, because "pre-existing" does not imply
"extractable". A defect only makes a PR if a reviewer can be shown what it
costs, and this one costs nothing until the feature that gives the two codes
distinct meanings lands in the same change.

The cost is a 2100-line PR, which is a real cost. It is stated plainly in the
PR body along with where it can still be cut, rather than papered over.

### H-1 can still be cut

Into (demultiplexing) then (the VISA operations on top), since the operations
exist to use the demultiplexer. Not done pre-emptively: only 20 of the added
lines in `hislip.py` are a mechanical typing change, so there is no
preparatory refactor left to peel off, and the rest is interdependent
protocol logic verified against an instrument. Offer H-1 whole, say in the
body that it can be split on request, and do it with the instrument
available if the maintainer asks.

## Wave 4 — hold

### Q-18 VXI-11 has no abort channel, so `viTerminate` is unimplementable
**Status** hold · **Reproduced** `conformance.py::terminate_read`

`create_link` returns an abort port (`tcpip.py:932`) and pyvisa-py discards it
as `_abort_port`. VXI-11 B.6.16 / RULE B.6.106 defines `device_abort` on that
channel as the way to stop an in-progress core-channel RPC, and the core
channel is serialized by protocol, so there is no other way to interrupt a
blocked read. Real, but it is a new channel, a new client and a threading
story — too big to bundle with anything else, and worth proposing in an issue
before writing.

### Q-19 `_recvrecord` discards buffered bytes at the end of a record
**Status** hold · **Reproduced** `conformance.py::stale_reply_recovery`

`rpc.py` returns at the last fragment and drops whatever else it read. Harmless
when records arrive aligned — which is why `usable_after_failed_lock` passes —
but when a stale reply's payload is split across segments the client over-reads
into the record behind it, and the `xid < lastxid` recovery path then parses
from the middle of a record. Latent, only reachable after a client-side
timeout. Fix alongside `Q-06`, which makes those timeouts rare, or leave it.

### Q-21 VISA locking is unimplemented across pyvisa-py
**Status** hold, needs an upstream decision · See `locking.md`

VPP-4.3 RULE 3.6.3 requires every resource to support both lock types, and
RULE 3.6.2 requires `VI_ATTR_RSRC_LOCK_STATE`. Serial, USB, GPIB and
TCPIP::SOCKET refuse `viLock` outright; VXI-11 grants a device lock for either
kind and returns an empty key that cannot be used to join anything; no
transport keeps the lock counts of RULES 3.6.9-3.6.22 or gates operations as
section 3.6.1.3 requires.

Section 3.6.1.6 names VXI-11 and says shared locks are handled locally, so the
fix is a lock registry in the library with a small per-transport hook, not a
refusal. Findings, design and staging are in `locking.md`.

*Earlier reading was wrong.* This entry proposed answering a shared request
with `VI_ERROR_INV_LOCK_TYPE`. RULE 3.6.3 forbids it.

### Q-20 VXI-11 reads have no client-side deadline
**Status** hold — probably never

`tcpip.py` loops until END, termchar or count, trusting the instrument to
honor `io_timeout`. Against a server that never returns error 15, `viRead`
never returns. But VXI-11 RULE B.6.23 clause 1.d and OBSERVATION B.6.9
explicitly bless returning with `reason` unset and tell the client to keep
calling, and RULE B.6.27 requires the server to time out. So the loop is what
the spec asks for and the hang needs a non-conformant server. Not worth a PR
unless someone reports it. Same for a `maxRecvSize` of 0, which RULE B.6.3
forbids ("SHALL be at least 1024").

---

### Q-22 `VI_ATTR_SUPPRESS_END_EN` is declared but ignored on TCPIP INSTR
**Status** ready, not written

*Symptom.* Setting `VI_ATTR_SUPPRESS_END_EN` true on a HiSLIP session is
accepted and changes nothing: the read still stops on END.

*Cause.* `tcpip.py:250` declares the attribute for HiSLIP and no read path
consults it. `TCPIPInstrVicp` does the same at `tcpip.py:1561`, and the
VXI-11 session does not declare it at all.

*Rule.* VPP-4.3 RULE 5.1.12 lists `VI_ATTR_SUPPRESS_END_EN` among the
attributes a TCPIP INSTR implementation SHALL support, and RULE 6.1.1 gates
the END case on it: END only outranks the termination character and the byte
count while suppression is off.

*Fix.* Consult it where END is evaluated, so a suppressed END falls through
to RULE 6.1.2 and then 6.1.3. `sessions.py:872` already does exactly this for
the transports built on `Session._read`.

*Precedent.* Honored by ASRL (`serial.py:123`), USB (`usb.py:173`) and
`TCPIP::SOCKET` (`tcpip.py:1848`), which defaults it to true because a raw
socket has no END to suppress. Only the two TCPIP INSTR transports are
missing it.

*Ambiguity.* RULE 5.1.12 does not define what "support the attributes"
means, and declaring one so `viGetAttribute` answers is a defensible minimal
reading — which is exactly what HiSLIP does today. The strong reading, that
the attribute has its specified effect, is the only one that leaves RULE
6.1.1 and RULE 6.1.2 able to apply to a TCPIP INSTR resource at all, so that
is the reading to argue from. Worth stating plainly in the PR rather than
asserting non-conformance, since a maintainer may read 5.1.12 the other way.
Note also that USB gets an explicit RULE 5.1.36 forcing `VI_TRUE` support and
TCPIP gets no equivalent; the likely reason is that USBTMC advertises
TermChar support in a capability bit that an implementation could hide behind
and HiSLIP has no such bit, but that is inference, not spec text.

*Note.* Found while checking RULE 6.1.1 for the HiSLIP read status. Left out
of H-1 deliberately: it is a pre-existing gap shared with VXI-11, it is not
needed to make the termination character work, and H-1 is large enough. Worth
one small PR covering HiSLIP and VXI-11 together once the stack has moved.
