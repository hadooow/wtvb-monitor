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
    expected = ['AT+SCAN=0'] if reply == ['ERROR'] else ['AT+SCAN=0'] * 3 + ['AT+CNNI='] * 3
    assert g._serial.writes == expected
    assert g._faulted and g.first_fault
    first = g.first_fault.copy()
    g._receive_line('OK')  # A late reply cannot unpause the worker.
    g._receive_line(REPLAY['corrupted'][0])
    assert g._faulted and g.first_fault == first
    assert g.warning_count == 1


def test_scan_start_rejection_resynchronizes_and_faults_only_at_limit():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    def respond(command):
        g._receive_line('+CNB:0' if command == 'AT+CNNI=' else 'ERROR')
    g._serial = FakeSerial(respond)
    for attempt in range(1, 4):
        g._serial.is_open = True
        g._running.set()
        g._scan_retry_at = 0
        g.scan()
        run_commands(g)
        assert g._scan_start_failures == attempt
        assert g._faulted == (attempt == 3)
        assert g.scanning is not True
        if attempt < 3:
            assert not g._desynced
            assert g._serial.writes.count('AT+CNNI=') == attempt
            assert not any(e.kind == 'error' for e in g.poll())
            g.scan()  # Backoff suppresses a new request even after resync.
            assert g._commands.empty()
    assert g._serial.writes == ['AT+SCAN=1', 'AT+CNNI='] * 2 + ['AT+SCAN=1']
    assert '连续 3 次' in g.first_fault['message']


def test_successful_scan_clears_rejection_budget():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line('ERROR'))
    g._execute('AT+SCAN=1', None)
    assert g._scan_start_failures == 1 and not g.first_fault
    g._serial.callback = lambda _: g._receive_line('OK')
    g._execute('AT+SCAN=1', None)
    assert g.scanning is True
    assert g._scan_start_failures == 0 and g._scan_retry_at == 0
    assert not g._faulted


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
    name = '测试网关'
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
    def report_no_data(self, mac):
        self.calls.append(("no_data", mac))
    def disconnect(self, mac):
        self.calls.append(('disconnect', mac))
    def diagnostics(self):
        return {'online': True, 'active_connections': len(self.active_links)}


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


def test_pause_while_connecting_only_records_intent(phase_scheduler):
    s, _ = phase_scheduler
    state = s.states[MACS[0]]
    state.status = 'connecting'
    asyncio.run(s.pause_device(state.mac))
    asyncio.run(s.pause_device(state.mac))
    assert state.manual_paused and state.status == 'connecting'
    assert not state.disconnect_requested and not s.gateway.calls
    assert '等待当前连接事务结束' in state.error


def test_pause_while_connecting_disconnects_once_on_late_success(phase_scheduler):
    s, _ = phase_scheduler
    state = s.states[MACS[0]]
    state.status = 'connecting'
    asyncio.run(s.pause_device(state.mac))
    for _ in range(2):
        asyncio.run(s._handle_event(GatewayEvent('connected', state.mac, handle=0)))
    assert state.status == 'disconnecting' and state.disconnect_requested
    assert s.gateway.calls == [('disconnect', state.mac)]


def test_pause_while_connecting_failure_finishes_paused(phase_scheduler):
    s, _ = phase_scheduler
    state = s.states[MACS[0]]
    state.status = 'connecting'
    asyncio.run(s.pause_device(state.mac))
    asyncio.run(s._handle_event(GatewayEvent(
        'error', state.mac, message='AT_RESPONSE_TIMEOUT: AT+CONN=test',
    )))
    assert state.status == 'paused' and state.manual_paused
    assert not state.disconnect_requested and not s.gateway.calls


@pytest.mark.parametrize('status', ['connecting', 'disconnecting'])
def test_resume_preserves_pending_transaction(phase_scheduler, status):
    s, _ = phase_scheduler
    state = s.states[MACS[0]]
    state.status = status
    state.manual_paused = True
    state.disconnect_requested = status == 'disconnecting'
    asyncio.run(s.resume_device(state.mac))
    assert not state.manual_paused and state.status == status
    s._fill_connections()
    assert not s.gateway.calls
    asyncio.run(s._handle_event(GatewayEvent(
        'connected' if status == 'connecting' else 'disconnected', state.mac, handle=0,
    )))
    assert state.status == ('connected' if status == 'connecting' else 'queued')


def test_pause_connecting_with_confirmed_link_disconnects(phase_scheduler):
    s, _ = phase_scheduler
    state = s.states[MACS[0]]
    state.status = 'connecting'
    s.gateway.active_links[state.mac] = 0
    asyncio.run(s.pause_device(state.mac))
    assert state.status == 'disconnecting'
    assert s.gateway.calls == [('disconnect', state.mac)]


def test_field_failure_sequence_drops_old_disconnects_and_restarts_scan(phase_scheduler, monkeypatch):
    s, clock = phase_scheduler
    first, second = MACS[:2]
    event(s, 'connected', first)
    g = SerialGateway('COM5', 115200)
    g._connected[first] = 0
    g._scanning = False
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    s.gateway = g
    s.states[second].status = 'connecting'
    s._serial_candidates = set(MACS)
    s._serial_batch = [MACS[2]]
    # Advance the transaction clock instead of waiting 60 physical seconds.
    monkeypatch.setattr(g._response, 'wait', lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr('app.gateway.time.sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    def respond(command):
        if command.startswith('AT+CONN='):
            asyncio.run(s.pause_device(second))
            assert g._commands.empty()  # No DISCON behind the ongoing CONN.
            asyncio.run(s.pause_device(first))
            # Simulate a legacy queued request as an additional worker guard.
            g.disconnect(second)
            g._receive_line(f'+DISCON:2,0,{first},8')
            g._receive_line('OK')
        elif command == 'AT+CNNI=':
            g._receive_line('+CNB:0')
        elif command == 'AT+SCAN=1':
            g._receive_line('OK')
            g._running.clear()
        else:
            pytest.fail(f'Unexpected serial write: {command}')
    g._serial = FakeSerial(respond)
    g.connect(second)
    original_get = g._commands.get
    def get_next(*args, **kwargs):
        if g._commands.empty():
            for incoming in g.poll():
                asyncio.run(s._handle_event(incoming))
            assert all(s.states[mac].status == 'paused' for mac in (first, second))
            assert not g.active_links and not g._desynced
            clock[0] += 3
            s._fill_connections()
            assert not g._commands.empty()
        return original_get(*args, **kwargs)
    monkeypatch.setattr(g._commands, 'get', get_next)
    g._command_loop()
    assert [c.split('=')[0] for c in g._serial.writes] == ['AT+CONN', 'AT+CNNI', 'AT+SCAN']
    assert not any(c.startswith('AT+DISCON=') for c in g._serial.writes)
    assert len([entry for entry in g.history if entry['kind'] == 'stale_disconnect']) == 2
    assert g._commands.unfinished_tasks == 0 and not g._faulted


def event(s, kind, mac, handle=0):
    if kind == 'connected':
        s.gateway.active_links[mac] = handle
    if kind == 'disconnected':
        s.gateway.active_links.pop(mac, None)
    asyncio.run(s._handle_event(GatewayEvent(kind, mac, handle=handle)))
    if kind == 'connected':
        sample(s, mac)


def sample(s, mac):
    asyncio.run(s._handle_event(parse_gateway_line(REPLAY['valid'].replace(MACS[0], mac))))
    # These phase fixtures represent an already established data stream.
    s.states[mac].data_stable_since = s.gateway.clock[0] - s.DATA_SETTLE_SECONDS


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


def test_dwell_finishes_batch_before_next_discovery(phase_scheduler):
    s, clock = phase_scheduler
    for i, mac in enumerate(MACS[:2]):
        event(s, 'connected', mac, i)
        s.states[mac].last_discovered = 90
    s._serial_batch = [MACS[2]]
    s._serial_candidates = set(MACS)
    s._serial_refresh_at = clock[0] + 1000
    clock[0] += 65
    for peer in MACS[:2]:
        if s.states[peer].status == "connected":
            sample(s, peer)
    s._fill_connections()
    assert s.gateway.calls[-1] == ('disconnect', MACS[0])
    event(s, 'disconnected', MACS[0])
    clock[0] += 2
    s._fill_connections()
    assert s.gateway.calls[-1] == ('disconnect', MACS[1])
    event(s, 'disconnected', MACS[1])
    clock[0] += 2
    discover(s, clock, MACS)
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[2])


def test_four_devices_rotate_by_batch_and_do_not_reconnect_completed_peer(phase_scheduler):
    s, clock = phase_scheduler
    fourth = '02A000000004'
    s.settings.max_connections = 4
    s.states[fourth] = DeviceRuntime(fourth)
    s._device_configs[fourth] = {'mac': fourth}
    for i, mac in enumerate(MACS):
        event(s, 'connected', mac, i)
    s._serial_batch = [fourth]
    s._serial_candidates = set(MACS) | {fourth}
    s._serial_refresh_at = clock[0] + 1000
    s._fill_connections()
    assert s.gateway.calls == []
    clock[0] += s.settings.dwell_seconds + 1
    for mac in MACS:
        sample(s, mac)
    s._fill_connections()
    assert s.gateway.calls == [('disconnect', MACS[0])]
    for index, mac in enumerate(MACS):
        event(s, 'disconnected', mac)
        clock[0] += 2
        if index < 2:
            s._fill_connections()
            assert s.gateway.calls[-1] == ('disconnect', MACS[index + 1])
    discover(s, clock, [*MACS, fourth])
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', fourth)
    assert not s.gateway.active_links


def test_many_device_queue_puts_completed_batch_behind_unserved_devices(phase_scheduler):
    s, clock = phase_scheduler
    macs = [f'02A00000{i:04X}' for i in range(18)]
    s.states = {mac: DeviceRuntime(mac) for mac in macs}
    s._device_configs = {mac: {'mac': mac} for mac in macs}
    s.settings.max_connections = 3
    for handle, mac in enumerate(macs[:3]):
        event(s, 'connected', mac, handle)
    clock[0] += s.settings.dwell_seconds + 1
    for mac in macs[:3]:
        sample(s, mac)
    s._fill_connections()
    for index, mac in enumerate(macs[:3]):
        assert s.gateway.calls[-1] == ('disconnect', mac)
        event(s, 'disconnected', mac)
        clock[0] += 2
        if index < 2:
            s._fill_connections()
    assert s.queue_order() == macs[3:] + macs[:3]
    discover(s, clock, macs)
    assert s._serial_batch[:3] == macs[3:6]
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', macs[3])


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
    for peer in MACS[:2]:
        if s.states[peer].status == "connected":
            sample(s, peer)
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


def test_new_registration_waits_for_dwell_before_discovery(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls == []
    s._serial_refresh_at = 0  # _sync_devices requests discovery for a new registration.
    s._fill_connections()
    assert s.gateway.calls == []
    clock[0] += s.settings.dwell_seconds
    sample(s, MACS[0])
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
    sample(s, MACS[0])
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
    g.DISCONNECT_TIMEOUT_SECONDS = .04
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
    g.DISCONNECT_TIMEOUT_SECONDS = .04
    g._running.set()
    def respond(_):
        g._receive_line('+CNB:2')
        for handle, mac in peers:
            g._receive_line(f'+NOTIFY:{handle},{mac},15,FFE4,0,4,55610000')
            g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g._execute('AT+CNNI=', None)
    assert g._desynced and not g._faulted
    assert not any(e.kind == 'connected' for e in g.poll())


def test_lost_disconnect_retries_same_mac_then_consumes_duplicate_ack():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g.DISCONNECT_TIMEOUT_SECONDS = .04
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


def test_disconnect_retry_exhaustion_marks_stream_for_resync_without_claiming_success():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g.DISCONNECT_TIMEOUT_SECONDS = .04
    g._running.set()
    g._serial = FakeSerial(lambda _: [g._receive_line(line) for line in [REPLAY['valid'], 'OK']])
    g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert g._desynced and not g._faulted and g._disconnect_retries == 2
    assert len(g._serial.writes) == 3
    assert not any(e.kind == 'disconnected' for e in g.poll())


def test_disconnect_result_without_terminator_is_not_retried():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g.DISCONNECT_TIMEOUT_SECONDS = .04
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line(f'+DISCON:2,0,{MACS[0]},22'))
    g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert g._desynced and not g._faulted and len(g._serial.writes) == 1


def test_lost_firmware_query_requires_op_payload_on_retry():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g.DISCONNECT_TIMEOUT_SECONDS = .04
    g._running.set()
    def respond(_):
        if len(g._serial.writes) == 2:
            g._receive_line('+OP:V1.5,EW-DTU02-M,000000000000')
        g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g._execute('AT+OP?', None)
    assert not g._faulted and g._command_retries == 1
    assert g._serial.writes == ['AT+OP?', 'AT+OP?']
