import asyncio
import pytest
from app.config import Settings
from app.gateway import GatewayEvent, SerialGateway, parse_gateway_line
from app.protocol import WtvbStreamDecoder, parse_wtvb01_frame
from test_scan_phases import MACS, REPLAY, event, phase_scheduler, sample

FRAME = bytes.fromhex(REPLAY['valid'].rsplit(',', 1)[1])


def notify(s, mac, handle=0, payload=FRAME):
    asyncio.run(s._handle_event(GatewayEvent('notify', mac, payload=payload, handle=handle)))


def test_early_notification_commits_once_after_matching_connection(phase_scheduler):
    s, clock = phase_scheduler
    state = s.states[MACS[0]]
    s._connect(state, clock[0])
    notify(s, state.mac)
    received_at = clock[0]
    assert state.last_sample_at is None and len(state.early_notifications) == 1
    clock[0] += 3
    asyncio.run(s._handle_event(GatewayEvent('connected', state.mac, handle=0, mtu=247, service_count=3)))
    assert state.last_sample_at == received_at
    assert state.latest and state.mtu == 247 and state.service_count == 3
    assert not state.early_notifications
    asyncio.run(s._handle_event(GatewayEvent('connected', state.mac, handle=0)))
    assert state.last_sample_at == received_at


def test_early_notify_wrong_handle_and_failed_session_never_commit(phase_scheduler):
    s, clock = phase_scheduler
    state = s.states[MACS[0]]
    s._connect(state, clock[0])
    notify(s, state.mac, handle=9)
    asyncio.run(s._handle_event(GatewayEvent('connected', state.mac, handle=0)))
    assert state.last_sample_at is None
    s._connect(state, clock[0])
    notify(s, state.mac)
    asyncio.run(s._handle_event(GatewayEvent('error', state.mac, message='TIMEOUT')))
    assert not state.early_notifications and state.latest is None


def test_old_handle_paused_and_unconnected_notifications_are_ignored(phase_scheduler):
    s, _ = phase_scheduler
    state = s.states[MACS[0]]
    notify(s, state.mac)
    assert state.latest is None
    asyncio.run(s._handle_event(GatewayEvent('connected', state.mac, handle=3)))
    notify(s, state.mac, handle=0)
    assert state.latest is None
    notify(s, state.mac, handle=3)
    assert state.latest is not None
    asyncio.run(s.pause_device(state.mac))
    notify(s, state.mac, handle=3)
    assert state.last_sample_at is None


def test_early_notification_buffer_is_bounded(phase_scheduler):
    s, clock = phase_scheduler
    state = s.states[MACS[0]]
    s._connect(state, clock[0])
    for _ in range(80): notify(s, state.mac)
    assert len(state.early_notifications) == 32
    s._connect(state, clock[0])
    assert not state.early_notifications


@pytest.mark.parametrize('recovers', [False, True])
def test_second_connection_timeout_has_bounded_recovery(recovers, phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    s.gateway.busy = True
    clock[0] += 40
    s._check_timeouts()
    assert not s.gateway.calls
    s.gateway.busy = False
    s.gateway.connection_finished_sequence = 1
    s.gateway.connection_finished_at = clock[0]
    s._check_timeouts()
    assert not s.gateway.calls and not s.status_dict(MACS[0])['collecting']
    clock[0] += 9
    s._check_timeouts()
    assert not s.gateway.calls
    if recovers: sample(s, MACS[0])
    clock[0] += 2
    s._check_timeouts()
    if recovers:
        assert s.states[MACS[0]].status == 'connected'
    else:
        assert s.gateway.calls == [('no_data', MACS[0]), ('disconnect', MACS[0])]


def test_failed_candidate_requires_new_discovery(phase_scheduler):
    s, clock = phase_scheduler
    state = s.states[MACS[0]]
    state.last_discovered = clock[0]
    s._serial_candidates = {state.mac}
    s._serial_batch = [state.mac]
    asyncio.run(s._handle_event(GatewayEvent('error', state.mac, message='TIMEOUT')))
    assert not s._serial_candidates and not s._serial_batch
    clock[0] += 11
    s._fill_connections()
    assert s.gateway.calls == [('scan',)]


def test_stale_candidates_do_not_disrupt_healthy_peer(phase_scheduler):
    s, clock = phase_scheduler
    event(s, 'connected', MACS[0])
    state = s.states[MACS[1]]
    state.last_discovered = clock[0] - 346
    s._serial_batch = [state.mac]
    s._serial_candidates = {state.mac}
    clock[0] += 3
    sample(s, MACS[0])
    s._fill_connections()
    assert not s.gateway.calls and not s._serial_candidates


@pytest.mark.parametrize('length', [28, 32])
def test_explicit_frame_formats_split_and_combined(length):
    decoder = WtvbStreamDecoder(length)
    frame = FRAME[:length]
    assert decoder.feed(MACS[0], frame[:9]) == []
    assert len(decoder.feed(MACS[0], frame[9:])) == 1
    assert len(decoder.feed(MACS[0], frame * 3)) == 3


def test_default_32_format_preserves_28_plus_4_fragment():
    decoder = WtvbStreamDecoder()
    assert decoder.feed(MACS[0], FRAME[:28]) == []
    assert len(decoder.feed(MACS[0], FRAME[28:])) == 1


@pytest.mark.parametrize('length', [28, 32])
def test_velocity_matches_vendor_unsigned_fields(length):
    frame = bytearray(FRAME[:length])
    frame[2:4] = (40000).to_bytes(2, 'little')
    assert parse_wtvb01_frame(MACS[0], bytes(frame)).velocity_x == 40000


@pytest.mark.parametrize('profile', [0, 1, 2, 3])
def test_fixed_profile_does_not_rotate_after_failure(profile):
    g = SerialGateway('COM3', 115200, connection_profile=profile)
    g.addresses[MACS[0]] = (0, 1)
    g.connect(MACS[0])
    first, _ = g._commands.get_nowait()
    g._finish_connection(GatewayEvent('error', MACS[0], message='TIMEOUT'))
    g.connect(MACS[0])
    assert g._commands.get_nowait()[0] == first


def test_old_address_is_not_sent_to_gateway(monkeypatch):
    g = SerialGateway('COM3', 115200)
    g.addresses[MACS[0]] = (9, 1)
    g.address_seen_at[MACS[0]] = 100
    monkeypatch.setattr('app.gateway.time.monotonic', lambda: 446)
    g.connect(MACS[0])
    assert ',9,1,' not in g._commands.get_nowait()[0]


@pytest.mark.parametrize('line', ['+CONN:2,0,FE6DF407B3E4,3,247', '+CONN:0,FE6DF407B3E4,3,247'])
def test_connection_metadata(line):
    result = parse_gateway_line(line)
    assert result.mtu == 247 and result.service_count == 3


def test_new_settings_round_trip_and_legacy_defaults(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text('{"gateway_driver":"serial"}')
    settings = Settings.load(path)
    assert settings.sensor_frame_bytes == 32 and settings.serial_connection_profile == -1
    settings.update({'sensor_frame_bytes': 28, 'serial_connection_profile': 1})
    settings.save(path)
    assert Settings.load(path).sensor_frame_bytes == 28
    assert Settings.load(path).serial_connection_profile == 1


@pytest.mark.parametrize('values', [{'sensor_frame_bytes': 20}, {'serial_connection_profile': 4}])
def test_invalid_settings(values):
    with pytest.raises(ValueError): Settings().update(values)
