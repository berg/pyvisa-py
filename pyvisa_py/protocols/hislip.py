"""
Python implementation of HiSLIP protocol.  Based on the HiSLIP spec:

http://www.ivifoundation.org/downloads/Class%20Specifications/IVI-6.1_HiSLIP-1.1-2024-02-24.pdf
"""

import queue
import select
import socket
import struct
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from pyvisa_py.common import (
    LOGGER,
    BytesBuffer,
    MutableBytesBuffer,
    SupportsRecvInto,
    set_keepalive,
)

PORT = 4880

MESSAGETYPE_STR: Dict[int, str] = {
    0: "Initialize",
    1: "InitializeResponse",
    2: "FatalError",
    3: "Error",
    4: "AsyncLock",
    5: "AsyncLockResponse",
    6: "Data",
    7: "DataEnd",
    8: "DeviceClearComplete",
    9: "DeviceClearAcknowledge",
    10: "AsyncRemoteLocalControl",
    11: "AsyncRemoteLocalResponse",
    12: "Trigger",
    13: "Interrupted",
    14: "AsyncInterrupted",
    15: "AsyncMaxMsgSize",
    16: "AsyncMaxMsgSizeResponse",
    17: "AsyncInitialize",
    18: "AsyncInitializeResponse",
    19: "AsyncDeviceClear",
    20: "AsyncServiceRequest",
    21: "AsyncStatusQuery",
    22: "AsyncStatusResponse",
    23: "AsyncDeviceClearAcknowledge",
    24: "AsyncLockInfo",
    25: "AsyncLockInfoResponse",
    26: "GetDescriptors",
    27: "GetDescriptorsResponse",
    28: "StartTLS",
    29: "AsyncStartTLS",
    30: "AsyncStartTLSResponse",
    31: "EndTLS",
    32: "AsyncEndTLS",
    33: "AsyncEndTLSResponse",
    34: "GetSaslMechanismList",
    35: "GetSaslMechanismListResponse",
    36: "AuthenticationStart",
    37: "AuthenticationExchange",
    38: "AuthenticationResult",
    # reserved for future use         39-127 inclusive
    # VendorSpecific                  128-255 inclusive
}
MESSAGETYPE: Dict[str, int] = {value: key for (key, value) in MESSAGETYPE_STR.items()}

FATALERRORMESSAGE: Dict[int, str] = {
    0: "Unidentified error",
    1: "Poorly formed message header",
    2: "Attempt to use connection without both channels established",
    3: "Invalid Initialization sequence",
    4: "Server refused connection due to maximum number of clients exceeded",
    5: "Secure connection failed",
    # 6-127:   reserved for HiSLIP extensions
    # 128-255: device defined errors
}
FATALERRORCODE: Dict[str, int] = {
    value: key for (key, value) in FATALERRORMESSAGE.items()
}

ERRORMESSAGE: Dict[int, str] = {
    0: "Unidentified error",
    1: "Unrecognized Message Type",
    2: "Unrecognized control code",
    3: "Unrecognized Vendor Defined Message",
    4: "Message too large",
    5: "Authentication failed",
    # 6-127:   Reserved
    # 128-255: Device defined errors
}
ERRORCODE: Dict[str, int] = {value: key for (key, value) in ERRORMESSAGE.items()}

LOCKCONTROLCODE: Dict[str, int] = {
    "release": 0,
    "request": 1,
}

LOCKRESPONSE: Dict[int, str] = {
    0: "failure",
    1: "success",  # or "success exclusive"
    2: "success shared",
    3: "error",
}

REMOTELOCALCONTROLCODE: Dict[str, int] = {
    "disableRemote": 0,
    "enableRemote": 1,
    "disableAndGTL": 2,
    "enableAndGotoRemote": 3,
    "enableAndLockoutLocal": 4,
    "enableAndGTRLLO": 5,
    "justGTL": 6,
}

HEADER_FORMAT = "!2sBBIQ"
# !  = network order,
# 2s = prologue ('HS'),
# B  = message type (unsigned byte),
# B  = control code (unsigned byte),
# I  = message parameter (unsigned int),
# Q  = payload length (unsigned long long)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

DEFAULT_MAX_MSG_SIZE = 1 << 20  # from VISA spec

#: Seconds to allow for the TCP connection when the caller did not ask for a
#: particular open timeout.
DEFAULT_CONNECT_TIMEOUT = 5.0

#: The MessageID a client starts from; it steps by two per Data/DataEND/Trigger
#: and is reset to this on initialization and device clear (IVI-6.1 3.1.2).
INITIAL_MESSAGE_ID = 0xFFFF_FF00

#: The MessageID standing for "no message sent yet". Messages that name the
#: most recently sent Data/DataEND/Trigger use it before there has been one;
#: IVI-6.1 writes it "0xffffff00-2" for both AsyncStatusQuery (6.14) and
#: AsyncLock release (6.5).
PRE_INITIAL_MESSAGE_ID = (INITIAL_MESSAGE_ID - 2) & 0xFFFF_FFFF

#: Seconds to let in-process synchronous messages finish after the server
#: acknowledges a device clear, before telling it the channel is clear.
#: Some instruments need longer than this when several messages were sent
#: back to back without waiting for a response, and drop the connection if
#: DeviceClearComplete arrives while they are still working through them.
DEVICE_CLEAR_SETTLE_TIME = 0.1


class HiSLIPInterruptedError(Exception):
    """Raised when a pending I/O operation is cancelled via terminate().

    This is the pyvisa-py equivalent of NI-VISA's VI_ERROR_ABORT.
    """

    def __init__(self, message_id: int = 0):
        self.message_id = message_id
        super().__init__(f"HiSLIP I/O terminated (message_id={message_id:#x})")


class HiSLIPConnectionLost(RuntimeError):
    """Raised when the server closes a HiSLIP connection unexpectedly.

    Subclasses ``RuntimeError`` because that is what earlier versions of this
    module raised for a dropped connection.
    """


class HiSLIPUnrecognizedMessage(RuntimeError):
    """A message this client cannot process, but which keeps us in sync.

    IVI-6.1 6.3 requires the receiver to discard such a message and reply
    with an Error on the channel it arrived on. Subclasses ``RuntimeError``
    because that is what earlier versions raised.

    """

    def __init__(self, message_type: int, payload_length: int):
        self.message_type = message_type
        self.payload_length = payload_length
        super().__init__(f"unrecognized message type: {message_type}")


class HiSLIPSynchronizationLost(RuntimeError):
    """Framing has failed and the connection can no longer be trusted.

    IVI-6.1 6.2 requires a FatalError on both channels followed by closing
    the connection.

    """


class HiSLIPServerError(Exception):
    """Raised when the server reports an Error or FatalError message.

    This is how a HiSLIP server refuses a transaction — there is no dedicated
    "operation refused" message — so the control code and the human readable
    payload are both kept for the session layer to map and report.

    Control codes 128-255 are device defined and carry no meaning fixed by
    the protocol; ``description`` is the server's own words for what went
    wrong.

    """

    def __init__(self, control_code: int, description: str, fatal: bool = False):
        self.control_code = control_code
        self.description = description
        self.fatal = fatal
        kind = "fatal error" if fatal else "error"
        super().__init__(f"HiSLIP {kind} {control_code}: {description}")


class CancellableSocket(socket.socket):
    """Socket subclass that supports cross-thread cancellation via select().

    Takes ownership of an existing socket's file descriptor and interposes
    a cancel pipe on recv_into().  When cancel() is called from another
    thread, any blocked recv_into() returns immediately with
    HiSLIPInterruptedError.

    This implements the "self-pipe trick" for viTerminate() support.
    """

    def __init__(self, sock: socket.socket) -> None:
        # Transfer the file descriptor from the original socket.  Socket
        # options (TCP_NODELAY, SO_KEEPALIVE, etc.) are properties of the
        # kernel fd and are preserved across detach/re-attach.  Only
        # Python-level state (timeout) needs explicit transfer.
        family, type_, proto = sock.family, sock.type, sock.proto
        timeout = sock.gettimeout()
        fd = sock.detach()
        super().__init__(family=family, type=type_, proto=proto, fileno=fd)
        self.settimeout(timeout)
        self._cancel_r, self._cancel_w = socket.socketpair()
        self._cancel_r.setblocking(False)
        self._cancel_w.setblocking(False)
        self._cancel_enabled = True

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        """Cancellable recv_into using select().

        Blocks until data is available on the underlying socket OR the cancel
        pipe is signalled.  Honours the socket's timeout.
        """
        if not self._cancel_enabled:
            return super().recv_into(buffer, nbytes, flags)
        timeout = self.gettimeout()
        readable, _, _ = select.select([self, self._cancel_r], [], [], timeout)
        if not readable:
            raise socket.timeout("timed out")
        if self._cancel_r in readable:
            self.drain_cancel()
            raise HiSLIPInterruptedError(0)
        return super().recv_into(buffer, nbytes, flags)

    def cancel(self) -> None:
        """Signal cancellation — unblocks any pending recv_into()."""
        try:
            self._cancel_w.send(b"\x00")
        except BlockingIOError:
            pass  # already signalled

    def drain_cancel(self) -> None:
        """Drain all bytes from the cancel pipe."""
        try:
            while self._cancel_r.recv(1024):
                pass
        except BlockingIOError:
            pass

    def close(self) -> None:
        self._cancel_r.close()
        self._cancel_w.close()
        super().close()


#########################################################################################


def receive_flush(sock: SupportsRecvInto, recv_len: int) -> None:
    """
    receive exactly 'recv_len' bytes from 'sock'.
    no explicit timeout is specified, since it is assumed
    that a call to select indicated that data is available.
    received data is thrown away and nothing is returned
    """
    # limit the size of the recv_buffer to something moderate
    # in order to limit the impact on virtual memory
    recv_buffer = bytearray(min(1 << 20, recv_len))
    bytes_recvd = 0

    while bytes_recvd < recv_len:
        request_size = min(len(recv_buffer), recv_len - bytes_recvd)
        data_len = sock.recv_into(recv_buffer, request_size)
        bytes_recvd += data_len


def receive_exact(sock: SupportsRecvInto, recv_len: int) -> bytearray:
    """
    receive exactly 'recv_len' bytes from 'sock'.
    no explicit timeout is specified, since it is assumed
    that a call to select indicated that data is available.
    returns a bytearray containing the received data.
    """
    recv_buffer = bytearray(recv_len)
    receive_exact_into(sock, recv_buffer)
    return recv_buffer


def receive_exact_into(sock: SupportsRecvInto, recv_buffer: MutableBytesBuffer) -> None:
    """
    receive data from 'sock' to exactly fill 'recv_buffer'.
    no explicit timeout is specified, since it is assumed
    that a call to select indicated that data is available.
    """
    view = memoryview(recv_buffer)
    recv_len = len(recv_buffer)
    bytes_recvd = 0

    while bytes_recvd < recv_len:
        request_size = recv_len - bytes_recvd
        data_len = sock.recv_into(view, request_size)
        if data_len == 0:
            raise HiSLIPConnectionLost("Connection was dropped by server.")
        bytes_recvd += data_len
        view = view[data_len:]

    if bytes_recvd > recv_len:
        raise MemoryError("socket.recv_into scribbled past end of recv_buffer")


def describe_error(control_code: int, fatal: bool) -> str:
    """Name a HiSLIP error control code, including the device defined range."""
    table = FATALERRORMESSAGE if fatal else ERRORMESSAGE
    kind = "fatal error" if fatal else "error"
    return table.get(control_code, f"device defined {kind} {control_code}")


def error_from_header(sock: SupportsRecvInto, header: "RxHeader") -> HiSLIPServerError:
    """Read the payload of an already-received Error/FatalError and describe it.

    The payload is the server's explanation, so it is worth carrying rather
    than discarding — for a refusal it is the only diagnostic there is.

    """
    fatal = header.msg_type == "FatalError"
    detail = ""
    if header.payload_length:
        try:
            detail = bytes(receive_exact(sock, header.payload_length)).decode(
                "utf-8", "replace"
            )
        except (OSError, RuntimeError):
            detail = ""
    name = describe_error(header.control_code, fatal)
    return HiSLIPServerError(
        header.control_code, f"{name}: {detail}" if detail else name, fatal
    )


def send_msg(
    sock: socket.socket,
    msg_type: str,
    control_code: int,
    message_parameter: Optional[int],
    payload: BytesBuffer = b"",
) -> None:
    """Send a message on sock w/ payload."""
    msg = bytearray(
        struct.pack(
            HEADER_FORMAT,
            b"HS",
            MESSAGETYPE[msg_type],
            control_code,
            message_parameter or 0,
            len(payload),
        )
    )
    # txdecode(msg, payload)  # uncomment for debugging
    msg.extend(payload)
    sock.sendall(msg)


class RxHeader:
    """Generic base class for receiving messages.

    specific protocol responses subclass this class.
    """

    def __init__(
        self,
        sock: SupportsRecvInto,
        expected_message_type: Optional[str] = None,
    ) -> None:
        """receive and decode the HiSLIP message header"""
        self.header = receive_exact(sock, HEADER_SIZE)
        # rxdecode(self.header)  # uncomment for debugging
        (
            prologue,
            msg_type,
            self.control_code,
            self.message_parameter,
            self.payload_length,
        ) = struct.unpack(HEADER_FORMAT, self.header)

        if prologue != b"HS":
            # Framing is gone; the caller sends a FatalError and closes.
            raise HiSLIPSynchronizationLost("protocol synchronization error")

        if msg_type not in MESSAGETYPE_STR:
            # Recoverable: the caller discards the payload and answers Error.
            raise HiSLIPUnrecognizedMessage(msg_type, self.payload_length)

        self.msg_type = MESSAGETYPE_STR[msg_type]

        if expected_message_type is not None and self.msg_type != expected_message_type:
            # XXX we should send an 'Error: Unidentified Error' to the server
            # and discard this packet plus any payload
            payload = (
                (": " + str(receive_exact(sock, self.payload_length)))
                if self.payload_length > 0
                else ""
            )
            raise RuntimeError(
                "expected message type '%s', received '%s%s'"
                % (expected_message_type, self.msg_type, payload)
            )

        if self.msg_type == "DataEnd" or self.msg_type == "Data":
            assert self.control_code == 0
            self.message_id = self.message_parameter


class InitializeResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "InitializeResponse")
        assert self.payload_length == 0
        # IVI-6.1 6.1: bit 0 is overlap mode, bit 1 encryption mode, bit 2
        # initial encryption. Reading the whole byte would mistake a server
        # announcing mandatory encryption for one requesting overlapped mode.
        self.overlap = bool(self.control_code & 0x01)
        self.encryption_mandatory = bool(self.control_code & 0x02)
        self.initial_encryption = bool(self.control_code & 0x04)
        self.version, self.session_id = struct.unpack("!4xHH8x", self.header)


class AsyncInitializeResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncInitializeResponse")
        assert self.control_code == 0
        assert self.payload_length == 0
        self.vendor_id = struct.unpack("!4x4s8x", self.header)


class AsyncMaxMsgSizeResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncMaxMsgSizeResponse")
        assert self.control_code == 0
        assert self.message_parameter == 0
        assert self.payload_length == 8
        payload = receive_exact(sock, self.payload_length)
        self.max_msg_size = struct.unpack("!Q", payload)[0]


class AsyncDeviceClearAcknowledge(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncDeviceClearAcknowledge")
        self.feature_bitmap = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncInterrupted(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncInterrupted")
        assert self.control_code == 0
        self.message_id = self.message_parameter
        assert self.payload_length == 0


class AsyncLockInfoResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncLockInfoResponse")
        self.exclusive_lock = self.control_code  # 0: no lock, 1: lock granted
        self.clients_holding_locks = self.message_parameter
        assert self.payload_length == 0


class AsyncLockResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncLockResponse")
        self.lock_response = LOCKRESPONSE[self.control_code]
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncRemoteLocalResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncRemoteLocalResponse")
        assert self.control_code == 0
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncServiceRequest(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncServiceRequest")
        self.server_status = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class AsyncStatusResponse(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "AsyncStatusResponse")
        self.server_status = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class DeviceClearAcknowledge(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "DeviceClearAcknowledge")
        self.feature_bitmap = self.control_code
        assert self.message_parameter == 0
        assert self.payload_length == 0


class Interrupted(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "Interrupted")
        assert self.control_code == 0
        self.message_id = self.message_parameter
        assert self.payload_length == 0


class Error(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "Error")
        # 128-255 are device defined, so there is no name to look up.
        self.error_code = ERRORMESSAGE.get(
            self.control_code, f"device defined error {self.control_code}"
        )
        assert self.message_parameter == 0
        self.error_message = receive_exact(sock, self.payload_length)


class FatalError(RxHeader):
    def __init__(self, sock: SupportsRecvInto) -> None:
        super().__init__(sock, "FatalError")
        # 128-255 are device defined, so there is no name to look up.
        self.error_code = FATALERRORMESSAGE.get(
            self.control_code, f"device defined fatal error {self.control_code}"
        )
        assert self.message_parameter == 0
        self.error_message = receive_exact(sock, self.payload_length)


class BufferedMessage:
    """Socket-like reader that serves one already-received message.

    The ``RxHeader`` subclasses read straight from a socket.  When the async
    channel is demultiplexed by a background thread the bytes have already
    been read, so they are replayed through this shim instead, which lets the
    response classes be reused unchanged.
    """

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._pos = 0

    def recv_into(self, buffer, nbytes: int = 0, flags: int = 0) -> int:
        view = memoryview(buffer)
        available = len(self._data) - self._pos
        count = min(nbytes or len(view), len(view), available)
        view[:count] = self._data[self._pos : self._pos + count]
        self._pos += count
        return count


class AsyncChannel:
    """Owns the HiSLIP asynchronous socket and demultiplexes incoming messages.

    The server may send an ``AsyncServiceRequest`` at any time, including in
    the middle of an unrelated request/response exchange.  A single reader
    thread therefore owns the socket: service requests are dispatched to the
    SRQ callback and every other message is handed to whichever thread is
    waiting in :meth:`transaction`.

    Requests are serialised by a lock, so at most one response is outstanding
    at a time.
    """

    def __init__(self, sock: socket.socket, timeout: float) -> None:
        self._sock = sock
        self._timeout = timeout
        # Reads in the reader thread block until a whole message arrives; the
        # idle wait is done with select() so the thread stays interruptible.
        self._sock.settimeout(None)
        self._request_lock = threading.Lock()
        self._responses: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._srq_callback: Optional[Callable[[int], None]] = None
        self._interrupted_callback: Optional[Callable[[int], None]] = None
        self._async_interrupted = threading.Event()
        self._srq_lock = threading.Lock()
        # Service requests are handed to a second thread so that a callback
        # is free to talk to the instrument — the usual reaction to an SRQ is
        # to read the status byte, which needs the reader thread to be free
        # to deliver the response.
        self._srq_queue: "queue.Queue[Optional[int]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._reader, name="hislip-async", daemon=True
        )
        self._srq_thread = threading.Thread(
            target=self._srq_worker, name="hislip-srq", daemon=True
        )
        self._thread.start()
        self._srq_thread.start()

    @property
    def timeout(self) -> float:
        """Time in seconds to wait for the response to a request."""
        return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        self._timeout = value

    def set_srq_callback(self, callback: Optional[Callable[[int], None]]) -> None:
        """Register (or clear) the callback invoked on AsyncServiceRequest.

        The callback receives the status byte carried by the service request.
        It runs on a dedicated thread and may perform I/O on the instrument.
        """
        with self._srq_lock:
            self._srq_callback = callback

    def close(self) -> None:
        self._stop.set()
        self._srq_queue.put(None)
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        self._thread.join(timeout=2.0)
        self._srq_thread.join(timeout=2.0)

    def transaction(
        self,
        msg_type: str,
        control_code: int,
        message_parameter: Optional[int],
        payload: BytesBuffer = b"",
        timeout: Optional[float] = None,
    ) -> BufferedMessage:
        """Send a request and return the matching response as a reader.

        Parameters
        ----------
        timeout : float, optional
            Seconds to wait for the response.  Defaults to the channel
            timeout; pass a larger value for requests such as ``AsyncLock``
            that the server is expected to sit on.

        """
        wait = self.timeout if timeout is None else timeout
        with self._request_lock:
            # A previous request may have timed out and been answered since;
            # that answer is not ours, so drop it rather than mistake it for
            # the response to this request.
            self._discard_stale_responses()
            try:
                send_msg(self._sock, msg_type, control_code, message_parameter, payload)
            except OSError as error:
                raise HiSLIPConnectionLost(
                    f"could not send {msg_type}: {error}"
                ) from error
            try:
                response = self._responses.get(timeout=wait)
            except queue.Empty:
                raise socket.timeout(
                    f"timed out waiting for the response to {msg_type}"
                ) from None
        if response is None:
            raise HiSLIPConnectionLost(
                "the asynchronous channel was closed by the server"
            )

        reader = BufferedMessage(response)
        # The server answers a refused asynchronous request with an Error
        # rather than the response type we asked for. Recognise it here, or
        # the response class raises an opaque synchronization error.
        header = RxHeader(BufferedMessage(response))
        if header.msg_type in ("Error", "FatalError"):
            raise error_from_header(BufferedMessage(response[HEADER_SIZE:]), header)
        return reader

    def _discard_stale_responses(self) -> None:
        """Drop queued responses left over from a request that timed out."""
        while True:
            try:
                stale = self._responses.get_nowait()
            except queue.Empty:
                return
            if stale is None:
                # End-of-channel sentinel: put it back so the next waiter
                # still sees that the connection is gone.
                self._responses.put(None)
                return

    def _reader(self) -> None:
        """Read messages until the channel is closed, dispatching each one."""
        try:
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select([self._sock], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue

                try:
                    message = self._read_message()
                except (OSError, RuntimeError, struct.error):
                    if not self._stop.is_set():
                        LOGGER.debug(
                            "HiSLIP asynchronous channel closed", exc_info=True
                        )
                    break

                if message is None:
                    break

                msg_type, raw = message
                if msg_type == "AsyncServiceRequest":
                    self._dispatch_srq(raw)
                elif msg_type == "AsyncInterrupted":
                    # Unsolicited, like a service request: the server sends it
                    # when it abandons a response. Queueing it as a reply would
                    # hand it to whichever transaction ran next.
                    self._dispatch_interrupted(raw)
                elif not msg_type:
                    # Unrecognized but still framed: discard and say so, per
                    # IVI-6.1 6.3, on the channel it arrived on.
                    self._report_unrecognized(raw)
                else:
                    self._responses.put(raw)
        finally:
            # Unblock anyone waiting on a response to a request that can no
            # longer be answered.
            self._responses.put(None)

    def _read_message(self) -> Optional[Tuple[str, bytes]]:
        """Read one complete message.  Returns None if the peer hung up."""
        header = bytearray(HEADER_SIZE)
        view = memoryview(header)
        received = 0
        while received < HEADER_SIZE:
            count = self._sock.recv_into(view, HEADER_SIZE - received)
            if count == 0:
                return None
            received += count
            view = view[count:]

        prologue, msg_type, _, _, payload_length = struct.unpack(HEADER_FORMAT, header)
        if prologue != b"HS":
            raise HiSLIPSynchronizationLost("protocol synchronization error")

        payload = receive_exact(self._sock, payload_length) if payload_length else b""
        return MESSAGETYPE_STR.get(msg_type, ""), bytes(header) + bytes(payload)

    def _report_unrecognized(self, raw: bytes) -> None:
        """Answer an unrecognized message with an Error, per IVI-6.1 6.3."""
        message_type = raw[2] if len(raw) > 2 else 0
        LOGGER.debug(
            "unrecognized HiSLIP message type %d on the async channel", message_type
        )
        try:
            with self._request_lock:
                send_msg(
                    self._sock,
                    "Error",
                    ERRORCODE["Unrecognized Message Type"],
                    0,
                    b"unrecognized message type",
                )
        except OSError:
            LOGGER.debug("could not report the unrecognized message", exc_info=True)

    def _dispatch_interrupted(self, raw: bytes) -> None:
        """Note an AsyncInterrupted and release anyone waiting for one."""
        with self._srq_lock:
            callback = self._interrupted_callback
        self._async_interrupted.set()
        if callback is None:
            return
        try:
            callback(AsyncInterrupted(BufferedMessage(raw)).message_id)
        except Exception:
            LOGGER.exception("error dispatching HiSLIP AsyncInterrupted")

    def wait_for_async_interrupted(self, timeout: float) -> bool:
        """Wait for an AsyncInterrupted, returning whether one arrived.

        IVI-6.1 3.1.2 rule 4: a client that saw Interrupted first must not
        send anything more until the matching AsyncInterrupted arrives.

        """
        return self._async_interrupted.wait(timeout)

    def clear_async_interrupted(self) -> None:
        self._async_interrupted.clear()

    def _dispatch_srq(self, raw: bytes) -> None:
        """Queue a service request for the worker thread to deliver."""
        with self._srq_lock:
            if self._srq_callback is None:
                return
        try:
            status_byte = AsyncServiceRequest(BufferedMessage(raw)).server_status
        except Exception:
            LOGGER.exception("could not decode a HiSLIP service request")
            return
        self._srq_queue.put(status_byte)

    def _srq_worker(self) -> None:
        """Deliver queued service requests to the registered callback."""
        while True:
            status_byte = self._srq_queue.get()
            if status_byte is None or self._stop.is_set():
                return
            with self._srq_lock:
                callback = self._srq_callback
            if callback is None:
                continue
            try:
                callback(status_byte)
            except Exception:
                LOGGER.exception("error dispatching HiSLIP service request")


class Instrument:
    """
    this is the principal export from this module.  it opens up a HiSLIP
    connection to the instrument at the specified IP address.
    """

    def __init__(
        self,
        ip_addr: str,
        open_timeout: Optional[float] = None,
        timeout: Optional[float] = None,
        port: int = PORT,
        sub_address: str = "hislip0",
    ) -> None:
        # init transaction:
        #     C->S: Initialize
        #     S->C: InitializeResponse
        #     C->S: AsyncInitialize
        #     S->C: AsyncInitializeResponse

        timeout = timeout or 5.0
        # ``open_timeout`` is expressed in milliseconds, like the VISA
        # attribute it comes from. Both None and 0 mean "no preference": 0 is
        # VI_TMO_IMMEDIATE and, more to the point, is what
        # ``ResourceManager.open_resource`` passes when the caller says
        # nothing, so taking it literally would make every default open fail
        # on a non-blocking connect. TCPIPSocketSession treats it the same way.
        connect_timeout = (
            1e-3 * open_timeout if open_timeout else DEFAULT_CONNECT_TIMEOUT
        )

        # Message state has to exist before any I/O: initialize() and the
        # async channel reader may both touch it.
        self._rmt = 0
        self._message_id = INITIAL_MESSAGE_ID
        self._last_message_id: Optional[int] = None
        self._msg_type: str = ""
        self._payload_remaining: int = 0
        self._receiving = threading.Event()
        #: An Interrupted arrived and its AsyncInterrupted has not yet.
        self._interrupted_pending = False
        # Guards the message bookkeeping shared by the synchronous send path
        # and the asynchronous status query.
        self._state_lock = threading.RLock()
        self._timeout = timeout
        self._async_channel: Optional[AsyncChannel] = None

        # open the synchronous socket and send an initialize packet
        raw_sync = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sync.settimeout(connect_timeout)
        raw_sync.connect((ip_addr, port))
        raw_sync.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Wrap with CancellableSocket for viTerminate() support.
        # The wrapper interposes select() on recv_into() so that a cancel()
        # call from another thread can unblock a pending read.
        self._sync: CancellableSocket = CancellableSocket(raw_sync)

        init = self.initialize(sub_address=sub_address.encode("ascii"))
        if init.overlap != 0:
            LOGGER.debug("HiSLIP server prefers overlap = %d", init.overlap)
        # We set the user timeout once we managed to initialize the connection.
        self._sync.settimeout(timeout)

        # open the asynchronous socket and send an initialize packet
        self._async = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._async.settimeout(connect_timeout)
        self._async.connect((ip_addr, port))
        self._async.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._async_init = self.async_initialize(session_id=init.session_id)

        # From here on the async socket is owned by the channel reader thread,
        # which splits service requests out from ordinary responses.
        self._async_channel = AsyncChannel(self._async, timeout)

        # initialize variables
        self.max_msg_size = DEFAULT_MAX_MSG_SIZE
        self.keepalive = False

    # ================ #
    # MEMBER FUNCTIONS #
    # ================ #

    @property
    def async_channel(self) -> AsyncChannel:
        """The demultiplexed asynchronous channel."""
        if self._async_channel is None:
            raise HiSLIPConnectionLost("the asynchronous channel is not open")
        return self._async_channel

    def set_srq_callback(self, callback: Optional[Callable[[int], None]]) -> None:
        """Register (or clear) a callback fired on AsyncServiceRequest.

        The callback receives the status byte carried by the service request.
        It runs on the channel reader thread and must not block or perform
        I/O on this instrument.
        """
        self.async_channel.set_srq_callback(callback)

    def close(self) -> None:
        self._sync.close()
        if self._async_channel is not None:
            self._async_channel.close()
            self._async_channel = None
        else:
            self._async.close()

    @property
    def timeout(self) -> float:
        """Timeout value in seconds for both the sync and async sockets"""
        return self._timeout

    @timeout.setter
    def timeout(self, val: float) -> None:
        """Timeout value in seconds for both the sync and async sockets"""
        self._timeout = val
        self._sync.settimeout(self._timeout)
        # The async socket itself stays blocking — it is read by the channel
        # thread — so the timeout applies to waiting for a response instead.
        if self._async_channel is not None:
            self._async_channel.timeout = val

    @property
    def max_msg_size(self) -> int:
        """Maximum HiSLIP message size in bytes."""
        return self._max_msg_size

    @max_msg_size.setter
    def max_msg_size(self, size: int) -> None:
        self._max_msg_size = self.async_maximum_message_size(size)

    @property
    def last_message_id(self) -> Optional[int]:
        return self._last_message_id

    @last_message_id.setter
    def last_message_id(self, message_id: Optional[int]) -> None:
        """Re-set last message id and related attributes"""
        with self._state_lock:
            self._last_message_id = message_id
            self._rmt = 0
            self._payload_remaining = 0
            self._msg_type = ""

    def _consume_send_state(self) -> Tuple[int, int]:
        """Claim the RMT flag and message id for a message about to be sent.

        The RMT-delivered flag rides on both synchronous messages and on
        AsyncStatusQuery, and the server tracks it to decide whether the
        previous response was consumed. Reading and clearing it has to be
        atomic, or a status query racing a write delivers the flag twice (or
        loses it) and the instrument answers the next command with
        "-410 Query INTERRUPTED".

        """
        # IVI-6.1 3.1.2 rule 3: any whole or partial server message still
        # buffered is stale once we send, so drop it rather than let it be
        # parsed as the next response. Only needed when a previous read was
        # abandoned part way; the normal read-to-completion path has nothing
        # outstanding.
        with self._state_lock:
            stale = self._payload_remaining > 0 or self._msg_type not in ("", "DataEnd")
        if stale:
            self._discard_sync_input()

        with self._state_lock:
            rmt, message_id = self._rmt, self._message_id
            self._rmt = 0
            self._last_message_id = message_id
            self._payload_remaining = 0
            self._msg_type = ""
            self._message_id = (message_id + 2) & 0xFFFF_FFFF
        return rmt, message_id

    def _consume_status_state(self) -> Tuple[int, int]:
        """As :meth:`_consume_send_state`, for an AsyncStatusQuery.

        A status query reports the RMT flag but does not consume a message
        id, so the id is not advanced. The id it carries is the *most recently
        sent* Data/DataEND/Trigger, not the next one: per IVI-6.1 6.14.3 the
        server compares it with the last message it received and reports MAV
        false when they differ, so sending the next id suppresses MAV on every
        status query.

        """
        with self._state_lock:
            rmt = self._rmt
            message_id = self.most_recent_message_id
            self._rmt = 0
        return rmt, message_id

    @property
    def most_recent_message_id(self) -> int:
        """MessageID of the last Data/DataEND/Trigger sent on this connection.

        AsyncStatusQuery, AsyncLock release and AsyncRemoteLocalControl all
        name it, and all use :data:`PRE_INITIAL_MESSAGE_ID` until this client
        has sent one.

        """
        last = self._last_message_id
        return PRE_INITIAL_MESSAGE_ID if last is None else last

    @property
    def keepalive(self) -> bool:
        """Status of the TCP keepalive.

        Keepalive is on/off for both the sync and async sockets

        If a connection is dropped as a result of “keepalives”, the error code
        VI_ERROR_CONN_LOST is returned to current and subsequent I/O
        calls on the session.

        """
        return self._keepalive

    @keepalive.setter
    def keepalive(self, keepalive: bool) -> None:
        self._keepalive = bool(keepalive)
        set_keepalive(self._sync, self._keepalive)
        set_keepalive(self._async, self._keepalive)

    def send(self, data: BytesBuffer, end: bool = True) -> int:
        """Send the data on the synchronous channel.

        More than one packet may be necessary in order
        to not exceed max_payload_size.

        Parameters
        ----------
        end : bool, optional
            Whether to mark the end of the message, which HiSLIP does by
            sending the final chunk as a DataEND rather than a Data message.
            This is the transport's equivalent of asserting EOI on GPIB, so
            it follows VI_ATTR_SEND_END_EN.

        """
        # print(f"send({data=})")  # uncomment for debugging
        self._await_async_interrupted()
        data_view = memoryview(data)
        num_bytes_to_send = len(data)
        max_payload_size = self._max_msg_size - HEADER_SIZE

        # send the data in chunks of max_payload_size bytes at a time
        while num_bytes_to_send > 0:
            if num_bytes_to_send <= max_payload_size:
                assert len(data_view) == num_bytes_to_send
                if end:
                    self._send_data_end_packet(data_view)
                else:
                    self._send_data_packet(data_view)
                bytes_sent = num_bytes_to_send
            else:
                self._send_data_packet(data_view[:max_payload_size])
                bytes_sent = max_payload_size

            data_view = data_view[bytes_sent:]
            num_bytes_to_send -= bytes_sent

        return len(data)

    def receive(self, max_len: int = 4096) -> bytes:
        """Receive data on the synchronous channel.

        Terminate after max_len bytes or after receiving a DataEnd message
        """

        # print(f"receive({max_len=})")  # uncomment for debugging

        # receive data, terminating after len(recv_buffer) bytes or
        # after receiving a DataEnd message.
        #
        # note the use of receive_exact_into (which calls socket.recv_into),
        # avoiding unnecessary copies.
        #
        self._receiving.set()
        try:
            recv_buffer = bytearray(max_len)
            view = memoryview(recv_buffer)
            bytes_recvd = 0

            while bytes_recvd < max_len:
                if self._payload_remaining <= 0:
                    if self._msg_type == "DataEnd":
                        # truncate to the actual number of bytes received
                        recv_buffer = recv_buffer[:bytes_recvd]
                        break
                    self._msg_type, self._payload_remaining = self._next_data_header()

                request_size = min(self._payload_remaining, max_len - bytes_recvd)
                receive_exact_into(self._sync, view[:request_size])
                self._payload_remaining -= request_size
                bytes_recvd += request_size
                view = view[request_size:]

            if bytes_recvd > max_len:
                raise MemoryError("scribbled past end of recv_buffer")

            # if there is no data remaining, set the RMT flag
            if self._payload_remaining == 0 and self._msg_type == "DataEnd":
                #
                # From IEEE Std 488.2: Response Message Terminator.
                #
                # RMT is the new-line accompanied by END sent from the server
                # to the client at the end of a response. Note that with HiSLIP
                # this is implied by the DataEND message.
                #
                with self._state_lock:
                    self._rmt = 1

            return bytes(recv_buffer)
        finally:
            self._receiving.clear()

    def _next_data_header(self) -> Tuple[str, int]:
        """
        receive the next data header (either Data or DataEnd), check the
        message_id, and return the msg_type and payload_length.
        """
        while True:
            try:
                header = RxHeader(self._sync)
            except HiSLIPUnrecognizedMessage as unknown:
                # IVI-6.1 6.3: discard it, answer Error, carry on.
                receive_flush(self._sync, unknown.payload_length)
                self.report_unrecognized_message()
                continue
            except HiSLIPSynchronizationLost:
                # IVI-6.1 6.2: FatalError on both channels, then close.
                self.report_fatal_error("Poorly formed message header")
                raise

            if header.msg_type in ("Data", "DataEnd"):
                # When receiving Data messages if the MessageID is not 0xffff ffff,
                # then verify that the MessageID indicated in the Data message is
                # the MessageID that the client sent to the server with the most
                # recent Data, DataEND or Trigger message.
                #
                # If the MessageIDs do not match, the client shall clear any Data
                # responses already buffered and discard the offending Data message

                if (
                    header.message_parameter == 0xFFFF_FFFF
                    or header.message_parameter == self.last_message_id
                ):
                    break

            if header.msg_type == "Interrupted":
                # The server abandoned the response. IVI-6.1 3.1.2 rule 4: if
                # we see this before the matching AsyncInterrupted, we must
                # not send anything more until that arrives.
                self._interrupted_pending = True
                raise HiSLIPInterruptedError(header.message_parameter)

            if header.msg_type in ("Error", "FatalError"):
                # The server is refusing the transaction — there is no
                # dedicated message for that, so this is the only way it can
                # say so. Discarding it here would leave the read waiting for
                # a reply that is never coming, and the caller would see a
                # timeout instead of the reason.
                raise error_from_header(self._sync, header)

            # we're out of sync.  flush this message and continue.
            receive_flush(self._sync, header.payload_length)

        return header.msg_type, header.payload_length

    def device_clear(self) -> None:
        feature = self.async_device_clear()
        # Abandon pending messages and wait for in-process synchronous messages
        # to complete.
        time.sleep(DEVICE_CLEAR_SETTLE_TIME)
        # Discard whatever they left in the socket, otherwise the
        # DeviceClearAcknowledge below would be read out of a stale stream.
        self._discard_sync_input()
        # Indicate to server that synchronous channel is cleared out.
        self.device_clear_complete(feature)
        # reset messageID and resume normal operation
        self._reset_message_state()

    def report_unrecognized_message(self) -> None:
        """Answer an unrecognized synchronous message with an Error.

        Required by IVI-6.1 6.3, on the channel the message arrived on.
        """
        try:
            self.error("Unrecognized Message Type")
        except OSError:
            LOGGER.debug("could not report the unrecognized message", exc_info=True)

    def report_fatal_error(self, error: str) -> None:
        """Send a FatalError on both channels, as IVI-6.1 6.2 requires."""
        payload = error.encode()
        code = FATALERRORCODE.get(error, 0)
        for sock in (self._sync, self._async):
            try:
                send_msg(sock, "FatalError", code, 0, payload)
            except OSError:
                LOGGER.debug("could not send FatalError", exc_info=True)

    def _await_async_interrupted(self) -> None:
        """Hold off sending until a pending Interrupted has been paired.

        IVI-6.1 3.1.2 rule 4. Bounded by the session timeout: a server that
        never sends the AsyncInterrupted should not wedge the client forever.
        """
        if not self._interrupted_pending:
            return
        self._interrupted_pending = False
        channel = self._async_channel
        if channel is None:
            return
        if not channel.wait_for_async_interrupted(min(self._timeout or 5.0, 5.0)):
            LOGGER.debug("no AsyncInterrupted followed the Interrupted message")
        channel.clear_async_interrupted()

    def _reset_message_state(self) -> None:
        """Return the message bookkeeping to its just-connected state."""
        with self._state_lock:
            self._message_id = INITIAL_MESSAGE_ID
            self._last_message_id = None
            self._rmt = 0
            self._payload_remaining = 0
            self._msg_type = ""
            self._interrupted_pending = False

    def _discard_sync_input(self) -> None:
        """Drop anything sitting in the synchronous socket's receive buffer."""
        self._sync.setblocking(False)
        try:
            while True:
                try:
                    if not self._sync.recv(65536):
                        break
                except BlockingIOError:
                    break
                except OSError:
                    break
        finally:
            self._sync.setblocking(True)
            self._sync.settimeout(self._timeout)

    def _read_sync_until(
        self, msg_type: str, timeout: Optional[float] = None
    ) -> RxHeader:
        """Read sync-channel messages until one of *msg_type* arrives.

        Anything else — an ``Interrupted`` left over from a device clear, say
        — is discarded. Waiting for the wanted message this way rather than
        expecting it to be next avoids both a fixed settling delay and a
        spurious protocol error when the server interleaves something.

        """
        saved_timeout = self._sync.gettimeout()
        if timeout is not None:
            self._sync.settimeout(timeout)
        try:
            while True:
                header = RxHeader(self._sync)
                if header.msg_type in ("Error", "FatalError") and msg_type not in (
                    "Error",
                    "FatalError",
                ):
                    raise error_from_header(self._sync, header)
                if header.payload_length > 0:
                    receive_flush(self._sync, header.payload_length)
                if header.msg_type == msg_type:
                    return header
        finally:
            self._sync.settimeout(saved_timeout)

    def terminate(self) -> None:
        """Cancel a pending I/O operation on the synchronous channel.

        Implements viTerminate() for HiSLIP sessions.  Writes to the cancel
        pipe, which causes any blocked recv_into() in the CancellableSocket
        to return immediately with HiSLIPInterruptedError (mapped to
        VI_ERROR_ABORT at the session layer).

        Thread-safe: may be called from any thread while another thread is
        blocked in receive().

        If no receive() is currently in progress, this is a no-op (matching
        the behavior of Keysight VISA's viTerminate on idle sessions).

        After the blocked operation returns, the caller MUST call
        complete_terminate() to reset the HiSLIP protocol state before
        performing further I/O on this session.
        """
        if not self._receiving.is_set():
            return
        self._sync.cancel()

    def complete_terminate(self) -> None:
        """Reset HiSLIP protocol state after terminate().

        Must be called after terminate() and after the blocked I/O thread
        has returned.  Performs a full HiSLIP device clear to re-sync the
        synchronous channel:

        1. Drain the cancel pipe (so it doesn't interfere with reads)
        2. Drain any partial/garbled data from the sync socket buffer
        3. Full HiSLIP AsyncDeviceClear → Interrupted → DeviceClearComplete
        4. Reset message counters
        """
        # 1. Drain the cancel pipe
        self._sync.drain_cancel()

        # Disable cancellation for all cleanup I/O — we don't want the
        # cancel pipe interfering with the device-clear handshake.
        self._sync._cancel_enabled = False
        try:
            # 2. Drain any bytes left in the sync socket buffer.
            #    After terminate() interrupted a read mid-stream, there may be
            #    partial HiSLIP message data in the buffer.
            self._discard_sync_input()

            # 3. Full device clear: AsyncDeviceClear → DeviceClearComplete →
            #    DeviceClearAcknowledge. The server may or may not send an
            #    Interrupted along the way; device_clear_complete skips it
            #    rather than this waiting a fixed period for one that, on many
            #    instruments, never comes.
            feature = self.async_device_clear()
            self.device_clear_complete(feature)

            # Drop an Interrupted that arrived after the acknowledge, so the
            # next read does not mistake it for an abort.
            self._discard_sync_input()
        finally:
            self._sync._cancel_enabled = True

        # 4. Reset all protocol state
        self._reset_message_state()

    def initialize(
        self,
        version: tuple = (1, 0),
        vendor_id: bytes = b"xx",
        sub_address: bytes = b"hislip0",
    ) -> InitializeResponse:
        """
        perform an Initialize transaction.
        returns the InitializeResponse header.
        """
        major, minor = version
        header = struct.pack(
            "!2sBBBB2sQ",
            b"HS",
            MESSAGETYPE["Initialize"],
            0,
            major,
            minor,
            vendor_id,
            len(sub_address),
        )
        # txdecode(header, sub_address)  # uncomment for debugging
        self._sync.sendall(header + sub_address)
        return InitializeResponse(self._sync)

    def async_initialize(self, session_id: int) -> AsyncInitializeResponse:
        """
        perform an AsyncInitialize transaction.
        returns the AsyncInitializeResponse header.
        """
        send_msg(self._async, "AsyncInitialize", 0, session_id)
        return AsyncInitializeResponse(self._async)

    def async_maximum_message_size(self, size: int) -> int:
        """
        perform an AsyncMaxMsgSize transaction.
        returns the max_msg_size from the AsyncMaxMsgSizeResponse packet.
        """
        # maximum_message_size transaction:
        #     C->S: AsyncMaxMsgSize
        #     S->C: AsyncMaxMsgSizeResponse
        payload = struct.pack("!Q", size)
        response = self.async_channel.transaction("AsyncMaxMsgSize", 0, 0, payload)
        return AsyncMaxMsgSizeResponse(response).max_msg_size

    def async_lock_info(self) -> int:
        """
        perform an AsyncLockInfo transaction.
        returns the exclusive_lock from the AsyncLockInfoResponse packet.
        """
        # async_lock_info transaction:
        #     C->S: AsyncLockInfo
        #     S->C: AsyncLockInfoResponse
        response = self.async_channel.transaction("AsyncLockInfo", 0, 0)
        return AsyncLockInfoResponse(response).exclusive_lock

    def async_lock_request(self, timeout: float, lock_string: str = "") -> str:
        """
        perform an AsyncLock request transaction.

        An empty ``lock_string`` requests an exclusive lock, anything else
        requests a shared lock under that name.  ``timeout`` is the time in
        seconds the server may spend waiting for the lock to become available;
        we wait a little longer than that for its answer.

        returns the lock_response from the AsyncLockResponse packet.
        """
        # async_lock transaction:
        #     C->S: AsyncLock
        #     S->C: AsyncLockResponse
        ctrl_code = LOCKCONTROLCODE["request"]
        timeout_ms = int(1e3 * timeout)
        response = self.async_channel.transaction(
            "AsyncLock",
            ctrl_code,
            timeout_ms,
            lock_string.encode(),
            timeout=timeout + self.async_channel.timeout,
        )
        return AsyncLockResponse(response).lock_response

    def async_lock_release(self) -> str:
        """
        perform an AsyncLock release transaction.
        returns the lock_response from the AsyncLockResponse packet.
        """
        # async_lock transaction:
        #     C->S: AsyncLock
        #     S->C: AsyncLockResponse
        ctrl_code = LOCKCONTROLCODE["release"]
        response = self.async_channel.transaction(
            "AsyncLock", ctrl_code, self.most_recent_message_id
        )
        return AsyncLockResponse(response).lock_response

    def async_remote_local_control(self, remotelocalcontrol: str) -> None:
        """
        perform an AsyncRemoteLocalControl transaction.
        """
        # remote_local transaction:
        #     C->S: AsyncRemoteLocalControl
        #     S->C: AsyncRemoteLocalResponse
        ctrl_code = REMOTELOCALCONTROLCODE[remotelocalcontrol]
        AsyncRemoteLocalResponse(
            self.async_channel.transaction(
                "AsyncRemoteLocalControl", ctrl_code, self.most_recent_message_id
            )
        )

    def async_status_query(self) -> int:
        """
        perform an AsyncStatusQuery transaction.
        returns the server_status from the AsyncStatusResponse packet.
        """
        # async_status_query transaction:
        #     C->S: AsyncStatusQuery
        #     S->C: AsyncStatusResponse
        #
        # The MessageID carried here is the id the next Data/DataEND/Trigger
        # message will use, matching other HiSLIP client implementations.
        rmt, message_id = self._consume_status_state()
        response = self.async_channel.transaction("AsyncStatusQuery", rmt, message_id)
        return AsyncStatusResponse(response).server_status

    def async_device_clear(self) -> int:
        """
        perform an AsyncDeviceClear transaction.
        returns the feature_bitmap from the AsyncDeviceClearAcknowledge packet.
        """
        response = self.async_channel.transaction("AsyncDeviceClear", 0, 0)
        return AsyncDeviceClearAcknowledge(response).feature_bitmap

    def device_clear_complete(self, feature_bitmap: int) -> int:
        """
        perform a DeviceClear transaction.
        returns the feature_bitmap from the DeviceClearAcknowledge packet.
        """
        send_msg(self._sync, "DeviceClearComplete", feature_bitmap, 0)
        # The server may still be sending an Interrupted for a message that
        # was in flight, so skip over anything that is not the acknowledge.
        return self._read_sync_until("DeviceClearAcknowledge").control_code

    def trigger(self) -> None:
        """send a Trigger packet on the sync channel"""
        self._await_async_interrupted()
        rmt, message_id = self._consume_send_state()
        send_msg(self._sync, "Trigger", rmt, message_id)

    def _send_data_packet(self, payload: BytesBuffer) -> None:
        """send a Data packet on the sync channel"""
        rmt, message_id = self._consume_send_state()
        send_msg(self._sync, "Data", rmt, message_id, payload)

    def _send_data_end_packet(self, payload: BytesBuffer) -> None:
        """send a DataEnd packet on the sync channel"""
        rmt, message_id = self._consume_send_state()
        send_msg(self._sync, "DataEnd", rmt, message_id, payload)

    def fatal_error(self, error: str, error_message: str = "") -> None:
        err_msg = (error_message or error).encode()
        send_msg(self._sync, "FatalError", FATALERRORCODE[error], 0, err_msg)

    def error(self, error: str, error_message: str = "") -> None:
        err_msg = (error_message or error).encode()
        send_msg(self._sync, "Error", ERRORCODE[error], 0, err_msg)


# the following two routines are only used for debugging.
# they are commented out because their f-strings use a feature
# that is a syntax error in Python versions < 3.7

# def rxdecode(header):
#     (
#         prologue,
#         msg_type,
#         control_code,
#         message_parameter,
#         payload_length,
#     ) = struct.unpack(HEADER_FORMAT, header)
#
#     msg_type = MESSAGETYPE_STR[msg_type]
#     print(
#         f"Rx: {prologue=}, "
#         f"{msg_type=}, "
#         f"{control_code=}, "
#         f"{message_parameter=}, "
#         f"{payload_length=}"
#     )


# def txdecode(header, payload=b""):
#     (
#         prologue,
#         msg_type,
#         control_code,
#         message_parameter,
#         payload_length,
#     ) = struct.unpack(HEADER_FORMAT, header)
#
#     msg_type = MESSAGETYPE_STR[msg_type]
#     print(
#         f"Tx: {prologue=}, "
#         f"{msg_type=}, "
#         f"{control_code=}, "
#         f"{message_parameter=}, "
#         f"{payload_length=}, "
#         f"{len(payload)=}, "
#         f"{bytes(payload[:20]).decode('iso-8859-1')!r}"
#     )
