# PR draft: Q-03, dropped connections

Not submitted. Cherry-pick `1abe57d` onto a branch cut from `main`.
Depends on nothing, but reads better after Q-02.

**Title:** Report a dropped VXI-11 connection instead of waiting out the timeout

---

When a VXI-11 instrument closes the connection during an operation,
pyvisa-py uses a full core until the VISA timeout expires and then reports
`VI_ERROR_IO`. On a 5 second timeout I measured 6.01 s wall clock and 6.01 s
of CPU, so the process is busy the whole time.

`_recvrecord` treats a zero length `recv` as "no data has arrived yet". That
is also what a closed connection looks like, and a closed socket stays ready
for `select`, so the loop never blocks and spins until the deadline.

A zero length `recv` now raises `RPCConnectionLost`. `read`, `write`,
`assert_trigger`, `clear`, `read_stb`, `lock` and `unlock` map it to
`VI_ERROR_CONN_LOST`, which is what `TCPIPInstrHiSLIP` already reports for a
dropped connection.

The `min_packages` path keeps its current behavior. It exists for servers
that end a short reply without marking the last fragment, and some of them
close the connection to signal the end, so a close after the requested number
of packages still returns the record rather than raising.

The test asserts both halves of the bug: that the call returns quickly, and
that it does not burn CPU while it waits.

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
