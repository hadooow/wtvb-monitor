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
                    return GatewayEvent("error", ma