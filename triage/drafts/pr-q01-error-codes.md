# PR draft: Q-01, unknown VXI-11 error codes

Not submitted. Cherry-pick `77c2987` onto a branch cut from `main`.
Title below, then the body under the line.

**Title:** Report unknown VXI-11 error codes instead of raising KeyError

---

`VXI11_ERRORS_TO_VISA` is subscripted directly by `read_stb`, `clear`,
`assert_trigger`, `lock` and `unlock`. When a server answers with an error
code that is not in the table, the lookup itself fails, so a `KeyError`
leaves the VISA call:

```
>>> instr.read_stb()
KeyError: 21
```

Lookups now go through `vxi11_error_to_status()`, which falls back to
`VI_ERROR_IO` for anything the table does not name.

Error 21 is added to the table and to `ErrorCodes`. VXI-11 B.5.2 Table B.2
defines it as *invalid address*, and Table B.4 lists it only for
`create_link`, so it reports a device the server cannot address rather than a
fault on an established link. `VI_ERROR_RSRC_NFOUND` is the closest VISA
status for that.

I hit this against a GPIB gateway that returns 21 for a secondary address it
cannot reach, but any code outside the table does the same thing.

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors. The checklist below still names black/isort/flake8; the repo
      moved to ruff in `.pre-commit-config.yaml`.
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
