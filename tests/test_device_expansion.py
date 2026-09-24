import asyncio
import threading

from app.gateway import GatewayEvent, SerialGateway
from test_gateway import FakeSerial, run_commands
from test_scan_phases import MACS, REPLAY, discover, event, phase_scheduler, sample
from app.gateway import parse_gateway_line


def test_acknowledged_disconnect_waits_past_old_five_second_window():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g.DISCONNECT_TIMEOUT_SECONDS = .3
    g._running.set()
    def respond(_):
        g._receive_line('OK')
        # Scale the six-second field delay down for this test.
        threading.Timer(.06, lambda: [g._receive_line(line) for line in
            [f'+DISCON:2,0,{MACS[0]},34', 'OK']]).start()
    g._serial = FakeSerial(respond)
    result = g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert result.kind == 'disconnected'
    assert not g._faulted and len(g._serial.writes) == 1


def test_disconnect_error_waits_for_matching_delayed_completion():
    g = SerialGateway('COM5', 115200)
    g.DISCONNECT_TIMEOUT_SECONDS = .2
    g._running.set()
    def respond(_):
        g._receive_line('ERROR')
        g._receive_line(f'+DISCON:2,0,{MACS[1]},8')
        g._receive_line('OK')
        assert g._terminal is None
        threading.Timer(.03, lambda: [g._receive_line(line) for line in
            [f'+DISCON:2,0,{MACS[0]},34', 'OK']]).start()
    g._serial = FakeSerial(respond)
    result = g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert result.kind == 'disconnected' and not g._faulted
    assert len(g._serial.writes) == 1


def test_disconnect_ack_alone_does_not_claim_success_or_repeat_command():
    g = SerialGateway('COM5', 115200)
    g.DISCONNECT_TIMEOUT_SECONDS = .02
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line('OK'))
    result = g._execute(f'AT+DISCON=,{MACS[0]}', MACS[0])
    assert result.kind == 'error' and g._desynced and not g._faulted
    assert len(g._serial.writes) == 1


def test_connected_without_data_blocks_next_attempt_then_isolates_only_peer(phase_scheduler):
    s, clock = phase_scheduler
    discover(s, clock, MACS)
    s._fill_connections()
    event(s, 'connected', MACS[0])
    s.states[MACS[0]].last_sample_at = None
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[0])
    clock[0] += 18
    s._check_timeouts()
    assert s.gateway.calls[-1] == ('disconnect', MACS[0])
    assert s.states[MACS[0]].recovery_reason
    event(s, 'disconnected', MACS[0])
    clock[0] += 2
    s._fill_connections()
    assert s.gateway.calls[-1] == ('connect', MACS[1])
    assert s.states[MACS[0]].status == 'retrying'
    assert s.states[MACS[0]].retry_at > clock[0]


def test_silent_peer_retries_from_batch_without_draining_healthy_peer(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    s._serial_candidates = set(MACS[:2])
    failed = s.states[MACS[1]]
    failed.status = 'retrying'
    failed.retry_at = clock[0] + 30
    s.states.pop(MACS[2])
    clock[0] += 10
    sample(s, MACS[0])
    s._fill_connections()
    assert s.gateway.calls == []
    clock[0] += 21
    sample(s, MACS[0])
    s._fill_connections()
    assert s.gateway.calls == [('connect', MACS[1])]


def test_long_batch_does_not_disconnect_just_connected_sensor(phase_scheduler):
    s, clock = phase_scheduler
    s._serial_refresh_at = clock[0] + 30
    clock[0] += 50
    event(s, 'connected', MACS[0])
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls == []
    assert s._serial_refresh_at > clock[0]


def test_no_data_advances_profile_and_successful_sample_resets_failure(phase_scheduler):
    g = SerialGateway('COM5', 115200)
    g._preferred_profile[MACS[0]] = 0
    g.report_no_data(MACS[0])
    assert MACS[0] not in g._preferred_profile and g._profile_cursor[MACS[0]] == 1
    s, clock = phase_scheduler
    s.stat