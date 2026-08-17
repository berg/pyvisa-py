# PR draft: Q-04, keepalive probe timers

Not submitted. Cherry-pick the keepalive commit onto a branch cut from
`main`. It now sits directly on `fix-default-open-timeout`, ahead of the
HiSLIP parity work, so it picks cleanly on its own.

**Title:** Set the TCP keepalive probe timers only where they exist

---

Enabling `VI_ATTR_TCPIP_KEEPALIVE` raises `AttributeError` on macOS:

```python
instr.set_visa_attribute(ResourceAttribute.tcpip_keepalive, True)
```

```
AttributeError: module 'socket' has no attribute 'TCP_KEEPIDLE'
```

`TCP_KEEPIDLE`, `TCP_KEEPINTVL` and `TCP_KEEPCNT` were set without checking
that the running interpreter has them. macOS has no `TCP_KEEPIDLE`; it calls
the same option `TCP_KEEPALIVE`. Both the VXI-11 session and
`TCPIPSocketSession` are affected.

The three call sites now share `common.set_keepalive`, which always sets
`SO_KEEPALIVE` and applies each probe timer only if it is available. So the
attribute does what it says on every platform, and still tunes the timers
where it can.

The only existing coverage is in `keysight_assisted_tests`, which needs an
instrument and is skipped in CI, which is why this did not show up there.
The new test uses a plain socket and runs everywhere.

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
