"""Tests for the status code a HiSLIP read reports.

The rules are VPP-4.3 6.1.1 to 6.1.3: an END indicator outranks a termination
character, which outranks the byte count. HiSLIP delivers END as a DataEND
message, and the interface reports it through _rmt.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.tcpip import TCPIPInstrHiSLIP


class FakeInterface:
    """Enough of a HiSLIP interface to answer reads from a canned message."""

    def __init__(self, data, rmt):
        self.data = data
        self._rmt = rmt

    def receive(self, count):
        out, self.data = self.data[:count], self.data[count:]
        return out


def make_session(data, rmt, term_char_en=False):
    session = TCPIPInstrHiSLIP.__new__(TCPIPInstrHiSLIP)
    session.interface = FakeInterface(data, rmt)
    session.attrs = {
        ResourceAttribute.termchar: ord("\n"),
        ResourceAttribute.termchar_enabled: term_char_en,
    }
    session._pending_buffer = bytearray()
    session._pending_end = False
    return session


def test_end_reports_success():
    """RULE 6.1.1. A read stopped by END is VI_SUCCESS."""
    data, status = make_session(b"hello\n", rmt=True).read(100)

    assert data == b"hello\n"
    assert status is StatusCode.success


def test_end_outranks_an_exactly_filled_buffer():
    """RULE 6.1.1 over 6.1.3. END wins when the two coincide."""
    data, status = make_session(b"hello", rmt=True).read(5)

    assert data == b"hello"
    assert status is StatusCode.success


def test_end_outranks_a_trailing_termination_character():
    """RULE 6.1.1 over 6.1.2.

    A 488.2 response ends with the termination character and END together,
    so this is the ordinary case once VI_ATTR_TERMCHAR_EN is on. It reported
    VI_SUCCESS_TERM_CHAR, which RULE 6.1.1 reserves to END.
    """
    data, status = make_session(b"hello\n", rmt=True, term_char_en=True).read(100)

    assert data == b"hello\n"
    assert status is StatusCode.success


def test_a_termination_character_mid_message_stops_the_read():
    """RULE 6.1.2. END is not this read's to report while bytes remain."""
    session = make_session(b"one\ntwo\n", rmt=True, term_char_en=True)

    data, status = session.read(100)
    assert data == b"one\n"
    assert status is StatusCode.success_termination_character_read

    # The held-back tail carries the END indicator.
    data, status = session.read(100)
    assert data == b"two\n"
    assert status is StatusCode.success


def test_a_full_buffer_without_end_reports_max_count():
    """RULE 6.1.3."""
    data, status = make_session(b"hello", rmt=False).read(5)

    assert data == b"hello"
    assert status is StatusCode.success_max_count_read


def test_a_short_read_without_end_reports_success():
    data, status = make_session(b"hi", rmt=False).read(100)

    assert data == b"hi"
    assert status is StatusCode.success
