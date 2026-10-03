"""Windows BLE path using the vendor's FFE5 service and FFE4 notifications."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from queue import Empty, Queue

from .gateway import GatewayEvent
from .protocol import normalize_mac

logger = logging.getLogger(__name__)
SERVICE_UUID = '0000ffe5-0000-1000-8000-00805f9a34fb'
NOTIFY_UUID = '0000ffe4-0000-1000-8000-00805f9a34fb'


class BleGateway:
    """All WinRT objects stay on the application's asyncio event loop.

    An explicit scan supplies BLEDevice objects, avoiding implicit scans when
    opening a second device. No sensor configuration writes or pairing occur.
    """
    def __init__(self, timeout: int = 40, *, scanner_factory=None, client_factory=None):
        self.timeout = timeout
        self._scanner_factory = scanner_factory
        self._client_factory = client_factory
        self.events = Queue()
        self._started = False
        self._busy = False
        self._tasks = set()
        self._clients = {}
        self._devices = {}
        self._seen = {}
        self._last_ad_event = {}
        self._handles = {}
        self._sequence = 0
        self._last_scan = float('-inf')
        self._scanner = None
        self.history = deque(maxlen=50)
        self.gatt = {}
        self.notifications = {}
        self.scanning = False
        self.last_scan_error = None
        self._intentional = set()
        self._connecting = {}
        self._pending_disconnects = {}
        self._retired_clients = deque()
        self._rediscover_after = {}
        self._full_discovery = set()
        self._subscribed = set()
        self._cleanup_retry = {}

    @property
    def name(self):
        return 'Windows 电脑蓝牙直连'

    @property
    def online(self):
        return self._started

    @property
    def busy(self):
        return self._busy or bool(self._pending_disconnects) or bool(self._retired_clients)

    @property
    def active_links(self):
        return dict(self._handles)

    def _record(self, kind, message):
        logger.info('BLE %s %s', kind, message)
        self.history.append({'kind': kind, 'message': message})

    def start(self):
        if self._scanner_factory is None or self._client_factory is None:
            from bleak import BleakClient, BleakScanner
            self._scanner_factory = self._scanner_factory or BleakScanner
            self._client_factory = self._client_factory or BleakClient
        self._started = True
        self.scan()

    def stop(self):
        self._started = False
        self._pending_disconnects.clear()
        for task in tuple(self._tasks):
            task.cancel()

    async def aclose(self):
        self.stop()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for mac, client in tuple(self._clients.items()):
            try:
                await self._release_client(mac, client)
            except Exception:
                logger.exception('BLE shutdown disconnect failed')
        while self._retired_clients:
            mac, client = self._retired_clients.popleft()
            try:
                await self._release_client(mac, client)
            except Exception:
                logger.exception('BLE retired client cleanup failed')
        self._clients.clear()
        self._handles.clear()
        self._connecting.clear()
        for mac, (client, _) in tuple(self._cleanup_retry.items()):
            try:
                await self._release_client(mac, client)
            except Exception:
                logger.exception('BLE deferred shutdown cleanup failed')
        self._cleanup_retry.clear()
        self._subscribed.clear()

    def _launch(self, coroutine):
        self._busy = True
        task = asyncio.create_task(self._run(coroutine))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: coroutine.close())

    async def _run(self, coroutine):
        try:
            await coroutine
        except Exception:
            logger.exception('BLE operation failed')
        finally:
            self._busy = False
            self._drain_disconnects()

    def _drain_disconnects(self):
        if self._started and not self._busy and self._retired_clients:
            self._launch(self._cleanup_retired())
            return
        if self._started and not self._busy and self._pending_disconnects:
            mac = next(iter(self._pending_disconnects))
            handle = self._pending_disconnects.pop(mac)
            self._launch(self._disconnect(mac, expected_handle=handle))

    async def _cleanup_retired(self):
        mac, client = self._retired_clients[0]
        try:
            await self._release_client(mac, client)
        except asyncio.CancelledError:
            # aclose will retry this native release after task cancellation.
            raise
        except Exception:
            logger.exception('BLE retired client cleanup failed')
            self._defer_cleanup(mac, client)
        else:
            self._require_rediscovery(mac)
        self._retired_clients.popleft()

    def _defer_cleanup(self, mac, client):
        # Retain native ownership but let healthy peers continue collecting.
        self._cleanup_retry[mac] = (client, time.monotonic() + 30)
        self._record('cleanup_deferred', f'{mac}; retry native release in 30s')

    def _retry_cleanup(self):
        if not self._started or self.busy:
            return
        for mac, (client, due) in tuple(self._cleanup_retry.items()):
            if time.monotonic() >= due:
                del self._cleanup_retry[mac]
                self._retired_clients.append((mac, client))
                self._drain_disconnects()
                break

    def _detection(self, device, advertisement):
        if not self._started:
            return
        try:
            mac = normalize_mac(device.address)
        except ValueError:
            return
        now = time.monotonic()
        old = self._last_ad_event.get(mac)
        self._devices[mac] = device
        self._seen[mac] = now
        if len(self._devices) > 256:
            victim = min(self._seen, key=self._seen.get)
            self._devices.pop(victim, None)
            self._seen.pop(victim, None)
            self._last_ad_event.pop(victim, None)
        if old is None or now - old >= 0.5:
            self._last_ad_event[mac] = now
            self.events.put(GatewayEvent('scan', mac, rssi=advertisement.rssi,
                                         message=advertisement.local_name or device.name))

    def discovered(self, mac):
        return (mac not in self._cleanup_retry
                and mac in self._devices and time.monotonic() - self._seen[mac] <= 60
                and self._seen[mac] >= self._rediscover_after.get(mac, float('-inf')))

    def _require_rediscovery(self, mac, settle=5.0):
        # A Windows session close is not proof that the radio/peripheral has
        # finished releasing it. Require a new advertisement after settling.
        self._devices.pop(mac, None)
        self._seen.pop(mac, None)
        self._last_ad_event.pop(mac, None)
        self._rediscover_after[mac] = time.monotonic() + settle
        if len(self._rediscover_after) > 256:
            self._rediscover_after.pop(next(iter(self._rediscover_after)))

    async def _release_client(self, mac, client):
        if client.is_connected and client in self._subscribed:
            try:
                await asyncio.wait_for(client.stop_notify(NOTIFY_UUID), 3)
            except Exception as exc:
                self._record('unsubscribe_failed', f'{mac}: {type(exc).__name__}: {exc}')
        # Bleak disconnect closes the native GATT services, even for a client
        # whose remote link is already lost. Do not only discard the Python map.
        await asyncio.wait_for(client.disconnect(), 10)
        self._subscribed.discard(client)

    def scan(self):
        if self._started and not self.busy and time.monotonic() - self._last_scan >= 10:
            self._last_scan = time.monotonic()
            self._launch(self._scan())

    async def _scan(self):
        scanner = None
        try:
            scanner = self._scanner_factory(detection_callback=self._detection)
            self._scanner = scanner
            await asyncio.wait_for(scanner.start(), 10)
            self.last_scan_error = None
            self.scanning = True
            await asyncio.sleep(5)
        except Exception as exc:
            self.last_scan_error = str(exc)
            self._record('scan_failed', str(exc))
            if self._started:
                self.events.put(GatewayEvent('warning', message=f'BLE_SCAN_FAILED: {exc}; 请检查电脑蓝牙是否开启'))
        finally:
            try:
                if scanner is not None:
                    await asyncio.wait_for(scanner.stop(), 10)
            except Exception as exc:
                self._record('scan_stop_failed', str(exc))
            self.scanning = False
            self._scanner = None

    def connect(self, mac):
        mac = normalize_mac(mac)
        if not self._started or self.busy:
            return
        if mac in self._clients:
            return
        if not self.discovered(mac):
            self.events.put(GatewayEvent('error', mac, message='BLE_NOT_DISCOVERED: 60 秒内未扫描到设备'))
            return
        self._sequence += 1
        self._launch(self._connect(mac, self._sequence))

    async def _connect(self, mac, handle):
        self._connecting[mac] = handle
        client = None
        ready = False
        stage = 'connect_and_services'
        started_at = time.monotonic()
        profile = 'full_uncached' if mac in self._full_discovery else 'targeted_uncached'
        def disconnected(_client):
            if ready and self._started and (mac, handle) not in self._intentional and self._clients.get(mac) is _client:
                self._clients.pop(mac, None)
                self._handles.pop(mac, None)
                self._require_rediscovery(mac)
                self._retired_clients.append((mac, _client))
                self._drain_disconnects()
                self.events.put(GatewayEvent('disconnected', mac, handle=handle,
                                             message='BLE_LINK_LOST: Windows 蓝牙连接断开'))
        def notification(_sender, data):
            if (self._started and (mac, handle) not in self._intentional
                    and self._clients.get(mac) is client and self._handles.get(mac) == handle):
                stats = self.notifications.setdefault(mac, {'count': 0, 'lengths': {}, 'last_hex': ''})
                stats['count'] += 1
                length = str(len(data))
                stats['lengths'][length] = stats['lengths'].get(length, 0) + 1
                stats['last_hex'] = bytes(data[:64]).hex()
                self.events.put(GatewayEvent('notify', mac, handle=handle, payload=bytes(data)))
        try:
            self._record('connect_attempt', f'{mac} handle={handle} profile={profile} timeout={self.timeout}s')
            client = self._client_factory(self._devices[mac], pair=False, timeout=self.timeout,
                                          services=None if mac in self._full_discovery else [SERVICE_UUID],
                                          winrt={'use_cached_services': False},
                                          disconnected_callback=disconnected)
            await asyncio.wait_for(client.connect(), self.timeout)
            stage = 'service_and_characteristic_check'
            services = list(client.services)
            summary = [{'uuid': s.uuid, 'characteristics': [c.uuid for c in s.characteristics]} for s in services]
            self.gatt[mac] = summary
            if len(self.gatt) > 50:
                self.gatt.pop(next(iter(self.gatt)))
            if mac not in self.notifications and len(self.notifications) >= 50:
                self.notifications.pop(next(iter(self.notifications)))
            self._record('gatt', f'{mac} {summary}')
            service = client.services.get_service(SERVICE_UUID)
            characteristic = next((c for c in service.characteristics if c.uuid.lower() == NOTIFY_UUID), None) if service else None
            if characteristic is None or 'notify' not in characteristic.properties:
                # A UUID-filtered WinRT query can return an empty collection
                # after reconnect. Recreate the session and enumerate all
                # services on the next fresh-advertisement attempt.
                self._full_discovery.add(mac)
                self._record('service_recovery', f'{mac}; next attempt=full_uncached')
                raise RuntimeError('BLE_SERVICE_MISMATCH: 未获得 FFE5/FFE4 notify 服务；释放连接并重新扫描后重试')
            self._clients[mac] = client
            self._handles[mac] = handle
            stage = 'subscribe_notify'
            await asyncio.wait_for(client.start_notify(characteristic, notification), 10)
            self._subscribed.add(client)
            if not client.is_connected or not self._started:
                raise RuntimeError('BLE_LINK_LOST during notification subscription')
            ready = True
            self.events.put(GatewayEvent('connected', mac, handle=handle,
                                         mtu=client.mtu_size, service_count=len(services)))
            self._record('connected', f'{mac} profile={profile} elapsed={time.monotonic()-started_at:.2f}s')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc) or f'operation exceeded {self.timeout if stage == "connect_and_services" else 10}s'
            self._record('connect_failed', f'{mac} stage={stage} profile={profile} elapsed={time.monotonic()-started_at:.2f}s: {type(exc).__name__}: {detail}')
            if self._started:
                self.events.put(GatewayEvent('error', mac, handle=handle, message=f'BLE_CONNECT_FAILED stage={stage} profile={profile}: {type(exc).__name__}: {detail}'))
        finally:
            self._connecting.pop(mac, None)
            if not ready and client is not None:
                if self._clients.get(mac) is client:
                    self._clients.pop(mac, None)
                    self._handles.pop(mac, None)
                try:
                    await self._release_client(mac, client)
                except Exception:
                    logger.exception('BLE failed connection cleanup')
                    self._defer_cleanup(mac, client)
                finally:
                    self._require_rediscovery(mac, settle=10.0)

    def disconnect(self, mac):
        if self._started:
            mac = normalize_mac(mac)
            # Pause/delete can arrive while another device is connecting or
            # while scanning. Preserve the operation and the target session.
            handle = self._handles.get(mac, self._connecting.get(mac))
            self._pending_disconnects.setdefault(mac, handle)
            self._drain_disconnects()

    async def _disconnect(self, mac, expected_handle=None):
        client = self._clients.get(mac)
        handle = self._handles.get(mac)
        if expected_handle is not None and handle != expected_handle:
            # A link-loss event already released this generation. Never let
            # the old request tear down a later connection to the same MAC.
            self._record('stale_disconnect', f'{mac} expected={expected_handle} current={handle}')
            return
        self._intentional.add((mac, handle))
        try:
            if client is not None:
                await self._release_client(mac, client)
        except Exception as exc:
            # Keep ownership and report the operation failure; never pretend
            # a still-live link has been released.
            if client is not None and client.is_connected:
                self._record('disconnect_failed', f'{mac}: {exc}')
                self.events.put(GatewayEvent('info', mac, handle=handle, message=f'BLE_DISCONNECT_FAILED: {exc}'))
                self.events.put(GatewayEvent('connected', mac, handle=handle))
                return
            if client is not None:
                self._defer_cleanup(mac, client)
        finally:
            self._intentional.discard((mac, handle))
        if self._clients.get(mac) is client:
            self._clients.pop(mac, None)
            self._handles.pop(mac, None)
        if client is not None:
            self._require_rediscovery(mac)
            self._record('released', f'{mac} handle={handle}; waiting for fresh advertisement')
        if self._started:
            self.events.put(GatewayEvent('disconnected', mac, handle=handle))

    def report_data(self, mac):
        pass

    def report_no_data(self, mac):
        self._record('no_data', mac)

    def poll(self, limit=500):
        self._retry_cleanup()
        result = []
        for _ in range(limit):
            try:
                result.append(self.events.get_nowait())
            except Empty:
                break
        return result

    def diagnostics(self):
        return {'online': self.online, 'busy': self.busy, 'scanning': self.scanning,
                'pending_disconnects': list(self._pending_disconnects),
                'pending_native_cleanup': len(self._retired_clients),
                'deferred_native_cleanup': list(self._cleanup_retry),
                'full_service_discovery': sorted(self._full_discovery),
                'rediscovery_wait_seconds': {m: round(max(0, due-time.monotonic()), 1)
                                            for m, due in self._rediscover_after.items()},
                'ble_scan_error': self.last_scan_error,
                'active_connections': len(self._clients), 'event_history': list(self.history),
                'gatt_services': self.gatt, 'notification_stats': self.notifications, 'discovered_devices': [
                    {'mac': m, 'name': d.name, 'seconds_ago': round(time.monotonic()-self._seen[m], 1)}
                    for m, d in self._devices.items()]}
