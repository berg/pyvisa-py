"""Tests for the VXI-11 protocol layer.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import pytest

from pyvisa import constants
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.protocols import vxi11
from pyvisa_py.tcpip import (
    VXI11_CREATE_LINK_ERRORS_TO_VISA,
    VXI11_ERRORS_TO_VISA,
    TCPIPInstrVxi11,
    vxi11_create_link_error_to_status,
    vxi11_error_to_status,
)


@pytest.mark.parametrize("error", sorted(VXI11_ERRORS_TO_VISA))
def test_defined_error_codes_keep_their_mapping(error):
    assert vxi11_error_to_status(error) is VXI11_ERRORS_TO_VISA[error]


@pytest.mark.parametrize("error", [2, 7, 21, 100, 200, 255])
def test_every_error_code_maps_to_a_status(error):
    """A code outside the table must not raise out of the VISA call.

    VXI-11 B.5.2 Table B.2 defines the codes in the table. Servers do return
    others, and the lookup used to be a subscript, so viReadSTB against such a
    server raised KeyError instead of reporting an error.
    """
    assert isinstance(vxi11_error_to_status(error), StatusCode)


def test_invalid_address_is_mapped():
    """VXI-11 B.5.2 Table B.2 defines error 21 as invalid address.

    Table B.4 lists it only for create_link, so it reports a device the
    server cannot address rather than a failure of an established link.
    """
    assert vxi11.ErrorCodes.invalid_address == 21
    assert vxi11_error_to_status(21) is StatusCode.error_resource_not_found


def test_error_codes_enum_covers_the_status_table():
    assert {int(code) for code in vxi11.ErrorCodes} == set(VXI11_ERRORS_TO_VISA)


@pytest.mark.parametrize("error", sorted(VXI11_CREATE_LINK_ERRORS_TO_VISA))
def test_create_link_errors_are_viopen_statuses(error):
    """viOpen may only report the statuses VPP-4.3 lists for it.

    VXI11_ERRORS_TO_VISA maps codes for operations on an established link, so
    it produces VI_ERROR_CONN_LOST and VI_ERROR_IO, neither of which viOpen is
    allowed to return.
    """
    allowed = {
        StatusCode.error_invalid_resource_name,
        StatusCode.error_resource_not_found,
        StatusCode.error_allocation,
        StatusCode.error_resource_busy,
        StatusCode.error_resource_locked,
        StatusCode.error_timeout,
    }
    assert vxi11_create_link_error_to_status(error) in allowed


def test_unknown_create_link_error_is_still_an_open_failure():
    assert vxi11_create_link_error_to_status(200) is StatusCode.error_resource_not_found


class FakeCoreChannel:
    """Records what device_lock was asked for, and answers as told."""

    def __init__(self, error=0):
        self.error = error
        self.calls = []

    def device_lock(self, link, flags, lock_timeout):
        self.calls.append({"flags": flags, "lock_timeout": lock_timeout})
        return self.error

    def device_unlock(self, link):
        return 0


def make_session(interface):
    """A VXI-11 session wired to a fake core channel, without opening one."""
    session = TCPIPInstrVxi11.__new__(TCPIPInstrVxi11)
    session.interface = interface
    session.link = 1
    session.lock_timeout = 10000
    session._lock_state = constants.VI_NO_LOCK
    return session


def test_lock_sends_the_callers_timeout():
    """VPP-4.3 3.6.2.1 defines timeout as how long the resource waits."""
    channel = FakeCoreChannel()
    session = make_session(channel)

    session.lock(constants.Lock.exclusive, 3000)

    assert channel.calls[0]["lock_timeout"] == 3000


def test_lock_sets_waitlock_so_the_instrument_waits():
    """VXI-11 B.5.3: without waitlock the server refuses instead of waiting."""
    channel = FakeCoreChannel()
    session = make_session(channel)

    session.lock(constants.Lock.exclusive, 3000)

    assert channel.calls[0]["flags"] & vxi11.OP_FLAG_WAIT_BLOCK


def test_immediate_lock_does_not_ask_the_instrument_to_wait():
    channel = FakeCoreChannel()
    session = make_session(channel)

    session.lock(constants.Lock.exclusive, constants.VI_TMO_IMMEDIATE)

    assert not channel.calls[0]["flags"] & vxi11.OP_FLAG_WAIT_BLOCK
    assert channel.calls[0]["lock_timeout"] == 0


def test_a_lock_that_waited_and_failed_is_a_timeout():
    """With waitlock set, error 11 means lock_timeout elapsed (RULE B.6.75)."""
    channel = FakeCoreChannel(error=vxi11.ErrorCodes.device_locked_by_another_link)
    session = make_session(channel)

    _key, status = session.lock(constants.Lock.exclusive, 3000)

    assert status is StatusCode.error_timeout


def test_an_immediate_lock_that_failed_is_reported_as_locked():
    channel = FakeCoreChannel(error=vxi11.ErrorCodes.device_locked_by_another_link)
    session = make_session(channel)

    _key, status = session.lock(constants.Lock.exclusive, constants.VI_TMO_IMMEDIATE)

    assert status is StatusCode.error_resource_locked


def test_lock_state_follows_lock_and_unlock():
    """VPP-4.3 RULE 3.6.2 requires every resource to support the attribute."""
    session = make_session(FakeCoreChannel())

    assert session.get_lock_state(ResourceAttribute.resource_lock_state) == (
        constants.VI_NO_LOCK,
        StatusCode.success,
    )

    session.lock(constants.Lock.exclusive, 3000)
    assert session._lock_state == constants.VI_EXCLUSIVE_LOCK

    session.unlock()
    assert session._lock_state == constants.VI_NO_LOCK


def test_a_failed_lock_leaves_the_state_alone():
    channel = FakeCoreChannel(error=vxi11.ErrorCodes.device_locked_by_another_link)
    session = make_session(channel)

    session.lock(constants.Lock.exclusive, 3000)

    assert session._lock_state == constants.VI_NO_LOCK


class FakeWriteChannel:
    """Records the flags each device_write carried."""

    def __init__(self):
        self.writes = []

    def device_write(self, link, io_timeout, lock_timeout, flags, data):
        self.writes.append({"flags": flags, "data": data})
        return 0, len(data)


def make_write_session(send_end, max_recv_size=1024):
    session = TCPIPInstrVxi11.__new__(TCPIPInstrVxi11)
    session.interface = FakeWriteChannel()
    session.link = 1
    session.lock_timeout = 10000
    session._io_timeout = 2000
    session.max_recv_size = max_recv_size
    session.attrs = {ResourceAttribute.send_end_enabled: send_end}
    return session


@pytest.mark.parametrize("send_end", [True, False])
def test_send_end_enabled_controls_the_end_flag(send_end):
    """VPP-4.3 RULE 5.1.12 requires TCPIP INSTR to support the attribute.

    VXI-11 B.5.3 carries it as the end flag, which asks for the last byte to
    go out with an END indicator.
    """
    session = make_write_session(send_end)

    session.write(b"*IDN?\n")

    carried = bool(session.interface.writes[-1]["flags"] & vxi11.OP_FLAG_END)
    assert carried is send_end


def test_only_the_last_block_carries_end():
    session = make_write_session(True, max_recv_size=8)

    session.write(b"0123456789abcdefghij")

    flags = [bool(w["flags"] & vxi11.OP_FLAG_END) for w in session.interface.writes]
    assert flags == [False] * (len(flags) - 1) + [True]


def test_no_block_carries_end_when_suppressed():
    session = make_write_session(False, max_recv_size=8)

    session.write(b"0123456789abcdefghij")

    assert not any(w["flags"] & vxi11.OP_FLAG_END for w in session.interface.writes)
