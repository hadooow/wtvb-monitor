from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Literal

import serial

from .protocol import normalize_mac


@dataclass(slots=True)
class GatewayEvent:
    kind: Literal["scan", "connected", "disconnected", "notify", "error", "info"]
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
        if len(parts) == 7:
            try:
                payload_text = "".join(parts[6].split())
                payload = bytes.fromhex(payload_text) if payload_text else b""
                return GatewayEvent(
                    "notify",
                    normalize_mac(parts[1]),
                    payload=payload,
                    handle=int(parts[0]),
                )
            except (ValueError, IndexError):
                return GatewayEvent("error", message=f"Malformed NOTIFY: {line[:160]}")
    if line.startswith("+DISCON:"):
        parts = line.removeprefix("+DISCON:").split(",")
        if len(parts) >= 2:
            try:
                return GatewayEvent("disconnected", normalize_mac(parts[1]), handle=int(parts[0]))
            except ValueError:
                return None
    if line.startswith("+CONN:"):
        parts = line.removeprefix("+CONN:").split(",")
        try:
            if len(parts) >= 5 and len(parts[2]) == 12:
                mac = normalize_mac(parts[2])
                if parts[-1] in {"DISSCONNECT", "TIMEOUT", "SERVICE_NOT_FOUND", "CCCD_ERROR", "CNN_BUSY"}:
                    code = parts[3] if len(parts) >= 5 else ""
                    message = f"{parts[-1]} (BLE code {code})" if code else parts[-1]
                    return GatewayEvent("error", mac, message=message)
                return GatewayEvent("connected", mac, handle=int(parts[1]))
            if len(parts) >= 4 and len(parts[1]) == 12:
                return GatewayEvent("connected", normalize_mac(parts[1]), handle=int(parts[0]))
        except ValueError:
            return None
    return None


class SerialGateway:
    CONNECTION_PROFILES = (
        ("安全连接/扫描地址", 247, 1, True),
        ("安全连接/自动地址", 247, 1, False),
        ("普通连接/自动地址", 247, 0, False),
        ("安全兼容/MTU23", 23, 1, False),
        ("普通兼容/MTU23", 23, 0, False),
    )

    def __init__(self, port: str, baudrate: int, connect_timeout_seconds: int = 40) -> None:
        self.port_name = port
        self.baudrate = baudrate
        self.connect_timeout_seconds = connect_timeout_seconds
        self.events: Queue[GatewayEvent] = Queue()
        self._serial: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._running = threading.Event()
        self._write_lock = threading.Lock()
        self.recent_lines: deque[str] = deque(maxlen=40)
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

    def diagnostics(self) -> dict:
        return {
            "online": self.online,
            "last_response_seconds_ago": round(time.monotonic() - self.last_response_at, 1) if self.last_response_at else None,
            "recent_lines": list(self.recent_lines)[-12:],
        }

    def start(self) -> None:
        if self._serial and self._serial.is_open:
            return
        self._serial = serial.Serial(self.port_name, self.baudrate, timeout=0.2, write_timeout=1)
        self._running.set()
        self._reader = threading.Thread(target=self._read_loop, name="gateway-reader", daemon=True)
        self._reader.start()
        self.send("AT")
        self.send("AT+CNNI=")
        self.send("AT+SCAN=1")

    def stop(self) -> None:
        if self._serial and self._serial.is_open:
            try:
                self.send("AT+SCAN=0")
            except Exception:
                pass
        self._running.clear()
        if self._reader:
            self._reader.join(timeout=1)
        if self._serial:
            self._serial.close()

    def send(self, command: str) -> None:
        if not self._serial or not self._serial.is_open:
            raise RuntimeError("gateway serial port is not open")
        with self._write_lock:
            self._serial.write((command + "\r\n").encode("ascii"))

    def connect(self, mac: str) -> None:
        mac = normalize_mac(mac)
        timeout_ms = int(self.connect_timeout_seconds * 1000)
        profile_index = self._preferred_profile.get(mac, self._profile_cursor.get(mac, 0))
        label, mtu, security, use_scanned_address = self.CONNECTION_PROFILES[profile_index]
        address = self.addresses.get(mac) if use_scanned_address else None
        address_fields = f"{address[0]},{address[1]}" if address else ","
        self._active_profile[mac] = profile_index
        self.recent_lines.append(f">> CONNECT {mac} [{label}]")
        self.send(
            f"AT+CONN={mac},{address_fields},{mtu},{timeout_ms},1,40,20,0,600,1,{security},0"
        )

    def disconnect(self, mac: str) -> None:
        self.send(f"AT+DISCON=,{normalize_mac(mac)}")

    def scan(self) -> None:
        self.send("AT+SCAN=1")

    def poll(self, limit: int = 500) -> list[GatewayEvent]:
        result: list[GatewayEvent] = []
        for _ in range(limit):
            try:
                result.append(self.events.get_nowait())
            except Empty:
                break
        return result

    def _read_loop(self) -> None:
        assert self._serial is not None
        while self._running.is_set():
            try:
                raw = self._serial.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="ignore").strip()
                self.last_response_at = time.monotonic()
                self.recent_lines.append(line)
                event = parse_gateway_line(line)
                if event:
                    if event.kind == "scan" and event.mac and event.addr_id is not None and event.addr_type is not None:
                        self.addresses[event.mac] = (event.addr_id, event.addr_type)
                    elif event.mac and event.kind == "connected":
                        profile_index = self._active_profile.pop(event.mac, 0)
                        self._preferred_profile[event.mac] = profile_index
                        self._profile_cursor[event.mac] = profile_index
                    elif event.mac and event.kind == "error":
                        profile_index = self._active_profile.pop(event.mac, self._profile_cursor.get(event.mac, 0))
                        label = self.CONNECTION_PROFILES[profile_index][0]
                        self._preferred_profile.pop(event.mac, None)
                        self._profile_cursor[event.mac] = (profile_index + 1) % len(self.CONNECTION_PROFILES)
                        event.message = f"{event.message or '连接失败'} | {label}"
                    self.events.put(event)
            except Exception as exc:
                self.events.put(GatewayEvent("error", message=str(exc)))
                time.sleep(0.5)


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
