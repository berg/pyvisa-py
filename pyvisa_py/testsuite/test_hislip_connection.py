"""Tests for HiSLIP connection loss.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import socket

import pytest

from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.protocols import hislip
from pyvisa_py.tcpip import TCPIPInstrHiSLIP


def test_receive_reports_a_closed_connection():
    """A zero length recv means the peer closed the connection."""
    left, right = socket.socketpair()
    right.close()
    try:
        with pytest.raises(hislip.HiSLIPConnectionLost):
            hislip.receive_exact(left, 16)
    finally:
        left.close()


def test_connection_lost_is_still_a_runtime_error():
    """This used to raise RuntimeError, so that has to keep working."""
    assert issubclass(hislip.HiSLIPConnectionLost, RuntimeError)


class DroppedInterface:
    """An interface whose socket has gone away."""

    def receive(self, count):
        raise hislip.HiSLIPConnectionLost("Connection was dropped by server.")

    def send(self, data, end=True):
        raise hislip.HiSLIPConnectionLost("Connection was dropped by server.")


def make_session():
    session = TCPIPInstrHiSLIP.__new__(TCPIPInstrHiSLIP)
    session.interface = DroppedInterface()
    # Enough of a session for read and write, here and as they grow later.
    session.attrs = {
        ResourceAttribute.termchar: ord("\n"),
        ResourceAttribute.termchar_enabled: False,
        ResourceAttribute.send_end_enabled: True,
    }
    session._pending_buffer = bytearray()
    return session


def test_read_reports_connection_lost():
    data, status = make_session().read(100)

    assert data == b""
    assert status is StatusCode.error_connection_lost


def test_write_reports_connection_lost():
    count, status = make_session().write(b"*IDN?\n")

    assert count == 0
    assert status is StatusCode.error_connection_lost
