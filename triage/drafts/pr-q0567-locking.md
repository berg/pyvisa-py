# PR draft: Q-05, Q-06 and Q-07, VXI-11 locking

Not submitted. Three commits, in order: `3ed9bab`, `60c58bd`, `e857948`.

They are one PR because they are one bug report. Issue #583 shows all three:
the socket giving up before the lock timeout, the bare `Exception` that
follows, and lock handling that cannot wait. Splitting them would mean three
PRs that each fix a third of one traceback.

The reporter of #583 diagnosed the socket deadline themselves and said they
had no bandwidth for a PR. **Ask them before submitting**, and credit them in
the PR body if they are happy for this to go ahead.

**Title:** Fix VXI-11 lock handling

---

Closes #583. Three defects, all visible in that report.

### The socket gave up before the timeouts it had sent

The deadline came from a branch on the procedure number covering
`device_write`, `device_read`, `device_docmd` and the five generic
operations, and gave everything else a fixed four seconds. Two procedures
that carry a lock timeout fell into that default: `device_lock` and
`create_link`.

`viLock` told the instrument it would wait `lock_timeout`, ten seconds by
default, then gave up on the socket after five. The instrument answered a
perfectly ordinary error 11 into a socket the client had stopped reading, and
the caller saw a raw `socket.timeout`. The same deadline applied to
`create_link`, which is where the report sees `Exception: error creating
link: 3` arrive before the lock timeout had elapsed.

The deadline now comes from a table of where each procedure carries its
`io_timeout` and `lock_timeout`, taken from the argument structures in
VXI-11 B.6, plus a margin.

`lock_timeout` counts even when the request is not asking to wait. A
conformant server ignores locks unless told otherwise, by the waitlock flag
in B.5.3 or by `lockDevice` for `create_link` in RULE B.6.6, so on such a
server that budget is never spent. It is included because a server that
blocks anyway is the case that failed, which is what the report shows. The
instrument owns these timeouts and reports them as error codes; the socket
deadline is only a backstop for a link that has gone away.

### A refused link raised a bare Exception

`create_link` returning an error raised `Exception("error creating link: 3")`,
which pyvisa cannot map to a status, and left the TCP connection open.

It now closes the socket and raises `OpenError` with a status. The status
comes from a separate table: `VXI11_ERRORS_TO_VISA` maps codes for operations
on an established link, so it produces `VI_ERROR_CONN_LOST` and
`VI_ERROR_IO`, and VPP-4.3 lists neither among the statuses `viOpen` may
report. VXI-11 Table B.4 gives the six codes `create_link` can return and
each has a home in the `viOpen` list.

### viLock could not wait, and the lock state was missing

`device_lock` was sent with flags of zero. VXI-11 B.5.3 defines bit 0 as
waitlock: when it is clear, "the network instrument server sets the error
value to 11 and returns if the operation cannot be performed due to a lock
held by another link". A conformant instrument therefore refused at once and
the timeout could never elapse.

The lock timeout sent was the session's fixed `lock_timeout` rather than the
caller's `timeout` argument. VPP-4.3 3.6.2.1 defines that argument as the
period the resource waits before returning with an error, so it is the value
that belongs on the wire.

`VI_ATTR_RSRC_LOCK_STATE` was never registered and read
`VI_ERROR_NSUP_ATTR` while a lock was held. VPP-4.3 RULE 3.6.2 requires every
VISA resource to support it.

`VI_TMO_IMMEDIATE` now leaves waitlock clear, which is what asks the
instrument not to wait. Error 11 is `VI_ERROR_TMO` when we asked it to wait,
since RULE B.6.75 returns that code once `lock_timeout` elapses, and
`VI_ERROR_RSRC_LOCKED` when we did not.

`lock_type` is still not handled: requesting a shared lock still takes the
device lock and returns an empty key, as before. VXI-11 carries no shared
lock of its own, and VPP-4.3 section 3.6.1.6 says an implementation keeps
those locally, so making this conformant means lock bookkeeping in the
library rather than a change to this call. Raised separately.

- [x] Closes #583
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
