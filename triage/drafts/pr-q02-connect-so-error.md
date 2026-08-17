# PR draft: Q-02, refused connections

Not submitted. Cherry-pick `e37e4a7` onto a branch cut from `main`.

**Title:** Check SO_ERROR before reporting a connection established

---

Opening a VXI-11 resource whose instrument is switched off raises
`BrokenPipeError` rather than a `VisaIOError`:

```python
rm = pyvisa.ResourceManager("@py")
rm.open_resource("TCPIP::192.168.1.100::inst0::INSTR")  # nothing listening
```

```
BrokenPipeError: [Errno 32] Broken pipe
```

`rpc._connect` drives a non-blocking connect and waits for the result with
`select`. It treated readiness as proof that the connection was made, but a
refused connection also makes the socket ready, with the reason in
`SO_ERROR`. So `_connect` returned `True` immediately for a host with nothing
listening, and the failure only surfaced on the first `sendall`.

Checking `SO_ERROR` once the socket reports ready is enough.
`Vxi11CoreClient` already turns a `_connect` failure into `OpenError`, which
pyvisa reports as `VI_ERROR_RSRC_NFOUND`. VPP-4.3 gives that status for a
resource that is not present in the system.

Reproduced with:

```python
import socket
from pyvisa_py.protocols import rpc

probe = socket.socket()
probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]
probe.close()  # nothing listening now

sock = socket.socket()
print(rpc._connect(sock, "127.0.0.1", port, 2.0))  # True
print(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR))  # 61
```

- [ ] Closes #
- [x] Executed `ruff check`, `ruff format --check` and `mypy pyvisa_py` with no
      errors
- [x] The change is fully covered by automated unit tests
- [ ] Documented in docs/ as appropriate (no user facing doc for this)
- [x] Added an entry to the CHANGES file
