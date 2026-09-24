from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .config import Settings
from .database import Database
from .gateway import GatewayEvent, SerialGateway, SimulatorGateway
from .protocol import SensorSample, WtvbStreamDecoder, evaluate_alarm, normalize_mac

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceRuntime:
    mac: str
    status: str = "queued"
    connected_at: float | None = None
    last_seen: float | None = None
    last_discovered: float | None = None
    last_sample_at: float | None = None
    last_cycle_at: float = 0.0
    retry_at: float = 0.0
    failures: int = 0
    handle: int | None = None
    rssi: int | None = None
    error: str | None = None
    latest: dict[str, Any] | None = None
    recovery_reason: str | None = None
    manual_paused: bool = False
    disconnect_requested: bool = False
    verified: bool = False
    data_stable_since: float | None = None
    alarm: dict[str, Any] = field(default_factory=lambda: {"level": "normal", "reasons": []})


def select_victim(states: list[DeviceRuntime], focus_mac: str | None, now: float, dwell: int) -> DeviceRuntime | None:
    candidates = [state for state in states if state.status == "connected" and state.mac != focus_mac]
    if not candidates:
        return None
    completed = [state for state in candidates if state.connected_at is not None and now - state.connected_at >= dwell]
    pool = completed or candidates
    return min(pool, key=lambda state: state.connected_at or 0)


class Scheduler:
    DISCOVERY_SECONDS = 3.0
    DISCOVERY_LIMIT_SECONDS = 8.0
    REDISCOVERY_SECONDS = 30.0
    FIRST_SAMPLE_SECONDS = 20.0
    STALE_SAMPLE_SECONDS = 20.0
    DATA_SETTLE_SECONDS = 5.0

    def __init__(
        self,
        database: Database,
        settings: Settings,
        publish: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.database = database
        self.settings = settings
        self.publish = publish
        self.decoder = WtvbStreamDecoder()
        self.states: dict[str, DeviceRuntime] = {}
        self.focus_mac: str | None = None
        self.focus_until = 0.0
        self._last_persist: dict[str, float] = {}
        self._last_publish: dict[str, float] = {}
        self._device_configs: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._connect_ready_at = 0.0
        self.gateway = self._make_gateway()
        self.gateway_error: str | None = None
        self._serial_batch: list[str] = []
        self._serial_candidates: set[str] = set()
        self._serial_draining = False
        self._serial_refresh_at = 0.0
        self._serial_focus_pending = False

    def _eligible_devices(self) -> list[dict[str, Any]]:
        include_simulated = self.settings.gateway_driver == "simulator"
        return [device for device in self.database.list_devices(include_simulated) if device["enabled"]]

    def _make_gateway(self):
        if self.settings.gateway_driver == "serial":
            return SerialGateway(
                self.settings.serial_port,
                self.settings.baudrate,
                self.settings.connect_timeout_seconds,
            )
        return SimulatorGateway(lambda: [device["mac"] for device in self._eligible_devices()])

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._sync_devices()
        try:
            self.gateway.start()
        except Exception as exc:
            self.gateway_error = str(exc)
            logger.exception("Gateway startup failed")
        self._task = asyncio.create_task(self._loop(), name="sensor-scheduler")

    async def sto