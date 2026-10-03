"""Regressions for empty WinRT services and avoidable v0.6.15 link cycling."""
import asyncio
from types import SimpleNamespace

import pytest

from app.ble_gateway import BleGateway, SERVICE_UUID
from app.gateway import GatewayEvent
from app.scheduler import DeviceRuntime
from test_ble_gateway import Client, DEVICE, MAC, gateway
from test_field_v0614 import finish
from test_scan_phases import phase_scheduler, MACS


def advertise(g, device=DEVICE):
    g._detection(device, SimpleNamespace(rssi=-65, local_name=device.name))


def test_empty_filtered_service_query_switches_profile_and_keeps_working(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.ble_gateway.time.monotonic', lambda: clock[0])
    clients = []

    class FilteredEmpty(Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)
            if kwargs['services'] is not None:
                self.services.clear()  # same gatt [] as the supplied field log

        async def stop_notify(self, *args):
            assert self.services, 'unsubscribing an unregistered characteristic'

    async def run():
        g = gateway(FilteredEmpty)
        g.connect(MAC)
        await finish(g)
        assert g.poll()[-1].kind == 'error'
        assert clients[0].disconnects == 1
        assert clients[0].options['services'] == [SERVICE_UUID]
        clock[0] += 6
        advertise(g)
        assert not g.discovered(MAC)
        clock[0] += 5
        assert not g.discovered(MAC)  # an early ad cannot mature into a fresh ad
        advertise(g)
        g.poll()
        g.connect(MAC)
        await finish(g)
        assert g.poll()[-1].kind == 'connected'
        assert clients[-1].options['services'] is None
        assert clients[-1].options['winrt']['use_cached_services'] is False
        g.disconnect(MAC)
        await finish(g)
        clock[0] += 6
        advertise(g)
        g.poll()
        g.connect(MAC)
        await finish(g)
        assert g.poll()[-1].kind == 'connected'
        assert clients[-1].options['services'] is None
        assert g.diagnostics()['full_service_discovery'] == [MAC]
        await g.aclose()

    asyncio.run(run())


def test_failed_native_release_blocks_only_affected_peer_and_is_retried(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.ble_gateway.time.monotonic', lambda: clock[0])
    clients = []

    class BrokenRelease(Client):
        async def connect(self):
            await super().connect()
            if self.device is DEVICE:
                self.services.clear()

        async def disconnect(self):
            self.disconnects += 1
            if self.device is DEVICE and self.disconnects == 1:
                raise RuntimeError('native close not finished')
            self.is_connected = False
            self.options['disconnected_callback'](self)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    async def run():
        g = gateway(BrokenRelease)
        g.connect(MAC)
        await finish(g)
        assert not g.busy
        assert g.diagnostics()['deferred_native_cleanup'] == [MAC]
        clock[0] += 11
        advertise(g)
        assert not g.discovered(MAC)
        peer = SimpleNamespace(address=MACS[1], name='healthy peer')
        advertise(g, peer)
        g.connect(MACS[1])
        await finish(g)
        assert MACS[1] in g.active_links
        peer_client = g._clients[MACS[1]]
        clock[0] += 20
        g.poll()  # retries native release without touching the healthy peer
        await finish(g)
        assert clients[0].disconnects == 2
        assert not g._cleanup_retry and not g.discovered(MAC)
        assert peer_client.disconnects == 0
        clock[0] += 6
        advertise(g)
        assert g.discovered(MAC)
        await g.aclose()

    asyncio.run(run())


def test_notifications_during_intentional_release_are_ignored():
    class LateNotify(Client):
        async def stop_notify(self, *args):
            self.notify(None, b'late while closing')

    async def run():
        g = gateway(LateNotify)
        g.connect(MAC)
        await finish(g)
        g.poll()
        g.disconnect(MAC)
        await finish(g)
        assert [e.kind for e in g.poll()] == ['disconnected']
        await g.aclose()

    asyncio.run(run())


def test_connect_timeout_has_actionable_duration_and_releases_client():
    class Timeout(Client):
        async def connect(self):
            raise TimeoutError()

    async def run():
        g = gateway(Timeout)
        g.connect(MAC)
        await finish(g)
        event = g.poll()[-1]
        assert event.kind == 'error' and '40s' in event.message
        assert 'connect_and_services' in event.message
        assert not g.busy and not g.active_links
        assert not g._subscribed
        await g.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('stage', ['start', 'stop'])
def test_native_scanner_operations_are_bounded(monkeypatch, stage):
    original_wait = asyncio.wait_for

    async def fast_wait(awaitable, timeout):
        return await original_wait(awaitable, .01)

    monkeypatch.setattr('app.ble_gateway.asyncio.wait_for', fast_wait)

    class Scanner:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            if stage == 'start':
                await asyncio.Event().wait()
            else:
                raise RuntimeError('force scanner cleanup')

        async def stop(self):
            if stage == 'stop':
                await asyncio.Event().wait()

    async def run():
        g = BleGateway(scanner_factory=Scanner, client_factory=Client)
        g.start()
        await finish(g)
        assert not g.busy and not g.scanning
        assert any(e['kind'] == f'scan_{"failed" if stage == "start" else "stop_failed"}' for e in g.history)
        await g.aclose()

    asyncio.run(run())


def configure_rotation(s, clock, *, limit, discovered):
    s.settings.gateway_driver = 'ble'
    s.settings.max_connections = limit
    calls = []
    s.gateway = SimpleNamespace(busy=False,
                                discovered=lambda mac: mac in discovered,
                                disconnect=lambda mac: calls.append(('disconnect', mac)),
                                scan=lambda: calls.append(('scan', None)))
    s.states[MACS[0]] = DeviceRuntime(MACS[0], 'connected', connected_at=clock[0] - 100)
    s.states[MACS[1]] = DeviceRuntime(MACS[1], 'connected', connected_at=clock[0] - 100)
    return calls


def test_waiting_failure_never_rotates_healthy_links_when_slot_is_free(phase_scheduler):
    s, clock = phase_scheduler
    calls = configure_rotation(s, clock, limit=3, discovered={MACS[2]})
    s._rotate_completed()
    assert calls == []
    assert all(s.states[mac].status == 'connected' for mac in MACS[:2])


def test_absent_replacement_scans_without_tearing_down_healthy_links(phase_scheduler):
    s, clock = phase_scheduler
    calls = configure_rotation(s, clock, limit=2, discovered=set())
    s._rotate_completed()
    assert calls == [('scan', None)]
    assert all(s.states[mac].status == 'connected' for mac in MACS[:2])


def test_full_adapter_with_no_waiting_peer_does_not_scan(phase_scheduler):
    s, clock = phase_scheduler
    calls = configure_rotation(s, clock, limit=2, discovered=set())
    s.states[MACS[2]].manual_paused = True
    s._rotate_completed()
    assert calls == []


def test_full_adapter_rotates_one_peer_when_replacement_is_discovered(phase_scheduler):
    s, clock = phase_scheduler
    calls = configure_rotation(s, clock, limit=2, discovered={MACS[2]})
    s._rotate_completed()
    assert calls == [('disconnect', MACS[0])]
    assert s.states[MACS[1]].status == 'connected'


def test_ble_retry_backoff_is_per_peer_and_resets_after_valid_sample(phase_scheduler):
    from test_field_v0612 import FRAME
    s, clock = phase_scheduler
    s.settings.gateway_driver = 'ble'
    state = s.states[MAC]
    for expected in (10, 20, 40, 80, 120, 120):
        asyncio.run(s._handle_event(GatewayEvent('error', MAC, message='BLE_SERVICE_MISMATCH')))
        assert state.retry_at - clock[0] == expected
        assert s.states[MACS[1]].failures == 0
    asyncio.run(s._handle_event(GatewayEvent('connected', MAC, handle=7)))
    asyncio.run(s._handle_event(GatewayEvent('notify', MAC, handle=7, payload=FRAME)))
    assert state.latest and state.failures == 0
