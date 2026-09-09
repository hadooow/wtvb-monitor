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
    last_sample_at: float | None = None
    last_cycle_at: float = 0.0
    retry_at: float = 0.0
    failures: int = 0
    handle: int | None = None
    rssi: int | None = None
    error: str | None = None
    latest: dict[str, Any] | None = None
    alarm: dict[str, Any] = field(default_factory=lambda: {"level": "normal", "reasons": []})


def select_victim(states: list[DeviceRuntime], focus_mac: str | None, now: float, dwell: int) -> DeviceRuntime | None:
    candidates = [state for state in states if state.status == "connected" and state.mac != focus_mac]
    if not candidates:
        return None
    completed = [state for state in candidates if state.connected_at is not None and now - state.connected_at >= dwell]
    pool = completed or candidates
    return min(pool, key=lambda state: state.connected_at or 0)


class Scheduler:
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

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.gateway.stop()

    async def restart_gateway(self) -> None:
        """Apply gateway driver/port changes without restarting the web application."""
        self.gateway.stop()
        self.decoder = WtvbStreamDecoder()
        self._connect_ready_at = 0.0
        now = time.monotonic()
        for state in self.states.values():
            state.status = "queued"
            state.connected_at = None
            state.handle = None
            state.retry_at = now
            state.error = None
            state.last_seen = None
            state.last_sample_at = None
            state.rssi = None
            state.latest = None
            state.alarm = {"level": "normal", "reasons": []}
        self.gateway = self._make_gateway()
        self.gateway_error = None
        try:
            self.gateway.start()
        except Exception as exc:
            self.gateway_error = str(exc)
            logger.exception("Gateway restart failed")
        await self.publish({"type": "snapshot", "snapshot": self.snapshot()})

    async def _loop(self) -> None:
        while self._running:
            try:
                self._sync_devices()
                for event in self.gateway.poll():
                    await self._handle_event(event)
                self._expire_focus()
                self._check_timeouts()
                self._rotate_completed()
                self._fill_connections()
            except Exception as exc:
                self.gateway_error = str(exc)
                logger.exception("Scheduler iteration failed")
            await asyncio.sleep(0.1)

    def _sync_devices(self) -> None:
        devices = self._eligible_devices()
        self._device_configs = {device["mac"]: device for device in devices}
        eligible = set(self._device_configs)
        for mac in eligible:
            self.states.setdefault(mac, DeviceRuntime(mac))
        for mac in list(self.states):
            if mac not in eligible:
                state = self.states[mac]
                if state.status == "connected":
                    if not getattr(self.gateway, "busy", False):
                        logger.info("Disconnect disabled device mac=%s", mac)
                        self.gateway.disconnect(mac)
                        state.status = "disconnecting"
                elif state.status not in {"connecting", "disconnecting"}:
                    del self.states[mac]

    async def _handle_event(self, event: GatewayEvent) -> None:
        if event.kind not in {"scan", "notify"}:
            logger.info("Gateway event kind=%s mac=%s handle=%s message=%s", event.kind, event.mac, event.handle, event.message)
        if event.kind == "error":
            if event.mac:
                state = self.states.get(event.mac)
                if not state:
                    return
                state.status = "retrying"
                state.failures += 1
                state.error = event.message
                state.connected_at = None
                state.handle = None
                state.last_cycle_at = time.monotonic()
                # Try each compatibility profile promptly before applying the
                # longer exponential backoff used for persistently absent units.
                if state.failures <= 5:
                    retry_delay = min(self.settings.reconnect_base_seconds, 10)
                else:
                    retry_delay = min(
                        self.settings.reconnect_base_seconds * (2 ** min(state.failures - 6, 4)), 600
                    )
                state.retry_at = time.monotonic() + retry_delay
                self._connect_ready_at = time.monotonic() + 2.0
            else:
                self.gateway_error = event.message
            return
        if not event.mac:
            return
        state = self.states.get(event.mac)
        if not state:
            return
        now = time.monotonic()
        if event.kind == "scan":
            state.last_seen = now
            state.rssi = event.rssi
        elif event.kind == "connected":
            state.status = "connected"
            state.connected_at = now
            state.handle = event.handle
            state.failures = 0
            state.error = None
            state.last_sample_at = None
            state.latest = None
            self._connect_ready_at = now + 2.0
        elif event.kind == "disconnected":
            state.status = "queued"
            state.connected_at = None
            state.handle = None
            state.last_cycle_at = now
            self._connect_ready_at = now + 1.0
        elif event.kind == "notify" and event.payload:
            state.last_seen = now
            for sample in self.decoder.feed(event.mac, event.payload, source=self.settings.gateway_driver):
                await self._accept_sample(sample)

    async def _accept_sample(self, sample: SensorSample) -> None:
        now = time.monotonic()
        state = self.states[sample.mac]
        if state.last_sample_at is None:
            logger.info("First valid sensor sample mac=%s temperature=%s", sample.mac, sample.temperature)
        state.last_sample_at = now
        state.latest = sample.as_dict()
        thresholds = self._device_configs.get(sample.mac, {}).get("thresholds", {})
        state.alarm = evaluate_alarm(sample, thresholds)
        persist_interval = self.settings.persist_interval_seconds
        if now - self._last_persist.get(sample.mac, 0) >= persist_interval:
            self.database.save_sample(sample)
            self._last_persist[sample.mac] = now
        publish_interval = 1.0 / max(self.settings.web_refresh_hz, 1)
        if now - self._last_publish.get(sample.mac, 0) >= publish_interval:
            await self.publish({"type": "sample", "mac": sample.mac, "sample": state.latest, "status": self.status_dict(sample.mac)})
            self._last_publish[sample.mac] = now

    def _connected_states(self) -> list[DeviceRuntime]:
        return [state for state in self.states.values() if state.status in {"connected", "connecting", "disconnecting"}]

    def _expire_focus(self) -> None:
        if self.focus_mac and time.monotonic() > self.focus_until:
            self.focus_mac = None

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        for state in self.states.values():
            if state.status == "connecting" and state.connected_at and now - state.connected_at > self.settings.connect_timeout_seconds + 20 and not getattr(self.gateway, "busy", False):
                state.status = "retrying"
                state.error = "连接超时"
                state.failures += 1
                state.retry_at = now + self.settings.reconnect_base_seconds
                state.connected_at = None
                self._connect_ready_at = now + 2.0
                state.last_cycle_at = now
                logger.error("Scheduler connection timeout mac=%s", state.mac)

    def _rotate_completed(self) -> None:
        if getattr(self.gateway, "busy", False) or any(s.status in {"connecting", "disconnecting"} for s in self.states.values()):
            return
        now = time.monotonic()
        waiting = [state for state in self.states.values() if state.status in {"queued", "retrying"} and state.retry_at <= now]
        if not waiting:
            return
        for state in self.states.values():
            if (
                state.status == "connected"
                and state.mac != self.focus_mac
                and state.connected_at is not None
                and now - state.connected_at >= self.settings.dwell_seconds
            ):
                state.status = "disconnecting"
                self.gateway.disconnect(state.mac)
                break

    def _fill_connections(self) -> None:
        if getattr(self.gateway, "busy", False):
            return
        now = time.monotonic()
        active = self._connected_states()
        if len(active) > self.settings.max_connections:
            victim = select_victim(list(self.states.values()), self.focus_mac, now, 0)
            if victim:
                victim.status = "disconnecting"
                self.gateway.disconnect(victim.mac)
            return
        # EW-DTU02 supports several established links, but only one connection
        # procedure/GATT discovery should be in flight at a time.
        if any(state.status in {"connecting", "disconnecting"} for state in self.states.values()):
            return
        if now < getattr(self, "_connect_ready_at", 0.0):
            return
        if self.focus_mac:
            focus = self.states.get(self.focus_mac)
            if focus and focus.status not in {"connected", "connecting", "disconnecting"} and focus.retry_at <= now and self._ready_to_connect(focus, now):
                if len(active) >= self.settings.max_connections:
                    victim = select_victim(list(self.states.values()), self.focus_mac, now, self.settings.dwell_seconds)
                    if victim:
                        victim.status = "disconnecting"
                        self.gateway.disconnect(victim.mac)
                        return
                self._connect(focus, now)
                return
        slots = self.settings.max_connections - len(active)
        if slots <= 0:
            return
        candidates = [
            state for state in self.states.values()
            if state.status in {"queued", "retrying"}
            and state.retry_at <= now
            and state.mac != self.focus_mac
            and self._ready_to_connect(state, now)
        ]
        candidates.sort(key=lambda state: (state.last_cycle_at, state.failures))
        if candidates:
            self._connect(candidates[0], now)

    def _connect(self, state: DeviceRuntime, now: float) -> None:
        logger.info("Schedule connection mac=%s failures=%s rssi=%s", state.mac, state.failures, state.rssi)
        state.status = "connecting"
        state.connected_at = now
        state.error = None
        self.gateway.connect(state.mac)

    def _ready_to_connect(self, state: DeviceRuntime, now: float) -> bool:
        if self.settings.gateway_driver != "serial":
            return True
        return state.last_seen is not None and now - state.last_seen <= 10

    def request_focus(self, mac: str) -> None:
        mac = normalize_mac(mac)
        if mac not in self.states:
            self.states[mac] = DeviceRuntime(mac)
        self.focus_mac = mac
        self.focus_until = time.monotonic() + self.settings.focus_lease_seconds

    def heartbeat_focus(self) -> None:
        if self.focus_mac:
            self.focus_until = time.monotonic() + self.settings.focus_lease_seconds

    def clear_focus(self) -> None:
        self.focus_mac = None
        self.focus_until = 0

    async def remove_device(self, mac: str) -> None:
        """Stop scheduling a device before its database registration is removed."""
        mac = normalize_mac(mac)
        state = self.states.pop(mac, None)
        self._device_configs.pop(mac, None)
        self._last_persist.pop(mac, None)
        self._last_publish.pop(mac, None)
        self.decoder.forget(mac)
        if self.focus_mac == mac:
            self.clear_focus()
        if state and state.status in {"connected", "connecting", "disconnecting"} and not getattr(self.gateway, "_faulted", False):
            try:
                self.gateway.disconnect(mac)
            except Exception:
                pass
        await self.publish({"type": "snapshot", "snapshot": self.snapshot()})

    def status_dict(self, mac: str) -> dict[str, Any]:
        state = self.states.get(normalize_mac(mac))
        if not state:
            return {"status": "disabled", "is_focus": False}
        now = time.monotonic()
        return {
            "status": state.status,
            "is_focus": state.mac == self.focus_mac,
            "connected_seconds": round(now - state.connected_at, 1) if state.connected_at else None,
            "last_seen_seconds_ago": round(now - state.last_seen, 1) if state.last_seen else None,
            "last_sample_seconds_ago": round(now - state.last_sample_at, 1) if state.last_sample_at else None,
            "collecting": state.status == "connected" and state.last_sample_at is not None and now - state.last_sample_at <= 10,
            "rssi": state.rssi,
            "failures": state.failures,
            "error": state.error,
            "latest": state.latest,
            "alarm": state.alarm,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "gateway": {
                "name": self.gateway.name,
                "driver": self.settings.gateway_driver,
                "error": self.gateway_error,
                **self.gateway.diagnostics(),
                "connected": sum(state.status == "connected" for state in self.states.values()),
                "maximum": self.settings.max_connections,
            },
            "focus_mac": self.focus_mac,
            "devices": {mac: self.status_dict(mac) for mac in self.states},
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
