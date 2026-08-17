# PR draft: H-0, HiSLIP connection loss

Not submitted. First in the HiSLIP stack. Small and independent of the rest,
so it can go out on its own.

**Title:** Report a lost HiSLIP connection as a VISA error

---

When a HiSLIP instrument closes the connection, `viRead` raises
`RuntimeError`, and `viWrite` had no error handling at all, so an `OSError`
from the socket escapes as well:

```
RuntimeError: Connection was dropped by server.
```

Neither is a VISA status, so a caller cannot treat an instrument that has
gone away as an I/O error. The VXI-11 session maps the same condition to
`VI_ERROR_CONN_LOST`.

The receive path now raises `HiSLIPConnectionLost`, and the session maps it
to `VI_ERROR_CONN_LOST` and any other socket failure to `VI_ERROR_IO`.

`HiSLIPConnectionLost` derives from `RuntimeError`, which is what this raised
before, so anything already catching that keeps working.

Verified against a Keysight M8132A as well as the unit tests.

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
