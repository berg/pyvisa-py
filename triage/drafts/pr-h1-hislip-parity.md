# PR draft: H-1, HiSLIP parity

Not submitted. Second in the HiSLIP stack, after H-0, and the last of it.

Around 2100 lines. The last section of the body says why it is not split;
the fuller reasoning is in `../queue.md` and stays there.

**Title:** Bring HiSLIP sessions to parity with VXI-11 and GPIB

---

HiSLIP sessions were missing most of the VISA operations the VXI-11 and GPIB
sessions implement, even though the protocol layer already had working code
for nearly all of them sitting unused. Bringing them up also meant fixing
what that code got wrong, which a cross-check against IVI-6.1 revision 2.0
and a run against real instruments both turned up.

### The asynchronous channel is demultiplexed

A dedicated reader thread owns the async socket and separates service
requests and Interrupted messages from request responses.

Besides making SRQ possible, this fixes a latent bug. The server may send a
service request at any time, and one arriving during an unrelated exchange, a
status query or a lock or a device clear, was parsed as that exchange's
response and raised a protocol synchronization error. Service requests are
delivered on a second thread, so a handler is free to talk to the instrument;
reading the status byte from an SRQ handler would otherwise deadlock against
the reader thread.

### Operations that were missing

- SRQ events: `viEnableEvent`, `viWaitOnEvent`, `viInstallHandler` and
  friends now work for HiSLIP. The status byte travels with the service
  request, so no status query is issued from the handler.
- `viLock` and `viUnlock`, for exclusive and shared locks, with
  `VI_ATTR_RSRC_LOCK_STATE` reporting the session's lock state.
- `viAssertTrigger`, which previously raised an uncaught
  `NotImplementedError` instead of returning a status code.
- `viGpibControlREN`, on top of `AsyncRemoteLocalControl`.
- `viFlush`, for the read buffer.

### Attributes and status codes

`VI_ATTR_TERMCHAR`/`VI_ATTR_TERMCHAR_EN` are honored on read and
`VI_ATTR_SEND_END_EN` on write, as RULE 5.1.12 requires of a TCPIP INSTR
implementation. HiSLIP carries no termination character of its own — it
frames messages with `DataEND` and has no field or message for one — so the
session has to do it: a block is buffered and whatever follows the
termination character is held back for the next read. Nothing on the wire
changes, since the message arrives whole either way.

That makes the read status codes mean what VPP-4.3 says. A read `DataEND`
stopped is `VI_SUCCESS`, since RULE 6.1.1 ranks END above both the
termination character and the byte count; previously every message end
reported `VI_SUCCESS_TERM_CHAR`, though no termination character was honored
on read at all. `VI_SUCCESS_TERM_CHAR` now means RULE 6.1.2: a termination
character stopped the read and no END arrived with it. Since a 488.2 response
ends with the termination character and END together, that case shows up when
one message carries several lines, which is exactly what the held-back bytes
are for. This is a behavior change for code inspecting the status of a
HiSLIP read.

`open_timeout` is applied to the connection attempt. It was accepted and then
ignored, and scaled as though it were in seconds rather than milliseconds.

### Server errors are reported

HiSLIP has no dedicated "operation refused" message, so an `Error` is the
only way a server can report a failed transaction: a refused write, a query
the instrument never answered, I/O from a session that does not hold the
lock. It surfaced as an opaque `RuntimeError` from whichever response class
was reading, with the server's own explanation buried in the message or
discarded with the payload.

Errors now raise `HiSLIPServerError` carrying that explanation, mapped to
`VI_ERROR_IO`, or `VI_ERROR_CONN_LOST` for a `FatalError`. Control codes 128
to 255 are device defined, so they are not mapped individually and no longer
raise `KeyError`.

### Conformance, against IVI-6.1 revision 2.0

- `AsyncStatusQuery` carried the wrong MessageID. Section 6.14 requires the
  id of the most recently sent Data, DataEND or Trigger, and `0xffffff00-2`
  before there has been one; we sent the id we would use next. Section 6.14.3
  has the server report MAV false whenever that id does not match the last
  message it received, so `viReadSTB` never reported message-available
  against a conformant instrument. Confirmed on hardware: a Keysight M8132A
  now reports `0x10` with a reply outstanding where it reported `0x00`.
- `AsyncInterrupted` is unsolicited, like a service request, so queueing it
  as a reply handed it to whichever transaction ran next. It is dispatched
  now, and after an `Interrupted` the client waits for the matching
  `AsyncInterrupted` before sending again, per section 3.1.2 rule 4.
- An unrecognized message is answered with `Error` and a framing failure with
  `FatalError`, on both channels, per sections 6.3 and 6.2. Both were
  `RuntimeError` with an `XXX` comment saying what should happen instead.
- `InitializeResponse` read the whole control code as the overlap-mode flag.
  Only bit 0 is overlap; bits 1 and 2 announce encryption requirements
  (section 6.1).
- Buffered server messages are discarded when the client sends, per section
  3.1.2 rule 3.

### Concurrency and resynchronization

The RMT-delivered flag and the message id are claimed atomically. They are
shared by the synchronous send path and by `AsyncStatusQuery`, and were read
and then cleared with no synchronization, so a status query racing a write
could deliver the flag twice or lose it, and the instrument answered the next
command with `-410 Query INTERRUPTED`. About once per 20000 concurrent status
queries against an M8132A.

`device_clear` discards buffered data and resets all message tracking state
rather than only the message id, since a stale in-flight message could
desynchronize the following read. `hislip.DEVICE_CLEAR_SETTLE_TIME` can be
raised for instruments needing longer than 100 ms.

`viTerminate` no longer waits a fixed two seconds for an `Interrupted`
message that many instruments never send. The device clear handshake skips
over interleaved messages instead, taking terminate and resync from about two
seconds to under ten milliseconds.

Trigger, lock and remote/local map dropped connections to
`VI_ERROR_CONN_LOST` and other socket failures to `VI_ERROR_IO`, as read and
write already do.

### Testing

An in-process fake HiSLIP server covers the new paths without hardware. Also
run against a Keysight M8132A and an HP 34401A behind a GPIB gateway, with an
out-of-tree suite that checks responses against known-good bytes rather than
just that calls do not raise.

### On the size of this

It is large, and given the changes it is hard to split into a set of smaller
pull requests. The asynchronous channel is rewritten here, and everything
else either depends on that rewrite or corrects something inside it.

Happy to cut it differently if you see a seam I missed.

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [x] Documented in docs/ as appropriate
- [x] Added an entry to the CHANGES file
