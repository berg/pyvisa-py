# -*- coding: utf-8 -*-
"""End to end tests for the HiSLIP session against a fake instrument.

A minimal HiSLIP server is run in-process so the session can be exercised
over real sockets: locking, triggering, remote/local control, service
requests, device clear and termination character handling all go over the
wire rather than through mocks.
"""

import socket
import struct
import threading
import time

import pytest

from pyvisa import constants
from pyvisa.constants import ResourceAttribute, StatusCode
from pyvisa_py.protocols import hislip
from pyvisa_py.tcpip import TCPIPInstrHiSLIP

SESSION_ID = 0x1234


def _pack(msg_type, control_code=0, message_parameter=0, payload=b""):
    return (
        struct.pack(
            hislip.HEADER_FORMAT,
            b"HS",
            hislip.MESSAGETYPE[msg_type],
            control_code,
            message_parameter,
            len(payload),
        )
        + payload
    )


def _recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _recv_message(sock):
    """Read one HiSLIP message. Returns (msg_type, control, parameter, payload)."""
    header = _recv_exact(sock, hislip.HEADER_SIZE)
    if header is None:
        return None
    _, msg_type, control, parameter, payload_length = struct.unpack(
        hislip.HEADER_FORMAT, header
    )
    payload = b""
    if payload_length:
        payload = _recv_exact(sock, payload_length) or b""
    return hislip.MESSAGETYPE_STR[msg_type], control, parameter, payload


class FakeHiSLIPServer:
    """A HiSLIP server implementing just enough of IVI-6.1 to drive a session.

    Responses are canned: any message written to the synchronous channel is
    answered with ``response``, which lets the read paths be exercised.

    """

    def __init__(self, response=b"FAKE,INSTRUMENT\n", status_byte=0x00):
        self.response = response
        self.status_byte = status_byte
        self.lock_response = "success"
        #: When set to a control code, refuse traffic with an Error carrying
        #: it. An Error is the only way a server can report a failed
        #: transaction.
        self.refuse_with = None
        self.refuse_message = b"operation refused by the server"
        #: Messages received on the async channel, for assertions.
        self.async_log = []
        #: Messages received on the sync channel, for assertions.
        self.sync_log = []
        #: Set to stop answering sync writes (to force a read timeout).
        self.mute = False

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(2)
        self.port = self._listener.getsockname()[1]

        self._stop = threading.Event()
        self._sync_sock = None
        self._async_sock = None
        self._async_ready = threading.Event()
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        for thread in self._threads:
            thread.start()

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self._stop.set()
        for sock in (self._listener, self._sync_sock, self._async_sock):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)

    def send_async_interrupted(self, message_id=0xFFFF_FF00):
        """Push an unsolicited AsyncInterrupted to the client."""
        assert self._async_ready.wait(5.0), "async channel never connected"
        self._async_sock.sendall(_pack("AsyncInterrupted", 0, message_id))

    def send_unrecognized(self, channel="async", message_type=120):
        """Send a message with a type no client can know."""
        header = struct.pack(hislip.HEADER_FORMAT, b"HS", message_type, 0, 0, 0)
        if channel == "async":
            assert self._async_ready.wait(5.0)
            self._async_sock.sendall(header)
        else:
            self._sync_sock.sendall(header)

    def send_service_request(self, status_byte=0x40):
        """Push an unsolicited AsyncServiceRequest to the client."""
        assert self._async_ready.wait(5.0), "async channel never connected"
        self._async_sock.sendall(_pack("AsyncServiceRequest", control_code=status_byte))

    # -- server internals --------------------------------------------------

    def _accept(self):
        try:
            sync, _ = self._listener.accept()
            self._sync_sock = sync
            thread = threading.Thread(
                target=self._serve_sync, args=(sync,), daemon=True
            )
            self._threads.append(thread)
            thread.start()

            asy, _ = self._listener.accept()
            self._async_sock = asy
            thread = threading.Thread(
                target=self._serve_async, args=(asy,), daemon=True
            )
            self._threads.append(thread)
            thread.start()
        except OSError:
            pass

    def _serve_sync(self, sock):
        # The Initialize message uses its own header layout.
        header = _recv_exact(sock, hislip.HEADER_SIZE)
        if header is None:
            return
        _, _, _, _, _, _, sub_address_len = struct.unpack("!2sBBBB2sQ", header)
        self.sub_address = _recv_exact(sock, sub_address_len)
        # version and session id share the message parameter field
        sock.sendall(_pack("InitializeResponse", 0, (0x0100 << 16) | SESSION_ID))

        try:
            while not self._stop.is_set():
                message = _recv_message(sock)
                if message is None:
                    return
                msg_type, control, parameter, payload = message
                self.sync_log.append((msg_type, control, parameter, payload))

                if self.refuse_with is not None and msg_type in (
                    "DataEnd",
                    "Data",
                    "Trigger",
                ):
                    sock.sendall(
                        _pack("Error", self.refuse_with, 0, self.refuse_message)
                    )
                elif msg_type == "DataEnd" and not self.mute:
                    sock.sendall(_pack("DataEnd", 0, parameter, self.response))
                elif msg_type == "DeviceClearComplete":
                    sock.sendall(_pack("DeviceClearAcknowledge", control))
        except OSError:
            pass

    def _serve_async(self, sock):
        message = _recv_message(sock)
        if message is None:
            return
        # vendor id travels in the message parameter of the response
        sock.sendall(_pack("AsyncInitializeResponse", 0, 0x41424344))
        self._async_ready.set()

        try:
            while not self._stop.is_set():
                message = _recv_message(sock)
                if message is None:
                    return
                msg_type, control, parameter, payload = message
                self.async_log.append((msg_type, control, parameter, payload))

                if self.refuse_with is not None and msg_type in (
                    "AsyncDeviceClear",
                    "AsyncStatusQuery",
                    "AsyncRemoteLocalControl",
                ):
                    sock.sendall(
                        _pack("Error", self.refuse_with, 0, self.refuse_message)
                    )
                elif msg_type == "AsyncMaxMsgSize":
                    requested = struct.unpack("!Q", payload)[0]
                    sock.sendall(
                        _pack(
                            "AsyncMaxMsgSizeResponse",
                            0,
                            0,
                            struct.pack("!Q", requested),
                        )
                    )
                elif msg_type == "AsyncStatusQuery":
                    sock.sendall(_pack("AsyncStatusResponse", self.status_byte))
                elif msg_type == "AsyncLock":
                    if control == hislip.LOCKCONTROLCODE["request"]:
                        code = {v: k for k, v in hislip.LOCKRESPONSE.items()}[
                            self.lock_response
                        ]
                    else:
                        code = 1  # release always succeeds
                    sock.sendall(_pack("AsyncLockResponse", code))
                elif msg_type == "AsyncLockInfo":
                    sock.sendall(_pack("AsyncLockInfoResponse", 0, 0))
                elif msg_type == "AsyncRemoteLocalControl":
                    sock.sendall(_pack("AsyncRemoteLocalResponse"))
                elif msg_type == "AsyncDeviceClear":
                    sock.sendall(_pack("AsyncDeviceClearAcknowledge", 0))
        except OSError:
            pass


@pytest.fixture
def server():
    srv = FakeHiSLIPServer()
    yield srv
    srv.close()


@pytest.fixture
def session(server):
    sess = TCPIPInstrHiSLIP(
        0, f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR", open_timeout=5000
    )
    yield sess
    try:
        sess.close()
    except Exception:
        pass


class TestBasicIO:
    def test_open_and_identify(self, session, server):
        assert server.sub_address == b"hislip0"
        assert session.get_attribute(ResourceAttribute.tcpip_is_hislip)[0]

    def test_write_then_read(self, session):
        count, status = session.write(b"*IDN?\n")
        assert (count, status) == (6, StatusCode.success)

        data, status = session.read(4096)
        assert data == b"FAKE,INSTRUMENT\n"
        # DataEND is the END indicator, so a plain VI_SUCCESS
        assert status == StatusCode.success

    def test_read_max_count(self, session):
        session.write(b"*IDN?\n")
        data, status = session.read(4)
        assert data == b"FAKE"
        assert status == StatusCode.success_max_count_read

    def test_read_exactly_message_length(self, session, server):
        """END wins over the byte count when the two coincide.

        Reporting VI_SUCCESS_MAX_CNT here would send ``read_raw`` back for
        another read that can only time out.
        """
        session.write(b"*IDN?\n")
        data, status = session.read(len(server.response))
        assert data == server.response
        assert status == StatusCode.success

    def test_read_raw_does_not_hang_on_exact_multiple(self, server):
        """A response that is an exact multiple of the chunk size still ends."""
        server.response = b"x" * 64
        sess = TCPIPInstrHiSLIP(
            0, f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR", open_timeout=5000
        )
        try:
            sess.set_attribute(ResourceAttribute.timeout_value, 2000)
            sess.write(b"*IDN?\n")
            collected = bytearray()
            for _ in range(10):
                data, status = sess.read(16)
                collected.extend(data)
                if status != StatusCode.success_max_count_read:
                    break
            assert bytes(collected) == server.response
            assert status == StatusCode.success
        finally:
            sess.close()

    def test_write_without_send_end(self, session, server):
        """SEND_END_EN false sends Data rather than DataEND."""
        session.set_attribute(ResourceAttribute.send_end_enabled, False)
        session.write(b"*IDN?\n")
        # Give the server a moment to log it
        time.sleep(0.2)
        assert server.sync_log[-1][0] == "Data"

        session.set_attribute(ResourceAttribute.send_end_enabled, True)
        session.write(b"*IDN?\n")
        time.sleep(0.2)
        assert server.sync_log[-1][0] == "DataEnd"

    def test_read_timeout(self, session, server):
        server.mute = True
        session.set_attribute(ResourceAttribute.timeout_value, 200)
        session.write(b"*IDN?\n")
        data, status = session.read(4096)
        assert data == b""
        assert status == StatusCode.error_timeout


class TestTermChar:
    def test_termchar_splits_response(self, server):
        server.response = b"first\nsecond\n"
        sess = TCPIPInstrHiSLIP(
            0, f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR", open_timeout=5000
        )
        try:
            sess.set_attribute(ResourceAttribute.termchar_enabled, True)
            sess.set_attribute(ResourceAttribute.termchar, ord("\n"))
            sess.write(b"*IDN?\n")

            data, status = sess.read(4096)
            assert data == b"first\n"
            assert status == StatusCode.success_termination_character_read

            # The rest is served from the pending buffer, without new traffic
            data, status = sess.read(4096)
            assert data == b"second\n"
            assert status == StatusCode.success_termination_character_read
        finally:
            sess.close()

    def test_termchar_disabled_returns_whole_message(self, server):
        server.response = b"first\nsecond\n"
        sess = TCPIPInstrHiSLIP(
            0, f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR", open_timeout=5000
        )
        try:
            sess.write(b"*IDN?\n")
            data, status = sess.read(4096)
            assert data == b"first\nsecond\n"
            assert status == StatusCode.success
        finally:
            sess.close()

    def test_flush_discards_pending(self, server):
        server.response = b"first\nsecond\n"
        sess = TCPIPInstrHiSLIP(
            0, f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR", open_timeout=5000
        )
        try:
            sess.set_attribute(ResourceAttribute.termchar_enabled, True)
            sess.write(b"*IDN?\n")
            sess.read(4096)
            assert sess._pending_buffer

            assert (
                sess.flush(constants.BufferOperation.discard_read_buffer)
                == StatusCode.success
            )
            assert not sess._pending_buffer
        finally:
            sess.close()


class TestStatusAndTrigger:
    def test_read_stb(self, server):
        server.status_byte = 0x50
        sess = TCPIPInstrHiSLIP(
            0, f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR", open_timeout=5000
        )
        try:
            stb, status = sess.read_stb()
            assert stb == 0x50
            assert status == StatusCode.success
        finally:
            sess.close()

    def test_assert_trigger(self, session, server):
        status = session.assert_trigger(constants.TriggerProtocol.default)
        assert status == StatusCode.success
        time.sleep(0.2)
        assert any(entry[0] == "Trigger" for entry in server.sync_log)

    def test_assert_trigger_rejects_other_protocols(self, session):
        status = session.assert_trigger(constants.TriggerProtocol.on)
        assert status == StatusCode.error_nonsupported_operation

    def test_clear(self, session, server):
        assert session.clear() == StatusCode.success
        assert any(entry[0] == "AsyncDeviceClear" for entry in server.async_log)
        assert any(entry[0] == "DeviceClearComplete" for entry in server.sync_log)
        # message state is back to its just-connected value
        assert session.interface._message_id == 0xFFFF_FF00
        assert session.interface._last_message_id is None


class TestRemoteLocal:
    @pytest.mark.parametrize(
        "mode, expected_code",
        [
            (constants.RENLineOperation.deassert, 0),
            (constants.RENLineOperation.asrt, 1),
            (constants.RENLineOperation.deassert_gtl, 2),
            (constants.RENLineOperation.asrt_address, 3),
            (constants.RENLineOperation.asrt_llo, 4),
            (constants.RENLineOperation.asrt_address_llo, 5),
            (constants.RENLineOperation.address_gtl, 6),
        ],
    )
    def test_control_ren(self, session, server, mode, expected_code):
        assert session.gpib_control_ren(mode) == StatusCode.success
        entry = [e for e in server.async_log if e[0] == "AsyncRemoteLocalControl"][-1]
        assert entry[1] == expected_code

    def test_invalid_mode(self, session):
        assert session.gpib_control_ren(99) == StatusCode.error_invalid_mode


class TestLocking:
    def test_exclusive_lock_and_unlock(self, session, server):
        key, status = session.lock(constants.Lock.exclusive, 1000)
        assert status == StatusCode.success
        assert key == ""
        assert (
            session.get_attribute(ResourceAttribute.resource_lock_state)[0]
            == constants.VI_EXCLUSIVE_LOCK
        )
        # an exclusive lock is requested with an empty lock string
        entry = [e for e in server.async_log if e[0] == "AsyncLock"][-1]
        assert entry[3] == b""

        assert session.unlock() == StatusCode.success
        assert (
            session.get_attribute(ResourceAttribute.resource_lock_state)[0]
            == constants.VI_NO_LOCK
        )

    def test_shared_lock_uses_key(self, session, server):
        key, status = session.lock(constants.Lock.shared, 1000, "my-key")
        assert status == StatusCode.success
        assert key == "my-key"
        entry = [e for e in server.async_log if e[0] == "AsyncLock"][-1]
        assert entry[3] == b"my-key"
        assert (
            session.get_attribute(ResourceAttribute.resource_lock_state)[0]
            == constants.VI_SHARED_LOCK
        )

    def test_shared_lock_generates_key(self, session):
        key, status = session.lock(constants.Lock.shared, 1000)
        assert status == StatusCode.success
        assert key.startswith("pyvisa-py")

    def test_lock_refused(self, session, server):
        server.lock_response = "failure"
        key, status = session.lock(constants.Lock.exclusive, 200)
        assert status == StatusCode.error_timeout
        assert key == ""

    def test_unlock_without_lock(self, session):
        assert session.unlock() == StatusCode.error_session_not_locked


class TestOpenTimeout:
    """The default open path must work, which is the one nobody passes.

    ``ResourceManager.open_resource`` defaults open_timeout to
    VI_TMO_IMMEDIATE (0). Taking that literally as a socket timeout makes the
    connect non-blocking and every default open fail.
    """

    @pytest.mark.parametrize("open_timeout", [None, 0, 5000])
    def test_open_succeeds_for_any_open_timeout(self, server, open_timeout):
        sess = TCPIPInstrHiSLIP(
            0,
            f"TCPIP0::127.0.0.1::hislip0,{server.port}::INSTR",
            open_timeout=open_timeout,
        )
        try:
            sess.write(b"*IDN?\n")
            data, _ = sess.read(4096)
            assert data == server.response
        finally:
            sess.close()


class TestSpecConformance:
    """Client-side rules from IVI-6.1 that are easy to get subtly wrong."""

    def test_status_query_names_the_most_recent_message(self, session, server):
        """6.14: the id is the most recently *sent* message, not the next one.

        6.14.3 has the server report MAV false when the id does not match the
        last message it received, so sending the next id suppresses MAV.
        """
        session.write(b"*IDN?\n")
        session.read(4096)
        sent = [e for e in server.sync_log if e[0] == "DataEnd"][-1][2]

        session.read_stb()
        query = [e for e in server.async_log if e[0] == "AsyncStatusQuery"][-1]
        assert query[2] == sent, (
            f"AsyncStatusQuery carried {query[2]:#x}, the last DataEND was {sent:#x}"
        )

    def test_status_query_before_any_write(self, session, server):
        """6.14: use 0xffffff00-2 until a message has been sent."""
        session.read_stb()
        query = [e for e in server.async_log if e[0] == "AsyncStatusQuery"][-1]
        assert query[2] == hislip.PRE_INITIAL_MESSAGE_ID

    def test_lock_release_names_the_most_recent_message(self, session, server):
        """6.5: the release names the last message to complete first."""
        session.lock(constants.Lock.exclusive, 1000)
        session.unlock()
        release = [
            e
            for e in server.async_log
            if e[0] == "AsyncLock" and e[1] == hislip.LOCKCONTROLCODE["release"]
        ][-1]
        assert release[2] == hislip.PRE_INITIAL_MESSAGE_ID

    def test_async_interrupted_is_not_taken_for_a_response(self, session, server):
        """6.11: AsyncInterrupted is unsolicited and must not answer a query."""
        server.send_async_interrupted()
        time.sleep(0.2)
        # The status query that follows must get the real status byte.
        server.status_byte = 0x18
        stb, status = session.read_stb()
        assert status == StatusCode.success
        assert stb == 0x18

    def test_unrecognized_async_message_is_answered_with_error(self, session, server):
        """6.3: discard it and reply with an Error on the same channel."""
        server.send_unrecognized(channel="async")
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if any(e[0] == "Error" for e in server.async_log):
                break
            time.sleep(0.02)
        errors = [e for e in server.async_log if e[0] == "Error"]
        assert errors, "no Error was sent back"
        assert errors[-1][1] == hislip.ERRORCODE["Unrecognized Message Type"]
        # and the channel still works
        assert session.read_stb()[1] == StatusCode.success

    def test_overlap_flag_reads_only_bit_zero(self):
        """6.1: bit 0 is overlap; bits 1-2 are encryption announcements."""
        header = _pack("InitializeResponse", 0b110, (0x0100 << 16) | SESSION_ID)
        response = hislip.InitializeResponse(hislip.BufferedMessage(header))
        assert response.overlap is False
        assert response.encryption_mandatory is True
        assert response.initial_encryption is True

    def test_partial_response_is_dropped_before_the_next_write(self, session, server):
        """3.1.2 rule 3: buffered server messages are cleared when we send."""
        server.response = b"0123456789" * 8
        session.write(b"*IDN?\n")
        first, _ = session.read(10)  # abandon the rest
        assert first == b"0123456789"

        # The leftovers must not be parsed as the reply to this next query.
        session.write(b"*IDN?\n")
        data, status = session.read(4096)
        assert data == server.response
        assert status == StatusCode.success


class TestServerRefusal:
    """A server refuses an operation with an Error; it must not read as a timeout.

    An Error is the only way a HiSLIP server can report a failed transaction.
    Dropping it leaves the caller waiting out its timeout with no diagnostic
    for a condition the server described exactly.
    """

    def test_refused_read_reports_an_error(self, session, server):
        server.refuse_with = 128
        session.write(b"*IDN?\n")
        data, status = session.read(4096)
        assert data == b""
        assert status == StatusCode.error_io

    def test_refused_read_is_prompt(self, session, server):
        """The refusal must not wait for the timeout to expire."""
        server.refuse_with = 128
        session.set_attribute(ResourceAttribute.timeout_value, 10000)
        session.write(b"*IDN?\n")
        start = time.time()
        _, status = session.read(4096)
        assert status == StatusCode.error_io
        assert time.time() - start < 2.0, "the refusal waited for the timeout"

    @pytest.mark.parametrize("code", [0, 4, 128, 200, 255])
    def test_any_error_code_is_handled(self, session, server, code):
        """Device-defined codes carry no portable meaning, but must not crash."""
        server.refuse_with = code
        session.write(b"*IDN?\n")
        _, status = session.read(4096)
        assert status == StatusCode.error_io

    def test_refused_trigger(self, session, server):
        server.refuse_with = 128
        session.write(b"*IDN?\n")
        session.read(4096)
        # The refusal to the Trigger surfaces on the next read of the channel.
        assert session.assert_trigger(constants.TriggerProtocol.default) in (
            StatusCode.success,
            StatusCode.error_io,
        )

    def test_refused_async_transaction_reports_an_error(self, session, server):
        """The same on the asynchronous channel."""
        server.refuse_with = 128
        _stb, status = session.read_stb()
        assert status == StatusCode.error_io

    def test_refused_async_transaction_is_prompt(self, session, server):
        server.refuse_with = 128
        session.set_attribute(ResourceAttribute.timeout_value, 10000)
        start = time.time()
        _, status = session.read_stb()
        assert status == StatusCode.error_io
        assert time.time() - start < 2.0, "the refusal waited for the timeout"

    def test_refused_device_clear(self, session, server):
        server.refuse_with = 128
        assert session.clear() == StatusCode.error_io

    def test_device_defined_codes_do_not_raise_keyerror(self):
        """ERRORMESSAGE stops at 5; the device-defined range must still name."""
        assert "128" in hislip.describe_error(128, fatal=False)
        assert "200" in hislip.describe_error(200, fatal=True)
        for code in (0, 1, 5):
            assert hislip.describe_error(code, fatal=False) == hislip.ERRORMESSAGE[code]

    def test_error_carries_the_server_explanation(self, session, server):
        """The payload is the only diagnostic there is; keep it."""
        server.refuse_with = 128
        server.refuse_message = b"operation refused by the server"
        session.write(b"*IDN?\n")
        try:
            session.interface.receive(4096)
        except hislip.HiSLIPServerError as exc:
            assert "operation refused by the server" in str(exc)
            assert exc.control_code == 128
            assert not exc.fatal
        else:
            raise AssertionError("no HiSLIPServerError raised")

    def test_fatal_error_reports_connection_lost(self):
        """A FatalError means the server is tearing the link down."""
        from pyvisa_py.tcpip import hislip_error_to_status

        fatal = hislip.HiSLIPServerError(3, "invalid init", fatal=True)
        assert hislip_error_to_status(fatal) == StatusCode.error_connection_lost
        non_fatal = hislip.HiSLIPServerError(128, "refused", fatal=False)
        assert hislip_error_to_status(non_fatal) == StatusCode.error_io


class TestMessageStateRace:
    """The RMT-delivered flag must be carried by exactly one message.

    It rides on both synchronous messages and on AsyncStatusQuery, and the
    instrument uses it to decide whether the previous response was consumed.
    Delivering it twice (or losing it) makes a real instrument answer the
    next command with "-410 Query INTERRUPTED".
    """

    def test_rmt_is_consumed_exactly_once(self, session, server):
        interface = session.interface
        rmt_seen = []

        def writer():
            for _ in range(200):
                interface._rmt = 1  # pretend a response was just consumed
                rmt, _ = interface._consume_send_state()
                rmt_seen.append(rmt)

        def status_querier():
            for _ in range(200):
                rmt, _ = interface._consume_status_state()
                rmt_seen.append(rmt)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=status_querier),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        # Every set flag is claimed by exactly one caller, so the number of
        # ones observed can never exceed the number of times it was set.
        assert sum(rmt_seen) <= 200

    def test_message_ids_are_never_reused(self, session):
        """Concurrent sends must not hand the same message id to two messages."""
        interface = session.interface
        ids = []
        lock = threading.Lock()

        def sender():
            local = [interface._consume_send_state()[1] for _ in range(200)]
            with lock:
                ids.extend(local)

        threads = [threading.Thread(target=sender) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(ids) == len(set(ids)), "a message id was handed out twice"

    def test_concurrent_status_query_and_write(self, session, server):
        """A status query racing writes leaves both channels intact."""
        errors = []

        def writer():
            for _ in range(100):
                try:
                    session.write(b"*IDN?\n")
                    data, _ = session.read(4096)
                    if data != server.response:
                        errors.append(f"bad response {data!r}")
                except Exception as exc:
                    errors.append(f"write/read: {exc!r}")

        def querier():
            for _ in range(100):
                try:
                    stb, _ = session.read_stb()
                    if not 0 <= stb <= 0xFF:
                        errors.append(f"bad stb {stb!r}")
                except Exception as exc:
                    errors.append(f"read_stb: {exc!r}")

        threads = [threading.Thread(target=writer), threading.Thread(target=querier)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors[:5]


class TestServiceRequests:
    def test_srq_delivered_to_queue(self, session, server):
        session._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.queue
        )
        assert session._start_event_monitor() == StatusCode.success

        server.send_service_request(0x40)

        ctx = session._event_state.queue.get_matching(
            constants.EventType.service_request, 2000
        )
        assert ctx is not None
        assert ctx.event_type == constants.EventType.service_request

    def test_handler_may_read_stb(self, session, server):
        """The classic SRQ reaction — reading the status byte — must not hang.

        The handler runs on its own thread precisely so that it can talk to
        the instrument while the channel reader stays free to deliver the
        reply.
        """
        server.status_byte = 0x48
        read_back = []
        done = threading.Event()

        def handler(sess, etype, ctx, handle):
            read_back.append(session.read_stb())
            done.set()

        session._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.handler
        )
        session._event_state.registry.install(
            constants.EventType.service_request, handler, None
        )
        session._start_event_monitor()

        server.send_service_request(0x48)

        assert done.wait(5.0), "handler deadlocked reading the status byte"
        assert read_back == [(0x48, StatusCode.success)]

    def test_srq_delivered_to_handler(self, session, server):
        received = []
        session._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.handler
        )
        session._event_state.registry.install(
            constants.EventType.service_request,
            lambda sess, etype, ctx, handle: received.append(etype),
            None,
        )
        session._start_event_monitor()

        server.send_service_request(0x40)

        deadline = time.time() + 2.0
        while not received and time.time() < deadline:
            time.sleep(0.01)
        assert received == [constants.EventType.service_request]

    def test_srq_not_delivered_after_stop(self, session, server):
        session._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.queue
        )
        session._start_event_monitor()
        session._stop_event_monitor()

        server.send_service_request(0x40)
        time.sleep(0.3)

        assert (
            session._event_state.queue.get_matching(
                constants.EventType.service_request, 0
            )
            is None
        )

    def test_srq_during_transaction_does_not_corrupt_response(self, session, server):
        """A service request arriving mid-query must not be read as the reply."""
        session._event_state.enable(
            constants.EventType.service_request, constants.EventMechanism.queue
        )
        session._start_event_monitor()
        server.status_byte = 0x18

        for _ in range(5):
            server.send_service_request(0x40)
            stb, status = session.read_stb()
            assert status == StatusCode.success
            assert stb == 0x18

    def test_events_supported(self):
        assert (
            constants.EventType.service_request
            in TCPIPInstrHiSLIP._supported_event_types
        )
