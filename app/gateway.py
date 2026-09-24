from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Literal

import serial

from .protocol import normalize_mac

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayEvent:
    kind: Literal["scan", "connected", "disconnected", "notify", "error", "warning", "info"]
    mac: str | None = None
    payload: bytes | None = None
    handle: int | None = None
    rssi: int | None = None
    message: str | None = None
    addr_id: int | None = None
    addr_type: int | None = None


def parse_gateway_line(line: str) -> GatewayEvent | None:
    line = line.strip()
    if line.startswith("+SC_NTF:"):
        parts = line.removeprefix("+SC_NTF:").split(",")
        if len(parts) >= 10:
            try:
                return GatewayEvent(
                    "scan",
                    normalize_mac(parts[0]),
                    rssi=int(parts[4]),
                    addr_id=int(parts[1]),
                    addr_type=int(parts[2]),
                )
            except (ValueError, IndexError):
                return None
    if line.startswith("+NOTIFY:"):
        parts = line.removeprefix("+NOTIFY:").split(",", 6)
        mac = None
        try:
            mac = normalize_mac(parts[1])
        except (ValueError, IndexError):
            pass
        if len(parts) == 7:
            try:
                # This line parser consumes ASCII/hex text, never raw HEX mode.
                if int(parts[4]) != 0 or int(parts[5]) < 0:
                    raise ValueError("unsupported NOTIFY type or length")
                if not 0 <= int(parts[0]) < 65535 or not 0 <= int(parts[2]) <= 65535:
                    raise ValueError("invalid NOTIFY handle")
                if len(parts[3]) not in {4, 32}:
                    raise ValueError("invalid characteristic UUID")
                bytes.fromhex(parts[3])
                payload_text = "".join(parts[6].split())
                payload = bytes.fromhex(payload_text) if payload_text else b""
                if len(payload) != int(parts[5]):
                    raise ValueError("NOTIFY length mismatch")
                return GatewayEvent(
                    "notify",
                    normalize_mac(parts[1]),
                    payload=payload,
                    handle=int(parts[0]),
                )
            except (ValueError, IndexError):
                pass
        return GatewayEvent("warning", mac, message=f"Malformed NOTIFY: {line[:160]}")
    if line.startswith("+DISCON:"):
        parts = line.removeprefix("+DISCON:").split(",")
        try:
            # Field firmware: role,handle,mac,reason. Older firmware: handle,mac,reason.
            if len(parts) == 4:
                _, handle, mac, reason = parts
            elif len(parts) in {2, 3}:
                handle, mac = parts[:2]
                reason = parts[2] if len(parts) == 3 else ""
            else:
                return None
            return GatewayEvent("disconnected", normalize_mac(mac), handle=int(handle),
                                message=f"DISCONNECT (BLE code {reason})" if reason else None)
        except ValueError:
            return None
    if line.startswith("+CONN:"):
        parts = line.removeprefix("+CONN:").split(",")
        try:
            if len(parts) >= 5 and len(parts[2]) == 12:
                mac = normalize_mac(parts[2])
                if parts[-1] in {"DISSCONNECT", "TIMEOUT", "SERVICE_NOT_FOUND", "CCCD_ERROR", "CNN_BUSY"}:
                    code = parts[3] if parts[-1] == "DISSCONNECT" and parts[3] != "0" else ""
                    message = f"{parts[-1]} (BLE code {code})" if code else parts[-1]
                    return GatewayEvent("error", mac, message=message)
                if int(parts[1]) == 65535:
                    return GatewayEvent("error", mac, message=f"CONN_FAILED: {line}")
                return GatewayEvent("connected", mac, handle=int(parts[1]))
            if len(parts) >= 4 and len(parts[1]) == 12:
                return GatewayEvent("connected", normalize_mac(parts[1]), handle=int(parts[0]))
        except ValueError:
            return None
    return None


class LineBuffer:
    """Preserve incomplete lines across serial read timeouts."""

    def __init__(self) -> None:
        self.pending = bytearray()

    def feed(self, raw: bytes) -> list[str]:
        self.pending.extend(raw)
        lines = []
        while b"\n" in self.pending:
            line, _, rest = self.pending.partition(b"\n")
            self.pending = bytearray(rest)
            if line.strip():
                lines.append(line.decode("ascii", errors="backslashreplace").strip())
        if len(self.pending) > 65536:
            logger.error("RX line exceeds 64 KiB; discarding incomplete data")
            self.pending.clear()
        return lines


class SerialGateway:
    COMMAND_TIMEOUT_SECONDS = 5.0
    IDEMPOTENT_RETRIES = 2
    DISCONNECT_TIMEOUT_SECONDS = 15.0
    TX_IDLE_SECONDS = 0.02
    CONNECTION_PROFILES = (
        # Verified by static inspection of the user's working v0.4 EXE.
        ("v0.4配对连接/扫描地址", 247, 1, True),
        ("普通连接/自动地址", 247, 0, False),
        ("普通连接/扫描地址", 247, 0, True),
        ("配对连接/自动地址", 247, 1, False),
    )

    def __init__(self, port: str, baudrate: int, connect_timeout_seconds: int = 40) -> None:
        self.port_name = port
        self.baudrate = baudrate
        self.connect_timeout_seconds = connect_timeout_seconds
        self.events: Queue[GatewayEvent] = Queue()
        self._serial: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._commands: Queue[tuple[str, str | None]] = Queue()
        self._running = threading.Event()
        self._response = threading.Condition()
        self._pending_command: str | None = None
        self._pending_mac: str | None = None
        self._connection_result: GatewayEvent | None = None
        self._terminal: str | None = None
        self._cnni_payload: list[str] = []
        self._cnni_live: dict[tuple[str, int], GatewayEvent] = {}
        self._cnni_listing: dict[str, GatewayEvent] = {}
        self._cnni_closed: set[str] = set()
        self._cnni_current: str | None = None
        self._unsolicited_ok_pending = False
        self._busy = False
        self._faulted = False
        self._desynced = False
        self._resync_failures = 0
        self._last_transaction_success = False
        self._connection_snapshot_complete = False
        self._pending_since = 0.0
        self._pending_non_ascii_start = 0
        self._collision_detected = False
        self.serial_collision_suspected = 0
        self.last_collision_command: str | None = None
        self.last_collision_at: str | None = None
        self._first_blocking_error: str | None = None
        self._scanning: bool | None = None
        self.scan_started_at: float | None = None
        self.first_fault: dict | None = None
        self.warning_count = 0
        self.last_warning: dict | None = None
        self.history: deque[dict] = deque(maxlen=50)
        self._connected: dict[str, int] = {}
        self._bad_notify_count = 0
        self._non_ascii_bytes = 0
        self._last_rx_at = 0.0
        self._rx_partial = False
        self._disconnect_retries = 0
        self._command_retries = 0
        self._query_response_seen = False
        self._scan_query_state: bool | None = None
        self._disconnect_accepted = False
        self.recent_lines: deque[str] = deque(maxlen=200)
        self.last_response_at: float | None = None
        self.addresses: dict[str, tuple[int, int]] = {}
        self._profile_cursor: dict[str, int] = {}
        self._preferred_profile: dict[str, int] = {}
        self._active_profile: dict[str, int] = {}

    @property
    def name(self) -> str:
        return f"EW-DTU02 {self.port_name}"

    @property
    def online(self) -> bool:
        return bool(self._serial and self._serial.is_open and self._running.is_set())

    @property
    def busy(self) -> bool:
        return self._busy or self._faulted or self._desynced or not self._commands.empty()

    @property
    def scanning(self) -> bool | None:
        return self._scanning

    @property
    def active_links(self) -> dict[str, int]:
        return self._connected

    def diagnostics(self) -> dict:
        return {
            "online": self.online,
            "busy": self.busy,
            "faulted": self._faulted,
            "desynced": self._desynced,
            "resync_failures": self._resync_failures,
            "scanning": self._scanning,
            "active_connections": len(self._connected),
            "first_blocking_error": self._first_blocking_error,
            "first_fault": self.first_fault,
            "warning_count": self.warning_count,
            "last_warning": self.last_warning,
            "event_history": list(self.history),
            "bad_notify_count": self._bad_notify_count,
            "non_ascii_bytes": self._non_ascii_bytes,
            "serial_collision_suspected": self.serial_collision_suspected,
            "last_collision_command": self.last_collision_command,
            "last_collision_at": self.last_collision_at,
            "disconnect_retries": self._disconnect_retries,
            "command_retries": self._command_retries,
            "pending_command": self._pending_command,
            "last_response_seconds_ago": round(time.monotonic() - self.last_response_at, 1) if self.last_response_at else None,
            "recent_lines": list(self.recent_lines)[-30:],
        }

    def _record(self, kind: str, message: str) -> dict:
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 "kind": kind, "message": message}
        self.history.append(entry)
        return entry

    def _replace_connection_snapshot(self, links: dict[str, int]) -> None:
        previous = set(self._connected)
        self._connected.clear()
        self._connected.update(links)
        for mac in previous - links.keys():
            self.events.put(GatewayEvent("disconnected", mac, message="连接状态同步确认已断开"))

    def _set_fault(self, message: str) -> None:
        self._faulted = True
        if self._first_blocking_error is None:
            self._first_blocking_error = message
        entry = self._record("fault", message)
        if self.first_fault is None:
            self.first_fault = entry

    def start(self) -> None:
        if self._serial and self._serial.is_open:
            return
        self._serial = serial.Serial(self.port_name, self.baudrate, timeout=0.2, write_timeout=1)
        self._running.set()
        self._reader = threading.Thread(target=self._read_loop, name="gateway-reader", daemon=True)
        self._reader.start()
        logger.info("Serial opened port=%s baudrate=%s connect_timeout=%ss", self.port_name, self.baudrate, self.connect_timeout_seconds)
        for command in ("AT+SCAN=0", "AT+OP?", "AT+CNNI="):
            self.send(command)
        self._worker = threading.Thread(target=self._command_loop, name="gateway-commands", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._running.clear()
        with self._response:
            self._response.notify_all()
        if self._worker:
            self._worker.join(timeout=2)
        if self._reader:
            self._reader.join(timeout=1)
        if self._serial:
            self._serial.close()
        self._scanning = False
        logger.info("Serial closed port=%s", self.port_name)

    def send(self, command: str) -> None:
        if not self._serial or not self._serial.is_open:
            raise RuntimeError("gateway serial port is not open")
        self._commands.put((command, None))

    def connect(self, mac: str) -> None:
        mac = normalize_mac(mac)
        timeout_ms = int(self.connect_timeout_seconds * 1000)
        index = self._preferred_profile.get(mac, self._profile_cursor.get(mac, 0))
        label, mtu, security, use_scanned_address = self.CONNECTION_PROFILES[index]
        address = self.addresses.get(mac) if use_scanned_address else None
        address_fields = f"{address[0]},{address[1]}" if address else ","
        self._active_profile[mac] = index
        logger.info("CONNECT mac=%s profile=%s address=%s", mac, label, address)
        command = f"AT+CONN={mac},{address_fields},{mtu},{timeout_ms},1,40,20,0,600"
        if security:
            command += ",1,1,0"
        # The worker owns the scan/collect phase transition. It confirms
        # AT+SCAN=0 before sending this connection request, then keeps scanning
        # disabled while notifications are being collected.
        self._commands.put((command, mac))

    def disconnect(self, mac: str) -> None:
        mac = normalize_mac(mac)
        self._commands.put((f"AT+DISCON=,{mac}", mac))

    def report_no_data(self, mac: str) -> None:
        """A link without samples has not validated its compatibility profile."""
        index = self._preferred_profile.pop(mac, self._profile_cursor.get(mac, 0))
        self._profile_cursor[mac] = (index + 1) % len(self.CONNECTION_PROFILES)
        self._record("no_data", f"{mac}; next_profile={self._profile_cursor[mac]}")

    def scan(self) -> None:
        if not self.busy and not self._connected and not self._scanning:
            self.send("AT+SCAN=1")

    def stop_scan(self) -> None:
        if not self.busy and self._scanning is not False:
            self.send("AT+SCAN=0")

    def poll(self, limit: int = 500) -> list[GatewayEvent]:
        result: list[GatewayEvent] = []
        for _ in range(limit):
            try:
                result.append(self.events.get_nowait())
            except Empty:
                break
        return result

    def _execute(self, command: str, mac: str | None) -> GatewayEvent | None:
        """Only one AT transaction can own the reply stream."""
        timeout = self.connect_timeout_seconds + 5 if command.startswith("AT+CONN=") else self.COMMAND_TIMEOUT_SECONDS
        if command.startswith("AT+DISCON="):
            timeout = self.DISCONNECT_TIMEOUT_SECONDS
        with self._response:
            if self._reader is not None and not self._wait_tx_gap():
                return None
            if command == "AT+SCAN=1":
                self.scan_started_at = time.monotonic()
            self._pending_command = command
            self._pending_mac = mac
            self._connection_result = None
            self._terminal = None
            self._cnni_payload = []
            self._cnni_live = {}
            self._cnni_listing = {}
            self._cnni_closed = set()
            self._cnni_current = None
            self._query_response_seen = False
            self._scan_query_state = None
            self._disconnect_accepted = False
            self._pending_since = time.perf_counter()
            self._pending_non_ascii_start = self._non_ascii_bytes
            self._collision_detected = False
            self._last_transaction_success = False
            self._connection_snapshot_complete = False
            self.recent_lines.append(f"TX {command}")
            logger.info("TX %s", command)
            assert self._serial is not None
            self._serial.write((command + "\r\n").encode("ascii"))
            deadline = time.monotonic() + timeout
            retries = 0
            while self._running.is_set() and self._terminal is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Retry only idempotent operations under the SAME reply
                    # owner. Never retry CONN or arbitrary writes. A parsed
                    # connection/list result missing its terminator still faults.
                    retryable = (
                        command.startswith("AT+DISCON=") and self._connection_result is None and not self._disconnect_accepted
                        or command in {"AT+SCAN=0", "AT+SCAN=1"}
                        or command in {"AT+OP?", "AT+SCAN?"} and not self._query_response_seen
                        or command == "AT+CNNI=" and not self._cnni_payload
                    )
                    if retryable and retries < self.IDEMPOTENT_RETRIES:
                        if self._reader is not None and not self._wait_tx_gap():
                            break
                        if self._terminal is not None or self._connection_result is not None:
                            deadline = time.monotonic() + timeout
                            continue
                        retries += 1
                        self._command_retries += 1
                        if command.startswith("AT+DISCON="):
                            self._disconnect_retries += 1
                        logger.warning("Retry same idempotent transaction attempt=%s command=%s", retries, command)
                        self._record("retry", f"{command}; attempt={retries}")
                        self.recent_lines.append(f"TX {command}")
                        self._serial.write((command + "\r\n").encode("ascii"))
                        deadline = time.monotonic() + timeout
                        continue
                    break
                self._response.wait(min(remaining, 0.2))
            if retries and self._terminal == "OK":
                # Retain reply ownership briefly for a duplicate command's
                # trailing ACK/ERROR; it must not terminate the next command.
                settle_until = time.monotonic() + .5
                while self._running.is_set() and time.monotonic() < settle_until:
                    self._response.wait(.05)
            if not self._running.is_set() and self._terminal is None:
                return None
            result, terminal = self._connection_result, self._terminal
            # Field firmware V1.5(2507081320) returns only +CNB:0 for
            # an empty connection list. Keep the full response window so an
            # optional trailing OK is consumed by this query, not the next one.
            if terminal is None and command == "AT+CNNI=" and self._cnni_payload == ["+CNB:0"]:
                terminal = "CNB_EMPTY"
                self._connection_snapshot_complete = True
                self._replace_connection_snapshot({})
                logger.info("AT+CNNI= completed: +CNB:0 without trailing OK; continuing startup")
            # Each listed link has its own OK. Hold the entire query window,
            # rather than releasing the next command after the first link.
            listed = len(self._cnni_listing)
            counts = [line for line in self._cnni_payload if line.startswith("+CNB:")]
            if (terminal is None and command == "AT+CNNI=" and 1 <= listed <= 7
                    and self._cnni_closed == set(self._cnni_listing)
                    and counts in ([], [f"+CNB:{listed}"])
                    and len({e.handle for e in self._cnni_listing.values()}) == listed
                    and not any(line.startswith("+DISCON:") for line in self._cnni_payload)
                    and sum(line.startswith("+CONN:") for line in self._cnni_payload) == listed):
                terminal = "CNB_LIST"
                snapshot = {event.mac: event.handle for event in self._cnni_listing.values()}
                self._replace_connection_snapshot(snapshot)
                for event in self._cnni_listing.values():
                    self.events.put(event)
                self._connection_snapshot_complete = True
                logger.info("AT+CNNI= completed: %s complete connection records", listed)
            # Some field firmware sends only CNB:N plus live notifications.
            # Require exactly N distinct MACs AND handles, with no conflicting
            # list/disconnect records; keep the full window to absorb trailing OKs.
            count = len(self._cnni_live)
            if (terminal is None and command == "AT+CNNI=" and 1 <= count <= 7
                    and self._cnni_payload == [f"+CNB:{count}"]
                    and len({mac for mac, _ in self._cnni_live}) == count
                    and len({handle for _, handle in self._cnni_live}) == count):
                terminal = "CNB_LIVE"
                snapshot = {mac: handle for (mac, handle) in self._cnni_live}
                self._replace_connection_snapshot(snapshot)
                for event in self._cnni_live.values():
                    self.events.put(GatewayEvent("connected", event.mac, handle=event.handle))
                self._connection_snapshot_complete = True
                logger.info("AT+CNNI= completed: %s links confirmed by live notifications", count)
            if command == "AT+CNNI=" and terminal == "OK" and self._cnni_payload == ["+CNB:0"]:
                terminal = "CNB_EMPTY"
                self._replace_connection_snapshot({})
                self._connection_snapshot_complete = True
            if command == "AT+CNNI=" and terminal == "OK":
                count = len(self._cnni_live)
                if (count and self._cnni_payload == [f"+CNB:{count}"]
                        and len({mac for mac, _ in self._cnni_live}) == count
                        and len({handle for _, handle in self._cnni_live}) == count):
                    self._replace_connection_snapshot({mac: handle for mac, handle in self._cnni_live})
                    for event in self._cnni_live.values():
                        self.events.put(GatewayEvent("connected", event.mac, handle=event.handle))
                    self._connection_snapshot_complete = True
            if self._collision_detected:
                terminal = None
                self._desynced = True
                self._record("transaction_fault", f"SERIAL_COLLISION_SUSPECTED: {command}")
            self._pending_command = None
            self._pending_mac = None
        if terminal is None:
            # A late reply cannot own the next transaction. Keep the reader
            # alive and recover reply ownership with AT+CNNI= in the worker.
            self._desynced = True
            message = (
                f"SERIAL_COLLISION_SUSPECTED: {command}; 控制指令期间检测到串口乱码"
                if self._collision_detected else
                f"AT_RESPONSE_TIMEOUT: {command}; 网关响应未完整返回，正在重新同步连接状态"
            )
            self._record("transaction_fault", message)
            logger.warning(message)
            self._last_transaction_success = False
            if mac:
                result = self._finish_connection(GatewayEvent("error", mac, message=message))
            return result
        self._last_transaction_success = terminal != "ERROR"
        if terminal != "ERROR":
            if command == "AT+CNNI=" and self._cnni_payload == ["+CNB:0"]:
                self._connected.clear()
            if command == "AT+SCAN=0":
                self._scanning = False
            elif command == "AT+SCAN=1":
                self._scanning = True
            elif command == "AT+SCAN?":
                self._scanning = self._scan_query_state
        if mac:
            if terminal == "ERROR" and (result is None or result.kind != "error"):
                result = GatewayEvent("error", mac, message=f"AT_ERROR: {command}")
            if command.startswith("AT+DISCON=") and (result is None or result.kind != "disconnected"):
                message = f"DISCONNECT_UNCONFIRMED: {command}; 正在重新同步连接状态"
                self._desynced = True
                self._record("transaction_fault", message)
                return self._finish_connection(GatewayEvent("error", mac, message=message))
            return self._finish_connection(result or GatewayEvent("error", mac, message="Missing connection result"))
        if terminal == "ERROR":
            message = f"AT_ERROR: {command}"
            logger.error("Command rejected: %s", command)
            self.events.put(GatewayEvent("error", message=message))
            if command in {"AT+SCAN=0", "AT+SCAN=1", "AT+SCAN?"}:
                self._set_fault(message)
        return None

    def _wait_tx_gap(self) -> bool:
        """Prefer an idle, complete-line gap on the half-duplex RS485 bus.

        USB buffering prevents a collision guarantee; bounded MAC-disconnect
        retries handle a lost command without retrying arbitrary AT operations.
        Called with the response lock, which wait releases for the reader.
        """
        deadline = time.monotonic() + 2.0
        while self._running.is_set():
            now = time.monotonic()
            if (not self._rx_partial and now - self._last_rx_at >= self.TX_IDLE_SECONDS
                    and self._serial is not None and self._serial.in_waiting == 0):
                return True
            if now >= deadline:
                logger.warning("No idle serial gap within 2s; sending pending command")
                return True
            self._response.wait(.005)
        return False

    def _confirm_scan_stopped(self) -> None:
        # Corrupted scan reports can leave an orphan OK on the RS485 bus.
        # Verify the typed state reply before any connection command follows.
        for _ in range(self.IDEMPOTENT_RETRIES + 1):
            self._execute("AT+SCAN=0", None)
            if self._faulted or not self._running.is_set():
                return
            if self._desynced and not self._resync_connections():
                return
            self._execute("AT+SCAN?", None)
            if self._faulted or not self._running.is_set():
                return
            if self._desynced and not self._resync_connections():
                return
            if self._scanning is False:
                return
        message = "SCAN_STOP_UNCONFIRMED: 网关仍在扫描，请检查串口链路并重新连接网关"
        self._set_fault(message)
        self.events.put(GatewayEvent("error", message=message))

    def _resync_connections(self) -> bool:
        """Recover a lost AT reply boundary from a complete connection list."""
        if not self._desynced:
            return True
        for attempt in range(1, self.IDEMPOTENT_RETRIES + 2):
            if not self._running.is_set() or self._faulted:
                return False
            time.sleep(0.5)
            self._execute("AT+CNNI=", None)
            if self._last_transaction_success and self._connection_snapshot_complete:
                self._desynced = False
                self._resync_failures = 0
                self._record("resync", f"AT+CNNI= confirmed {len(self._connected)} active connection(s)")
                logger.info("Serial command stream resynchronized active=%s", len(self._connected))
                return True
            self._resync_failures += 1
            logger.warning("AT+CNNI= resync failed attempt=%s", attempt)
        message = "AT_RESYNC_FAILED: 连续查询连接状态失败，请检查串口链路并重新连接网关"
        self._set_fault(message)
        self.events.put(GatewayEvent("error", message=message))
        return False

    def _command_loop(self) -> None:
        while self._running.is_set() and not self._faulted:
            try:
                command, mac = self._commands.get(timeout=0.2)
            except Empty:
                continue
            self._busy = True
            try:
                if command == "AT+SCAN=1" and self._connected:
                    logger.info(
                        "Skip scan start while BLE notifications are active connections=%s",
                        len(self._connected),
                    )
                    continue
                if mac and command.startswith("AT+CONN="):
                    if self._scanning is not False:
                        self._confirm_scan_stopped()
                    if self._faulted:
                        message = self._first_blocking_error or "AT+SCAN=0 failed"
                        self._finish_connection(
                            GatewayEvent("error", mac, message=f"SCAN_STOP_FAILED: {message}")
                        )
                        continue
                    if not self._running.is_set():
                        continue
                    self._execute(command, mac)
                elif command == "AT+SCAN=0":
                    self._confirm_scan_stopped()
                else:
                    self._execute(command, mac)
                if self._desynced:
                    self._resync_connections()
            except Exception as exc:
                message = str(exc)
                self._set_fault(message)
                logger.exception("Serial command failed")
                self.events.put(GatewayEvent("error", message=message))
            finally:
                self._busy = False
                self._commands.task_done()

    def _finish_connection(self, event: GatewayEvent) -> GatewayEvent:
        mac = event.mac
        if mac and event.kind == "connected":
            self._connected[mac] = event.handle if event.handle is not None else 0
            index = self._active_profile.pop(mac, None)
            if index is not None:
                self._preferred_profile[mac] = index
                self._profile_cursor[mac] = index
        elif mac and event.kind == "disconnected":
            self._connected.pop(mac, None)
        elif mac and event.kind == "error":
            index = self._active_profile.pop(mac, self._profile_cursor.get(mac, 0))
            self._preferred_profile.pop(mac, None)
            if "CNN_BUSY" not in (event.message or ""):
                self._profile_cursor[mac] = (index + 1) % len(self.CONNECTION_PROFILES)
            event.message = f"{event.message or '连接失败'} | {self.CONNECTION_PROFILES[index][0]}"
        logger.info("Connection result mac=%s kind=%s message=%s", mac, event.kind, event.message)
        self._record(event.kind, f"{mac}: {event.message or event.kind}")
        self.events.put(event)
        return event

    def _receive_line(self, line: str) -> None:
        self.last_response_at = time.monotonic()
        self.recent_lines.append(f"RX {line}")
        logger.info("RX %s", line)
        event = parse_gateway_line(line)
        if event and event.kind == "warning":
            self._bad_notify_count += 1
            self.warning_count += 1
            self.last_warning = self._record("warning", event.message or "Malformed NOTIFY")
        with self._response:
            if self._pending_command == "AT+OP?" and line.startswith("+OP:"):
                self._query_response_seen = True
            if self._pending_command == "AT+SCAN?" and line.startswith("+SCAN:"):
                fields = line.removeprefix("+SCAN:").split(",")
                if len(fields) == 6 and fields[0] in {"0", "1"} and all(p.isdigit() for p in fields):
                    self._scan_query_state = fields[0] == "1"
                    self._query_response_seen = True
            if self._pending_command == "AT+CNNI=" and line.startswith(("+CNB:", "+CONN:", "+SERV:", "+CHAR:", "+DISCON:")):
                self._cnni_payload.append(line)
            if (self._pending_command == "AT+CNNI=" and event and event.kind == "notify"
                    and event.mac and event.handle is not None and event.payload):
                self._cnni_live[(event.mac, event.handle)] = event
            if line.startswith(("+SC_NTF:", "+NOTIFY:", "+INDICATE:")):
                self._unsolicited_ok_pending = True
            if event and event.kind == "disconnected":
                self._connected.pop(event.mac, None)
                if not ((self._pending_command or "").startswith("AT+DISCON=")
                        and event.mac == self._pending_mac):
                    self._unsolicited_ok_pending = True
            if line == "OK" and self._unsolicited_ok_pending:
                self._unsolicited_ok_pending = False
                return
            if self._pending_command == "AT+CNNI=":
                if event and event.kind == "connected":
                    self._cnni_listing[event.mac] = event
                    self._cnni_current = event.mac
                    return
                if line == "OK":
                    if self._cnni_current is not None:
                        self._cnni_closed.add(self._cnni_current)
                        self._cnni_current = None
                    return
            expected = "disconnected" if (self._pending_command or "").startswith("AT+DISCON=") else "connected"
            if event and event.mac == self._pending_mac and self._pending_mac and event.kind in {expected, "error"}:
                self._connection_result = event
                return
            if line in {"OK", "ERROR"} and self._pending_command:
                if self._pending_command.startswith("AT+DISCON=") and self._connection_result is None:
                    # Field firmware acknowledges immediately but reports the
                    # actual disconnect after its BLE timeout (~6s). A duplicate
                    # DISCON can return ERROR while that operation still runs.
                    # Keep ownership and wait for matching DISCON + its OK.
                    self._disconnect_accepted = True
                    return
                if line == "OK" and self._pending_command in {"AT+OP?", "AT+SCAN?"} and not self._query_response_seen:
                    return
                # Unsolicited scan/notify OKs cannot complete a connection
                # before the matching +CONN result has arrived.
                if self._terminal is None and (line == "ERROR" or not self._pending_mac or self._connection_result):
                    self._terminal = line
                    self._response.notify_all()
                return
        if event:
            if event.kind == "scan" and event.mac and event.addr_id is not None and event.addr_type is not None:
                self._scanning = True
                self.addresses[event.mac] = (event.addr_id, event.addr_type)
            elif event.kind == "connected" and event.mac:
                self._connected[event.mac] = event.handle if event.handle is not None else 0
            elif event.kind == "disconnected" and event.mac:
                self._connected.pop(event.mac, None)
            self.events.put(event)

    def _read_loop(self) -> None:
        assert self._serial is not None
        buffer = LineBuffer()
        reported_partial = b""
        while self._running.is_set():
            try:
                raw = self._serial.read(max(1, min(self._serial.in_waiting, 4096)))
                if raw:
                    self._last_rx_at = time.monotonic()
                    self._rx_partial = True
                non_ascii = sum(byte > 0x7F for byte in raw)
                if non_ascii:
                    self._non_ascii_bytes += non_ascii
                    logger.warning(
                        "RX non-ASCII bytes count=%s total=%s hex=%s",
                        non_ascii,
                        self._non_ascii_bytes,
                        raw.hex()[:320],
                    )
                    with self._response:
                        if (not self._collision_detected
                                and (self._pending_command or "").startswith("AT+CONN=")
                                and time.monotonic() - self._pending_since <= 0.5
                                and self._non_ascii_bytes - self._pending_non_ascii_start >= 4):
                            self._collision_detected = True
                            self.serial_collision_suspected += 1
                            self.last_collision_command = self._pending_command
                            self.last_collision_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                            message = f"SERIAL_COLLISION_SUSPECTED: {self._pending_command}"
                            self._record("collision", message)
                            logger.error(message)
                for line in buffer.feed(raw):
                    self._receive_line(line)
                self._rx_partial = bool(buffer.pending.strip())
                if not raw and buffer.pending and bytes(buffer.pending) != reported_partial:
                    reported_partial = bytes(buffer.pending)
                    logger.warning("RX incomplete line (retained) hex=%s", reported_partial.hex())
                elif not buffer.pending:
                    reported_partial = b""
            except Exception as exc:
                logger.exception("Serial reader failed")
                message = str(exc)
                self.events.put(GatewayEvent("error", message=message))
                self._set_fault(message)
                self._running.clear()
                with self._response:
                    self._response.notify_all()


class SimulatorGateway:
    def __init__(self, mac_provider) -> None:
        self._mac_provider = mac_provider
        self.connected: dict[str, int] = {}
        self._last_emit = 0.0
        self._started = False
        self._sequence = 0
        self.events: Queue[GatewayEvent] = Queue()

    @property
    def name(self) -> str:
        return "内置模拟网关"

    @property
    def online(self) -> bool:
        return self._started

    def diagnostics(self) -> dict:
        return {"online": self.online, "last_response_seconds_ago": 0, "recent_lines": []}

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        self.connected.clear()

    def connect(self, mac: str) -> None:
        mac = normalize_mac(mac)
        if mac not in self.connected:
            handle = next((i for i in range(7) if i not in self.connected.values()), 0)
            self.connected[mac] = handle
            self.events.put(GatewayEvent("connected", mac, handle=handle))

    def disconnect(self, mac: str) -> None:
        mac = normalize_mac(mac)
        handle = self.connected.pop(mac, None)
        self.events.put(GatewayEvent("disconnected", mac, handle=handle))

    def scan(self) -> None:
        return None

    def poll(self, limit: int = 500) -> list[GatewayEvent]:
        if self._started:
            now = time.monotonic()
            if now - self._last_emit >= 0.2:
                self._last_emit = now
                for mac in self._mac_provider():
                    self.events.put(GatewayEvent("scan", normalize_mac(mac), rssi=random.randint(-76, -42)))
                for mac, handle in list(self.connected.items()):
                    self.events.put(GatewayEvent("notify", mac, payload=self._frame(mac), handle=handle))
        result: list[GatewayEvent] = []
        for _ in range(limit):
            try:
                result.append(self.events.get_nowait())
            except Empty:
                break
        return result

    def _frame(self, mac: str) -> bytes:
        self._sequence += 1
        phase = self._sequence / 9.0 + int(mac[-2:], 16)
        velocity = [int(8 + 7 * abs(math.sin(phase + axis))) for axis in (0, 1.2, 2.1)]
        angles = [int(value / 180.0 * 32768) for value in (2.5, 1.2, 0.6)]
        temperature = int((27.0 + 2.2 * math.sin(phase / 30)) * 100)
        displacement = [int(value * 8 + random.randint(0, 4)) for value in velocity]
        frequency = [15, 15, 15]
        values = velocity + angles + [temperature] + displacement + frequency
        frame = bytearray(b"\x55\x61")
        for index, value in enumerate(values):
            signed = index < 10
            frame.extend(int(value).to_bytes(2, "little", signed=signed))
        # The physical WTVB01-BT50 notification is 32 bytes. The last four
        # bytes are outside the 13 measurement fields consumed by the parser.
        frame.extend(b"\x00\x00\x00\x00")
        return bytes(frame)
