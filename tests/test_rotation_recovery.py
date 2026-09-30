import asyncio

import pytest
import serial

from app.config import Settings
from app.database import Database
from app.gateway import GatewayEvent, SerialGateway, parse_gateway_line
from app.scheduler import Scheduler
from test_gateway import FakeSerial
from test_scan_phases import MACS, REPLAY, discover, event, phase_scheduler, sample


@pytest.mark.parametrize('reason', [
    'SERIAL_COLLISION_SUSPECTED: AT+CONN=test',
    'AT_RESPONSE_TIMEOUT: AT+CONN=test',
    'SCAN_STOP_FAILED: AT_RESPONSE_TIMEOUT',
])
def test_transport_failure_retries_same_profile_without_sensor_evidence(reason):
    g = SerialGateway('COM5', 115200)
    g._profile_cursor[MACS[0]] = 0
    g._active_profile[MACS[0]] = 0
    g._finish_connection(GatewayEvent('error', MACS[0], message=reason))
    g.connect(MACS[0])
    assert g._commands.get_nowait()[0].endswith(',1,1,0')
    assert g._profile_cursor[MACS[0]] == 0


def test_valid_samples_lock_profile_across_collision_ble_error_and_stale_stream(tmp_path):
    async def publish(_):
        pass
    s = Scheduler(Database(tmp_path / 'test.db'), Settings(gateway_driver='serial'), publish)
    s._sync_devices()
    mac = 'FE6DF407B3E4'
    g = s.gateway
    g._active_profile[mac] = 0
    # Field logs show samples can precede the final GATT-discovery OK.
    s.states[mac].status = 'connecting'
    asyncio.run(s._handle_event(parse_gateway_line(REPLAY['valid'].replace(MACS[0], mac))))
    g._finish_connection(GatewayEvent('connected', mac, handle=0))
    for incoming in g.poll():
        asyncio.run(s._handle_event(incoming))
    for reason in ['SERIAL_COLLISION_SUSPECTED', 'DISSCONNECT (BLE code 22)']:
        g.connect(mac)
        g._commands.get_nowait()
        g._finish_connection(GatewayEvent('error', mac, message=reason))
        g.report_no_data(mac)
        g.connect(mac)
        command, _ = g._commands.get_nowait()
        assert command.endswith(',1,1,0')
        assert g._validated_profile[mac] == 0
        assert g._preferred_profile[mac] == 0


def test_unvalidated_silent_connection_can_still_try_compatibility_profile():
    g = SerialGateway('COM5', 115200)
    g._finish_connection(GatewayEvent('connected', MACS[0], handle=0))
    g.report_no_data(MACS[0])
    g.connect(MACS[0])
    assert not g._commands.get_nowait()[0].endswith(',1,1,0')


def test_corrupt_background_notify_does_not_invalidate_complete_connection_reply():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    def respond(_):
        g._collision_detected = True
        g._receive_line(REPLAY['corrupted'][0])
        g._receive_line('OK')
        g._receive_line(f'+CONN:2,1,{MACS[1]},4,247')
        g._receive_line('OK')
    g._serial = FakeSerial(respond)
    result = g._execute('AT+CONN=test', MACS[1])
    assert result.kind == 'connected'
    assert g.active_links == {MACS[1]: 1}
    assert not g._desynced


def test_corruption_with_missing_connection_reply_still_requires_resync(monkeypatch):
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g._active_profile[MACS[0]] = 0
    g._serial = FakeSerial(lambda _: setattr(g, '_collision_detected', True))
    ticks = iter([0, 0, 100])
    monkeypatch.setattr('app.gateway.time.monotonic', lambda: next(ticks, 100))
    result = g._execute('AT+CONN=test', MACS[0])
    assert result.kind == 'error' and 'SERIAL_COLLISION_SUSPECTED' in result.message
    assert g._desynced and MACS[0] not in g.active_links


def test_collision_resynchronizes_then_drains_before_new_discovery(phase_scheduler):
    s, clock = phase_scheduler
    s.settings.max_connections = 3
    for handle, mac in enumerate(MACS[:2]):
        event(s, 'connected', mac, handle)
    s.states[MACS[2]].status = 'connecting'
    s._serial_batch = [MACS[2]]
    s._serial_candidates = set(MACS)
    s.gateway.busy = True  # CNNI recovery still owns the reply stream.
    asyncio.run(s._handle_event(GatewayEvent('error', MACS[2], message='SERIAL_COLLISION_SUSPECTED')))
    s._fill_connections()
    assert not s.gateway.calls
    assert s._connection_limit() == 2
    s.gateway.busy = False
    clock[0] += 3
    for mac in MACS[:2]:
        s._fill_connections()
        assert s.gateway.calls[-1] == ('disconnect', mac)
        event(s, 'disconnected', mac)
        clock[0] += 2
    discover(s, clock, MACS)
    s._fill_connections()
    assert s.gateway.calls[-1][0] == 'connect'
    # Later collisions can reduce to one, but cannot disable the queue.
    asyncio.run(s._handle_event(GatewayEvent('error', MACS[2], message='SERIAL_COLLISION_SUSPECTED')))
    assert s._connection_limit() == 1
    asyncio.run(s._handle_event(GatewayEvent('error', MACS[2], message='SERIAL_COLLISION_SUSPECTED')))
    assert s._connection_limit() == 1


def test_stale_data_requires_new_advertisement_instead_of_old_frozen_batch(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    s._serial_candidates = {MACS[0]}
    clock[0] += 21
    s._check_timeouts()
    assert s.gateway.calls[-1] == ('disconnect', MACS[0])
    event(s, 'disconnected', MACS[0])
    clock[0] += 31
    s._fill_connections()
    assert s.gateway.calls[-1] == ('scan',)


def test_usb_read_failure_reopens_port_and_keeps_validated_profile_and_manual_pause(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.scheduler.time.monotonic', lambda: clock[0])
    async def publish(_):
        pass
    s = Scheduler(Database(tmp_path / 'test.db'), Settings(gateway_driver='serial'), publish)
    s._sync_devices()
    mac = 'FE6DF407B3E4'
    paused = 'C2372102DEEF'
    s.states[paused].manual_paused = True
    s.states[mac].last_cycle_at = 80
    s._serial_safe_limit = 2
    previous = s.gateway
    previous._validated_profile[mac] = 0

    class BrokenSerial(FakeSerial):
        in_waiting = 0
        def read(self, size):
            raise serial.SerialException('ReadFile failed (PermissionError 158)')

    previous._serial = BrokenSerial()
    previous._running.set()
    previous._read_loop()
    assert not previous.online and previous.io_failed
    replacements = []
    def open_replacement(gateway):
        replacements.append(gateway)
        gateway._serial = FakeSerial()
        gateway._running.set()
    monkeypatch.setattr(SerialGateway, 'start', open_replacement)
    assert asyncio.run(s._recover_serial_port())
    clock[0] += 4
    asyncio.run(s._recover_serial_port())
    assert replacements == []
    clock[0] += 1
    asyncio.run(s._recover_serial_port())
    assert len(replacements) == 1 and not previous._serial.is_open
    assert s.gateway.online and s.gateway is not previous
    assert s.states[paused].status == 'paused'
    assert s.states[mac].last_cycle_at == 80
    assert s._connection_limit() == 2
    s.gateway.connect(mac)
    assert s.gateway._commands.get_nowait()[0].endswith(',1,1,0')
    # Reopening the USB port does not disconnect BLE: adopt queried live links.
    s.gateway.active_links[mac] = 0
    asyncio.run(s._adopt_registered_links())
    assert s.states[mac].status == 'connected'
    assert s.states[paused].status == 'paused'
    s.gateway.stop()


def test_unavailable_port_uses_backoff_without_repeated_open_each_loop(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.scheduler.time.monotonic', lambda: clock[0])
    async def publish(_):
        pass
    s = Scheduler(Database(tmp_path / 'test.db'), Settings(gateway_driver='serial'), publish)
    s.gateway.io_failed = True
    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append(clock[0])
        raise serial.SerialException('COM5 unavailable')
    monkeypatch.setattr('app.gateway.serial.Serial', unavailable)
    for tick in [100, 101, 104, 105, 106, 110, 115, 116, 130, 135, 136]:
        clock[0] = tick
        asyncio.run(s._recover_serial_port())
    assert attempts == [105, 115, 135]
    assert s.gateway.io_failed and not s.gateway.online
    assert s._gateway_restarts == 3
