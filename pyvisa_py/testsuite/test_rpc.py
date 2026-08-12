"""Tests for the ONC RPC layer used by VXI-11.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket
import struct
import time

import pytest

from pyvisa_py.protocols import rpc


@pytest.fixture
def closed_port():
    """A port on the loopback interface with nothing listening on it."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture
def listening_port():
    """A port that accepts connections. Yields (port, server socket)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield server.getsockname()[1], server
    server.close()


def test_connect_reports_failure_for_a_refused_port(closed_port):
    """A refused connection must not be reported as a connected socket.

    connect_ex is non-blocking here, so the result arrives through select.
    A refused connection makes the socket ready with SO_ERROR set, which used
    to be taken for success. The first send then raised BrokenPipeError, and
    that left the VISA call as an OSError rather than a VISA status.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert rpc._connect(sock, "127.0.0.1", closed_port, 2.0) is False
    finally:
        sock.close()


def test_connect_reports_success_for_a_listening_port(listening_port):
    port, _server = listening_port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert rpc._connect(sock, "127.0.0.1", port, 2.0) is True
    finally:
        sock.close()


def test_client_raises_rpcerror_for_a_refused_port(closed_port):
    """RawTCPClient turns a failed connect into RPCError, which callers map."""
    with pytest.raises(rpc.RPCError):
        rpc.RawTCPClient("127.0.0.1", 0x0607AF, 1, closed_port, open_timeout=2000)


def test_recvrecord_reports_a_closed_connection(listening_port):
    """A peer that closes must be reported, not waited out.

    A closed socket stays ready for select, so a zero length recv used to be
    treated as "no data yet". The loop then ran without ever blocking, at full
    CPU, until the timeout expired, and reported a timeout rather than the
    lost connection.
    """
    port, server = listening_port
    client = socket.socket()
    client.connect(("127.0.0.1", port))
    conn, _ = server.accept()
    conn.close()

    start = time.monotonic()
    cpu_start = time.process_time()
    try:
        with pytest.raises(rpc.RPCConnectionLost):
            rpc._recvrecord(client, 10.0)
    finally:
        client.close()

    elapsed = time.monotonic() - start
    cpu = time.process_time() - cpu_start
    assert elapsed < 1.0, f"took {elapsed:.1f}s of a 10s timeout"
    assert cpu < 0.5, f"burned {cpu:.1f}s of CPU"


def test_recvrecord_still_returns_a_complete_record(listening_port):
    """The close is only an error when the record is incomplete."""
    port, server = listening_port
    client = socket.socket()
    client.connect(("127.0.0.1", port))
    conn, _ = server.accept()
    payload = b"\x00\x00\x00\x2a"
    conn.sendall(struct.pack(">I", 0x80000000 | len(payload)) + payload)
    conn.close()
    try:
        assert rpc._recvrecord(client, 10.0) == payload
    finally:
        client.close()
