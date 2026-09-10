import time
import asyncio
from types import SimpleNamespace

from app.gateway import GatewayEvent
from app.scheduler import DeviceRuntime, Scheduler, select_victim


def test_focus_never_selected_as_victim():
    now = time.monotonic()
    focus = DeviceRuntime("FE6DF407B3E4", "connected", connected_at=now - 500)
    normal = DeviceRuntime("02A000000001", "connected", connected_at=now - 200)
    assert select_victim([focus, normal], focus.mac, now, 180) is normal


def test_completed_sampling_window_is_preempted_first():
    now = time.monotonic()
    completed = DeviceRuntime("02A000000001", "connected", connected_at=now - 200)
    fresh = DeviceRuntime("02A000000002", "connected", connected_at=now - 30)
    assert select_victim([fresh, completed], None, now, 180) is completed


def test_scan_does_not_add_unregistered_neighbour_device():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.states = {"FE6DF407B3E4": DeviceRuntime("FE6DF407B3E4")}
    scheduler.gateway_error = None
    asyncio.run(scheduler._handle_event(GatewayEvent("scan", "CCB5D1682B71", rssi=-50)))
    assert set(scheduler.states) == {"FE6DF407B3E4"}


def test_only_one_connection_attempt_is_started_at_a_time():
    class FakeGateway:
        def __init__(self):
            self.calls = []

        def connect(self, mac):
            self.calls.append(mac)

    scheduler = Scheduler.__new__(Scheduler)
    scheduler.settings = SimpleNamespace(max_connections=5, gateway_driver="simulator", dwell_seconds=180)
    scheduler.gateway = FakeGateway()
    scheduler.focus_mac = None
    scheduler._connect_ready_at = 0.0
    scheduler.states = {
        "F8C5C0B8917E": DeviceRuntime("F8C5C0B8917E"),
        "E358B14B81B5": DeviceRuntime("E358B14B81B5"),
        "E91470052EF9": DeviceRuntime("E91470052EF9"),
    }

    scheduler._fill_connections()
    scheduler._fill_connections()

    assert len(scheduler.gateway.calls) == 1
    assert sum(state.status == "connecting" for state in scheduler.states.values()) == 1


def test_busy_gateway_prevents_rotation_and_new_connection(tmp_path):
    from app.config import Settings
    from app.database import Database
    async def publish(_):
        pass
    scheduler = Scheduler(Database(tmp_path / 'test.db'), Settings(), publish)
    calls = []
    scheduler.gateway = SimpleNamespace(busy=True, disconnect=lambda mac: calls.append(mac), connect=lambda mac: calls.append(mac))
    scheduler.states = {
        'F8C5C0B8917E': DeviceRuntime('F8C5C0B8917E', 'connected', connected_at=time.monotonic() - 500),
        'C2372102DEEF': DeviceRuntime('C2372102DEEF'),
    }
    scheduler._rotate_completed()
    scheduler._fill_connections()
    assert calls == []


def test_failed_device_yields_to_waiting_device(tmp_path):
    from app.config import Settings
    from app.database import Database
    async def publish(_):
        pass
    scheduler = Scheduler(Database(tmp_path / 'test.db'), Settings(), publish)
    scheduler._sync_devices()
    failed = scheduler.states['E8C5C0B8917E']
    asyncio.run(scheduler._handle_event(GatewayEvent('error', failed.mac, message='TIMEOUT')))
    failed.retry_at = 0
    scheduler._connect_ready_at = 0
    scheduler._fill_connections()
    assert failed.status == 'retrying'
    assert any(s.status == 'connecting' for s in scheduler.states.values())


def test_connected_without_sample_is_not_collecting(tmp_path):
    from app.config import Settings
    from app.database import Database
    async def publish(_):
        pass
    scheduler = Scheduler(Database(tmp_path / 'test.db'), Settings(), publish)
    scheduler._sync_devices()
    mac = 'C2372102DEEF'
    asyncio.run(scheduler._handle_event(GatewayEvent('connected', mac, handle=0)))
    assert scheduler.status_dict(mac)['collecting'] is False
    scheduler.states[mac].last_sample_at = time.monotonic()
    assert scheduler.status_dict(mac)['collecting'] is True
    scheduler.states[mac].last_sample_at -= 20
    assert scheduler.status_dict(mac)['collecting'] is False


def test_disabling_connected_sensor_releases_link(tmp_path):
    from app.config import Settings
    from app.database import Database
    async def publish(_):
        pass
    database = Database(tmp_path / 'test.db')
    scheduler = Scheduler(database, Settings(), publish)
    scheduler._sync_devices()
    mac = 'C2372102DEEF'
    scheduler.states[mac].status = 'connected'
    device = database.get_device_by_mac(mac)
    database.update_device(device['id'], {'enabled': False})
    scheduler._sync_devices()
    assert scheduler.states[mac].status == 'disconnecting'
    for event in scheduler.gateway.poll():
        asyncio.run(scheduler._handle_event(event))
    scheduler._sync_devices()
    assert mac not in scheduler.states


def test_field_disconnect_clears_phantom_connection(tmp_path):
    from app.config import Settings
    from app.database import Database
    from app.gateway import parse_gateway_line
    async def publish(_):
        pass
    scheduler = Scheduler(Database(tmp_path / 'test.db'), Settings(), publish)
    scheduler._sync_devices()
    mac = 'FE6DF407B3E4'
    runtime = scheduler.states[mac]
    runtime.status = 'connected'
    runtime.last_sample_at = time.monotonic()
    runtime.handle = 0
    scheduler.decoder.feed(mac, b'\x55\x61')
    asyncio.run(scheduler._handle_event(parse_gateway_line('+DISCON:2,0,FE6DF407B3E4,8')))
    assert scheduler.snapshot()['gateway']['connected'] == 0
    assert runtime.status == 'queued'
    assert runtime.handle is None and runtime.last_sample_at is None
    assert mac not in scheduler.decoder._buffers
    assert '8' in scheduler.status_dict(mac)['error']
