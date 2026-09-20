import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database
from app.gateway import GatewayEvent, SerialGateway, parse_gateway_line
from app.scheduler import DeviceRuntime, Scheduler
from test_gateway import FakeSerial, run_commands

MACS = ['02A000000001', '02A000000002', '02A000000003']
REPLAY = json.loads((Path(__file__).parent / 'fixtures/notify-replay.json').read_text())


@pytest.mark.parametrize('reply', [['ERROR'], [REPLAY['valid'], 'OK'], []])
def test_unconfirmed_scan_stop_never_sends_connection(reply):
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = 0.01
    g._running.set()
    g._serial = FakeSerial(lambda _: [g._receive_line(line) for line in reply])
    g.connect(MACS[0])
    run_commands(g)
    assert g._serial.writes == ['AT+SCAN=0'] * (1 if reply == ['ERROR'] else 3)
    assert g._faulted and g.first_fault
    first = g.first_fault.copy()
    g._receive_line('OK')  # A late reply cannot unpause the worker.
    g._receive_line(REPLAY['corrupted'][0])
    assert g._faulted and g.first_fault == first
    assert g.warning_count == 1


def test_scan_start_rejection_is_blocking():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line('ERROR'))
    g._execute('AT+SCAN=1', None)
    assert g._faulted and g.scanning is not True


def test_notify_and_unrelated_disconnect_ok_do_not_ack_scan_stop():
    g = SerialGateway('COM5', 115200)
    g._pending_command = 'AT+SCAN=0'
    for line in [REPLAY['valid'], 'OK', f'+DISCON:2,0,{MACS[0]},8', 'OK']:
        g._receive_line(line)
        assert g._terminal is None
    g._receive_line('OK')
    assert g._terminal == 'OK'


def test_disconnect_does_not_resume_scanning():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g.active_links[MACS[0]] = 0
    def respond(_):
        g._receive_line('OK')  # Some firmware acknowledges before the event.
        assert g._terminal is None
        g._receive_line(f'+DISCON:2,0,{MACS[0]},22')
        g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g.disconnect(MACS[0])
    run_commands(g)
    assert g._serial.writes == [f'AT+DISCON=,{MACS[0]}']
    assert not g.active_links


@pytest.mark.parametrize('line', REPLAY['corrupted'])
def test_field_corruption_is_dropped_as_warning(line):
    e = parse_gateway_line(line)
    assert e.kind == 'warning' and e.mac == MACS[0] and e.payload is None


@pytest.mark.parametrize('replacement', [',1,32,', ',0,31,', ',0,-1,'])
def test_notification_type_and_length_are_checked(replacement):
    assert parse_gateway_line(REPLAY['valid'].replace(',0,32,', replacement)).kind == 'warning'


def test_serial_read_fault_survives_later_notification_warning():
    class BrokenSerial(FakeSerial):
        def read(self, size):
            return b''
        @property
        def in_waiting(self):
            raise OSError('ReadFile failed')
    g = SerialGateway('COM5', 115200)
    g._serial = BrokenSerial()
    g._running.set()
    g._read_loop()
    g._receive_line(REPLAY['corrupted'][0])
    assert not g.online and g.busy
    assert 'ReadFile failed' in g.first_fault['message']
    assert g.warning_count == 1 and len(g.history) == 2


class PhaseGateway:
    busy = False
    scanning = False
    scan_started_at = None
    def __init__(self, clock):
        self.clock = clock
        self.active_links = {}
        self.calls = []
    def scan(self):
        assert not self.active_links
        self.calls.append(('scan',))
        self.scanning = True
        self.scan_started_at = self.clock[0]
    def stop_scan(self):
        self.calls.append(('stop_scan',))
        self.scanning = False
    def connect(self, mac):
        assert not self.scanning
        self.calls.append(('connect', mac))
    def disconnect(self, mac):
        self.calls.append(('disconnect', mac))


@pytest.fixture
def phase_scheduler(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('app.scheduler.time.monotonic', lambda: clock[0])
    async def publish(_):
        pass
    s = Scheduler(Database(tmp_path / 'test.db'),
                  Settings(gateway_driver='serial', max_connections=2, dwell_seconds=60), publish)
    s.gateway = PhaseGateway(clock)
    s.states = {mac: DeviceRuntime(mac) for mac in MACS}
    s._device_configs = {mac: {'mac': mac} for mac in MACS}
    return s, clock


def event(s, kind, mac, handle=0):
    if kind == 'connected':
        s.gateway.active_links[mac] = handle
    if kind == 'disconnected':
        s.gateway.active_links.pop(mac, None)
    asyncio.run(s._handle_event(GatewayEvent(kind, mac, handle=handle)))


def discover(s, clock, macs):
    s._fill_connections()
    assert s.gateway.calls[-1] == ('scan',)
    clock[0] += 9
    for mac in macs:
        event(s, 'scan', mac)
    s._fill_connections()
    assert s.gateway.calls[-1] == ('stop_scan',)


def test_frozen_batch_survives_slow_connection_and_never_scans_over_links(phase_scheduler):
    s, clock = phase_scheduler
    discover(s, clock, MACS[:2])
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[0])
    clock[0] += 40
    event(s, 'connected', MACS[0])
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[1])
    event(s, 'connected', MACS[1], 1)
    clock[0] += 3
    s._fill_connections()
    assert [c for c in s.gateway.calls if c[0] == 'scan'] == [('scan',)]


def test_dwell_drains_whole_batch_then_requires_fresh_discovery(phase_scheduler):
    s, clock = phase_scheduler
    for i, mac in enumerate(MACS[:2]):
        event(s, 'connected', mac, i)
        s.states[mac].last_discovered = 90
    clock[0] += 65
    s._fill_connections()
    assert s.gateway.calls[-1] == ('disconnect', MACS[0])
    event(s, 'disconnected', MACS[0])
    clock[0] += 2
    s._fill_connections()
    assert s.gateway.calls[-1] == ('disconnect', MACS[1])
    event(s, 'disconnected', MACS[1])
    clock[0] += 2
    s._fill_connections()
    assert s.gateway.calls[-1] == ('scan',)
    clock[0] += 9
    s._fill_connections()
    assert s.gateway.calls[-1] == ('scan',)  # Old RSSI cannot start the new batch.
    event(s, 'scan', MACS[2])
    s._fill_connections()
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[2])


def test_unseen_focus_releases_current_link_to_rediscover(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    clock[0] += 3
    s.request_focus(MACS[1])
    s._fill_connections()
    assert s.gateway.calls[-1] == ('disconnect', MACS[0])
    event(s, 'disconnected', MACS[0])
    clock[0] += 2
    discover(s, clock, [MACS[0]])  # Missing focus must not cause an endless drain loop.
    s._fill_connections()
    event(s, 'connected', MACS[0])
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[0])


def test_active_focus_postpones_scan_until_cleared(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    s.settings.max_connections = 1
    s.request_focus(MACS[0])
    clock[0] += 65
    s._fill_connections()
    assert s.gateway.calls == []
    s.clear_focus()
    s._fill_connections()
    assert s.gateway.calls == [('disconnect', MACS[0])]


def test_adopted_unknown_link_is_released_before_scan(phase_scheduler):
    s, _ = phase_scheduler
    s.gateway.active_links['02A000009999'] = 0
    s._fill_connections()
    assert s.gateway.calls == [('disconnect', '02A000009999')]


def test_bad_notify_clears_partial_frame_without_hiding_fault_or_disconnect(phase_scheduler):
    s, _ = phase_scheduler
    mac = MACS[0]
    event(s, 'connected', mac)
    s.gateway_error = 'first blocking fault'
    s.decoder.feed(mac, b'\x55\x61\x01')
    asyncio.run(s._handle_event(parse_gateway_line(REPLAY['corrupted'][0])))
    assert mac not in s.decoder._buffers
    assert s.states[mac].status == 'connected'
    assert s.gateway_error == 'first blocking fault'
    asyncio.run(s._handle_event(parse_gateway_line(REPLAY['valid'])))
    assert s.states[mac].latest['temperature'] == 26.26


def test_repeated_focus_between_connected_devices_keeps_both_streams(phase_scheduler):
    s, clock = phase_scheduler
    for i, mac in enumerate(MACS[:2]):
        event(s, 'connected', mac, i)
    clock[0] += 3
    for mac in MACS[:2] * 4:
        s.request_focus(mac)
        s._fill_connections()
        for peer in MACS[:2]:
            asyncio.run(s._handle_event(parse_gateway_line(REPLAY['valid'].replace(MACS[0], peer))))
            assert s.status_dict(peer)['collecting']
        assert s.focus_mac == mac
    assert not s.gateway.calls


def test_restart_with_one_adopted_link_refills_without_waiting_for_dwell(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls == [('disconnect', MACS[0])]
    event(s, 'disconnected', MACS[0])
    clock[0] += 2
    discover(s, clock, MACS[:2])
    s._fill_connections()
    first = s.gateway.calls[-1][1]
    event(s, 'connected', first)
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls[-1][0] == 'connect'
    assert s.gateway.calls[-1][1] != first


def test_focus_waits_for_later_advertisement_in_same_discovery_window(phase_scheduler):
    s, clock = phase_scheduler
    s.request_focus(MACS[1])
    s._fill_connections()
    event(s, 'scan', MACS[0])
    clock[0] += 4
    s._fill_connections()
    assert s.gateway.calls[-1] == ('scan',)
    event(s, 'scan', MACS[1])
    s._fill_connections()
    assert s.gateway.calls[-1] == ('stop_scan',)
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[1])


def test_focus_in_frozen_batch_uses_free_slot_without_disconnecting(phase_scheduler):
    s, clock = phase_scheduler
    discover(s, clock, MACS[:2])
    s._fill_connections()
    event(s, 'connected', MACS[0])
    clock[0] += 40  # Old moving ten-second deadline must not discard peer.
    s.request_focus(MACS[1])
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[1])
    assert not any(call[0] == 'disconnect' for call in s.gateway.calls)


def test_failed_candidate_does_not_prevent_other_frozen_candidates(phase_scheduler):
    s, clock = phase_scheduler
    discover(s, clock, MACS)
    s._fill_connections()
    asyncio.run(s._handle_event(GatewayEvent('error', MACS[0], message='TIMEOUT')))
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[1])
    assert s.states[MACS[0]].status == 'retrying'


def test_query_adopts_two_distinct_live_links_and_never_starts_scan():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    def respond(command):
        assert command == 'AT+CNNI='
        g._receive_line('+CNB:2')
        for handle, mac in enumerate(MACS[:2]):
            g._receive_line(f'+NOTIFY:{handle},{mac},15,FFE4,0,4,55610000')
            g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g.send('AT+CNNI=')
    g.send('AT+SCAN=1')
    run_commands(g)
    assert not g._faulted
    assert g.active_links == {MACS[0]: 0, MACS[1]: 1}
    assert len([e for e in g.poll() if e.kind == 'connected']) == 2
    assert g._serial.writes == ['AT+CNNI=']


@pytest.mark.parametrize('peers', [
    [(0, MACS[0]), (0, MACS[1])],  # Handle reuse is not two links.
    [(0, MACS[0]), (1, MACS[0])],  # One MAC is not two devices.
    [(0, MACS[0])],                # Count disagrees with evidence.
])
def test_two_link_query_rejects_ambiguous_evidence(peers):
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    def respond(_):
        g._receive_line('+CNB:2')
        for handle, mac in peers:
            g._receive_line(f'+NOTIFY:{handle},{mac},15,FFE4,0,4,55610000')
            g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g._execute('AT+CNNI=', None)
    assert g._faulted
    assert not any(e.kind == 'connected' for e in g.poll())


def test_lost_disconnect_retries_same_mac_then_consumes_duplicate_ack():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    command = f'AT+DISCON=,{MACS[0]}'
    def respond(_):
        if len(g._serial.writes) == 1:
            # Damaged traffic and its OK never prove a disconnect.
            g._receive_line(REPLAY['corrupted'][0])
            g._receive_line('OK')
            return
        g._receive_line(f'+DISCON:2,0,{MACS[0]},22')
        g._receive_line('OK')
        g._receive_line('ERROR')  # Late duplicate reply retains same owner.
    g._serial = FakeSerial(respond)
    result = g._execute(command, MACS[0])
    assert result.kind == 'disconnected'
    assert not g._faulted and g._disconnect_retries == 1
    assert g._serial.writes == [command, command]
    g._serial.callback = lambda _: g._receive_line('ERROR')
    g._execute('AT+OP?', None)
    assert g._terminal == 'ERROR'  # No previous ACK completes the next query.


def test_disconnect_retry_exhaustion_faults_without_claiming_success():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    g._serial = FakeSerial(lambda _: [g._receive_line(line) for line in [REPLAY['valid'], 'OK']])
    g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert g._faulted and g._disconnect_retries == 2
    assert len(g._serial.writes) == 3
    assert not any(e.kind == 'disconnected' for e in g.poll())


def test_disconnect_result_without_terminator_is_not_retried():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line(f'+DISCON:2,0,{MACS[0]},22'))
    g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert g._faulted and len(g._serial.writes) == 1


def test_lost_firmware_query_requires_op_payload_on_retry():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    def respond(_):
        if len(g._serial.writes) == 2:
            g._receive_line('+OP:V1.5,EW-DTU02-M,000000000000')
        g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g._execute('AT+OP?', None)
    assert not g._faulted and g._command_retries == 1
    assert g._serial.writes == ['AT+OP?', 'AT+OP?']
