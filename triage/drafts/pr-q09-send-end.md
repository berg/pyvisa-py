# PR draft: Q-09, VI_ATTR_SEND_END_EN

Not submitted. Cherry-pick `ecabf3f` onto a branch cut from `main`.
Independent of the locking PR.

**Title:** Honor VI_ATTR_SEND_END_EN on a VXI-11 write

---

A VXI-11 write always asks for the END indicator on the last block, whatever
`VI_ATTR_SEND_END_EN` is set to:

```python
instr.set_visa_attribute(ResourceAttribute.send_end_enabled, False)
instr.write_raw(b"CONF:VOLT:DC ")  # END still asserted
```

VPP-4.3 defines the attribute as whether to assert END during the transfer of
the last byte of the buffer, and RULE 5.1.12 requires a TCPIP INSTR
implementation to support it. VXI-11 B.5.3 carries it as bit 3 of the
operation flags, valid for `device_write`.

This matters for instruments that treat END as the end of a command. With the
attribute cleared a caller can build one command out of several writes, which
was not possible before.

The HiSLIP session honors the same attribute.

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
