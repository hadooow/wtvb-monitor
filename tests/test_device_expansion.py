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
    s.states[MACS[0]].failures = 2
    asyncio.run(s._handle_event(GatewayEvent('connected', MACS[0], handle=0)))
    assert s.states[MACS[0]].failures == 2
    sample(s, MACS[0])
    assert s.states[MACS[0]].failures == 0


def test_query_keeps_ownership_until_all_connection_records_end():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .1
    g._running.set()
    def respond(_):
        g._receive_line('+CNB:2')
        g._receive_line(f'+CONN:2,0,{MACS[0]},3,247')
        g._receive_line('OK')
        assert g._terminal is None and not g.active_links
        threading.Timer(.025, lambda: [g._receive_line(line) for line in
            [f'+CONN:2,1,{MACS[1]},3,247', 'OK']]).start()
    g._serial = FakeSerial(respond)
    g._execute('AT+CNNI=', None)
    assert not g._faulted and len(g.active_links) == 2
    assert len([e for e in g.poll() if e.kind == 'connected']) == 2


def test_query_does_not_accept_missing_second_terminator():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .02
    g._running.set()
    g._serial = FakeSerial(lambda _: [g._receive_line(line) for line in [
        '+CNB:2', f'+CONN:2,0,{MACS[0]},3,247', 'OK', f'+CONN:2,1,{MACS[1]},3,247']])
    g._execute('AT+CNNI=', None)
    assert g._desynced and not g._faulted and not g.active_links


def test_single_sample_does_not_immediately_start_next_connection(phase_scheduler):
    s, clock = phase_scheduler
    discover(s, clock, MACS[:2])
    s._fill_connections()
    asyncio.run(s._handle_event(GatewayEvent('connected', MACS[0], handle=0)))
    s.gateway.active_links[MACS[0]] = 0
    for tick in range(6):
        asyncio.run(s._handle_event(parse_gateway_line(REPLAY['valid'])))
        s._fill_connections()
        if tick < 5:
            assert s.gateway.calls[-1] == ('connect', MACS[0])
        clock[0] += 1
    assert s.gateway.calls[-1] == ('connect', MACS[1])


def test_registration_adopts_link_whose_startup_event_was_already_consumed(phase_scheduler):
    s, clock = phase_scheduler
    s.gateway.active_links[MACS[1]] = 3
    asyncio.run(s._adopt_registered_links())
    assert s.states[MACS[1]].status == 'connected'
    assert s.states[MACS[1]].handle == 3
    sample(s, MACS[1])
    assert s.status_dict(MACS[1])['collecting']
    previous = s.states[MACS[1]].last_sample_at
    asyncio.run(s._adopt_registered_links())
    assert s.states[MACS[1]].last_sample_at == previous


def test_registration_does_not_adopt_unconfirmed_or_disconnecting_link(phase_scheduler):
    s, clock = phase_scheduler
    asyncio.run(s._adopt_registered_links())
    assert s.states[MACS[1]].status == 'queued'
    s.gateway.active_links[MACS[1]] = 3
    s.gateway.busy = True
    asyncio.run(s._adopt_registered_links())
    assert s.states[MACS[1]].status == 'queued'
    s.gateway.busy = False
    s.states[MACS[1]].status = 'disconnecting'
    asyncio.run(s._adopt_registered_links())
    assert s.states[MACS[1]].status == 'disconnecting'


def test_false_stop_ack_requires_state_readback_before_connecting():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    queries = []
    def respond(command):
        if command == 'AT+SCAN=0':
            g._receive_line('OK')
        elif command == 'AT+SCAN?':
            queries.append(command)
            scanning = int(len(queries) == 1)
            g._receive_line(f'+SCAN:{scanning},160,80,0,1,0')
            g._receive_line('OK')
        else:
            assert command.startswith('AT+CONN=') and len(queries) == 2
            g._receive_line(f'+CONN:2,0,{MACS[0]},3,247')
            g._receive_line('OK')
    g._serial = FakeSerial(respond)
    g.connect(MACS[0])
    run_commands(g)
    assert not g._faulted and MACS[0] in g.active_links
    assert g._serial.writes[:4] == ['AT+SCAN=0','AT+SCAN?'] * 2


def test_orphan_oks_cannot_confirm_scan_state():
    g = SerialGateway('COM5', 115200)
    g.COMMAND_TIMEOUT_SECONDS = .01
    g._running.set()
    g._serial = FakeSerial(lambda _: g._receive_line('OK'))
    g.connect(MACS[0])
    run_commands(g)
    assert g._faulted and not g.active_links
    assert not any(c.startswith('AT+CONN=') for c in g._serial.writes)


def test_scan_readback_error_never_authorizes_connection():
    g = SerialGateway('COM5', 115200)
    g._running.set()
    g._serial = FakeSerial(lambda cmd: g._receive_line('ERROR' if cmd == 'AT+SCAN?' else 'OK'))
    g.connect(MACS[0])
    run_commands(g)
    assert g._faulted and g._serial.writes == ['AT+SCAN=0','AT+SCAN?']
