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
