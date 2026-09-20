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
        self._serial_batch.clear()
        self._serial_candidates.clear()
        self._serial_draining = False
        self._serial_refresh_at = 0.0
        self._serial_focus_pending = bool(self.focus_mac)
        now = time.monotonic()
        for state in self.states.values():
            state.status = "queued"
            state.connected_at = None
            state.handle = None
            state.retry_at = now
            state.error = None
            state.last_seen = None
            state.last_discovered = None
            state.last_sample_at = None
            state.rssi = None
            state.latest = None
            state.recovery_reason = None
            state.data_stable_since = None
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
                await self._adopt_registered_links()
                self._expire_focus()
                self._check_timeouts()
                self._rotate_completed()
                self._fill_connections()
            except Exception as exc:
                self.gateway_error = self.gateway_error or str(exc)
                logger.exception("Scheduler iteration failed")
            await asyncio.sleep(0.1)

    async def _adopt_registered_links(self) -> None:
        """A device can be registered after its startup connection event."""
        if self.settings.gateway_driver != "serial" or self.gateway.busy:
            return
        for mac, handle in tuple(self.gateway.active_links.items()):
            state = self.states.get(mac)
            if (mac in self._device_configs and state is not None
                    and state.status in {"queued", "retrying"}):
                await self._handle_event(GatewayEvent("connected", mac, handle=handle))

    def _sync_devices(self) -> None:
        devices = self._eligible_devices()
        self._device_configs = {device["mac"]: device for device in devices}
        eligible = set(self._device_configs)
        for mac in eligible:
            if mac not in self.states:
                self.states[mac] = DeviceRuntime(mac)
                # A newly enabled/registered target gets one prompt discovery.
                self._serial_refresh_at = 0.0
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
        if event.kind == "warning":
            # A damaged notification is not a failed BLE connection. Discard
            # its partial frame so a later valid packet cannot complete it.
            if event.mac:
                self.decoder.forget(event.mac)
            else:
                self.decoder = WtvbStreamDecoder()
            return
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
                self.gateway_error = self.gateway_error or event.message
            return
        if not event.mac:
            return
        state = self.states.get(event.mac)
        if not state:
            return
        now = time.monotonic()
        if event.kind == "scan":
            state.last_seen = now
            state.last_discovered = now
            state.rssi = event.rssi
        elif event.kind == "connected":
            if state.status == "connected" and state.handle == event.handle:
                return  # A connection-list replay must not reset live samples.
            state.status = "connected"
            state.connected_at = now
            state.handle = event.handle
            state.error = None
            state.recovery_reason = None
            state.data_stable_since = None
            state.last_sample_at = None
            state.latest = None
            self._connect_ready_at = now + 2.0
            # Long connection procedures must not consume the collection window.
            self._serial_refresh_at = now + max(self.settings.dwell_seconds, self.REDISCOVERY_SECONDS)
        elif event.kind == "disconnected":
            silent_link = (state.status == "connected" and state.last_sample_at is None)
            if silent_link and self.settings.gateway_driver == "serial":
                self._prepare_no_data_retry(state, now)
            self.decoder.forget(event.mac)
            state.last_sample_at = None
            state.error = state.recovery_reason or event.message
            state.status = "retrying" if state.recovery_reason else "queued"
            state.recovery_reason = None
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
        if state.last_sample_at is None or now - state.last_sample_at > 2.0:
            state.data_stable_since = now
        state.last_sample_at = now
        state.verified = True
        state.failures = 0
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
            if (self.settings.gateway_driver == "serial" and state.status == "connected"
                    and state.connected_at is not None and not getattr(self.gateway, "busy", False)):
                last_data = state.last_sample_at if state.last_sample_at is not None else state.connected_at
                grace = self.STALE_SAMPLE_SECONDS if state.last_sample_at is not None else self.FIRST_SAMPLE_SECONDS
                if now - last_data >= grace:
                    self._prepare_no_data_retry(state, now)
                    state.status = "disconnecting"
                    self.gateway.disconnect(state.mac)
                    return
            if state.status == "connecting" and state.connected_at and now - state.connected_at > self.settings.connect_timeout_seconds + 20 and not getattr(self.gateway, "busy", False):
                state.status = "retrying"
                state.error = "连接超时"
                state.failures += 1
                state.retry_at = now + self.settings.reconnect_base_seconds
                state.connected_at = None
                self._connect_ready_at = now + 2.0
                state.last_cycle_at = now
                logger.error("Scheduler connection timeout mac=%s", state.mac)

    def _prepare_no_data_retry(self, state: DeviceRuntime, now: float) -> None:
        state.failures += 1
        state.recovery_reason = "已连接但未收到有效数据，已隔离该连接并等待重试"
        state.error = state.recovery_reason
        state.retry_at = now + min(30 * 2 ** min(state.failures - 1, 3), 240)
        self.gateway.report_no_data(state.mac)
        logger.warning("No valid data mac=%s retry_in=%.0fs", state.mac, state.retry_at - now)

    def _rotate_completed(self) -> None:
        if self.settings.gateway_driver == "serial":
            return  # Serial links rotate as a batch before the next scan.
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
        if self.settings.gateway_driver == "serial":
            self._schedule_serial()
            return
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

    def _schedule_serial(self) -> None:
        """Discover without links, freeze candidates, then collect without scans.

        A batch fills multiple slots sequentially, then streams concurrently.
        Full batches keep an active focus pinned. Refilling unused slots or
        switching to an unseen focus releases the batch before fresh discovery.
        """
        gateway = self.gateway
        if gateway.busy:
            return
        # Reopening the serial port does not disconnect existing BLE links.
        # Release unregistered/disabled links rather than scanning over them.
        for mac in tuple(gateway.active_links):
            if mac not in self._device_configs:
                gateway.disconnect(mac)
                return
        if any(s.status in {"connecting", "disconnecting"} for s in self.states.values()):
            return
        now = time.monotonic()
        if now < self._connect_ready_at:
            return
        active = [s for s in self.states.values() if s.status == "connected"]
        waiting = [s for s in self.states.values()
                   if s.status in {"queued", "retrying"} and s.retry_at <= now]
        if len(active) > self.settings.max_connections:
            victim = select_victim(active, self.focus_mac, now, 0)
            if victim:
                victim.status = "disconnecting"
                gateway.disconnect(victim.mac)
            return
        focus = self.states.get(self.focus_mac)
        # A focus already in this batch can use a free slot without rediscovery.
        if self.focus_mac in self._serial_batch:
            self._serial_batch.remove(self.focus_mac)
            self._serial_batch.insert(0, self.focus_mac)
        focus_switch = (self._serial_focus_pending and focus in waiting
                        and (focus.mac not in self._serial_batch
                             or len(active) >= self.settings.max_connections))
        if not self._serial_draining and not focus_switch and any(
            s.last_sample_at is None or now - s.last_sample_at > 10.0
            or s.data_stable_since is None or now - s.data_stable_since < self.DATA_SETTLE_SECONDS
            for s in active
        ):
            # Confirm data flow before adding another radio/GATT procedure.
            # _check_timeouts isolates a silent peer instead of stalling forever.
            return
        if not self._serial_draining and not focus_switch and len(active) < self.settings.max_connections:
            while self._serial_batch:
                candidate = self.states.get(self._serial_batch.pop(0))
                if candidate in waiting and candidate.mac in self._device_configs:
                    self._connect(candidate, now)
                    return
            retries = [s for s in waiting if s.mac in self._serial_candidates]
            if retries:
                retries.sort(key=lambda s: (s.mac != self.focus_mac, s.last_cycle_at, s.failures))
                self._connect(retries[0], now)
                return
        # Restarting the app can adopt only one existing link. Rebuild a batch
        # promptly to fill unused slots, then bound retries for an absent peer.
        refill = (waiting and len(active) < self.settings.max_connections
                  and now >= self._serial_refresh_at)
        expired = (waiting and focus not in active and active and all(
            s.connected_at is not None and now - s.connected_at >= self.settings.dwell_seconds
            for s in active))
        if active and (focus_switch or refill or expired):
            self._serial_draining = True
            self._serial_batch.clear()
            self._serial_candidates.clear()
        if self._serial_draining:
            if active:
                victim = next((s for s in active if s.mac != self.focus_mac), active[0])
                victim.status = "disconnecting"
                gateway.disconnect(victim.mac)
                return
            self._serial_draining = False
        if active or gateway.active_links:
            return
        if not waiting:
            return
        if gateway.scanning is not True:
            gateway.scan()
            return
        started = gateway.scan_started_at
        if started is None or now - started < self.DISCOVERY_SECONDS:
            return
        candidates = [s for s in waiting if s.last_discovered is not None
                      and s.last_discovered >= started]
        enough = len(candidates) >= min(len(waiting), self.settings.max_connections)
        focus_seen = focus not in waiting or focus in candidates
        if (not enough or not focus_seen) and now - started < self.DISCOVERY_LIMIT_SECONDS:
            return
        if not candidates:
            return
        if focus in waiting and focus not in candidates:
            focus.error = "本轮未发现优先设备，请确认供电、距离及手机连接状态；稍后自动重试"
        candidates.sort(key=lambda s: (s.mac != self.focus_mac, s.last_cycle_at, not s.verified, s.failures, s.mac))
        self._serial_batch = [s.mac for s in candidates]
        self._serial_candidates = set(self._serial_batch)
        self._serial_focus_pending = False
        self._serial_refresh_at = now + self.REDISCOVERY_SECONDS
        gateway.stop_scan()

    def _connect(self, state: DeviceRuntime, now: float) -> None:
        logger.info("Schedule connection mac=%s failures=%s rssi=%s", state.mac, state.failures, state.rssi)
        state.status = "connecting"
        state.connected_at = now
        state.error = None
        self.gateway.connect(state.mac)

    def _ready_to_connect(self, state: DeviceRuntime, now: float) -> bool:
        if self.settings.gateway_driver != "serial":
            return True
        return state.mac in self._serial_batch and self.gateway.scanning is False

    def request_focus(self, mac: str) -> None:
        mac = normalize_mac(mac)
        if mac not in self.states:
            self.states[mac] = DeviceRuntime(mac)
        self.states[mac].retry_at = min(self.states[mac].retry_at, time.monotonic())
        self.focus_mac = mac
        self._serial_focus_pending = True
        self.focus_until = time.monotonic() + self.settings.focus_lease_seconds

    def heartbeat_focus(self) -> None:
        if self.focus_mac:
            self.focus_until = time.monotonic() + self.settings.focus_lease_seconds

    def clear_focus(self) -> None:
        self.focus_mac = None
        self.focus_until = 0
        self._serial_focus_pending = False

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
        diagnostics = self.gateway.diagnostics()
        fault = diagnostics.get("first_fault")
        return {
            "gateway": {
                "name": self.gateway.name,
                "driver": self.settings.gateway_driver,
                "error": fault["message"] if fault else self.gateway_error,
                **diagnostics,
                "connected": sum(state.status == "connected" for state in self.states.values()),
                "maximum": self.settings.max_connections,
            },
            "focus_mac": self.focus_mac,
            "devices": {mac: self.status_dict(mac) for mac in self.states},
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
