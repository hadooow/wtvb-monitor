import asyncio
from types import SimpleNamespace

import pytest
import serial

from app.database import Database
from app.gateway import SerialGateway, parse_gateway_line
from app.serial_ports import available_ports, resolve_port
from test_ble_gateway import Client, DEVICE, MAC, gateway
from test_scan_phases import phase_scheduler, MACS, event

BENCH = ('FE6DF407B3E4', 'E95BEA0C2DE1', 'D169C0659EA8', 'C5AE323B69BC', 'D60F8E9D1899')


def test_bench_migration_preserves_existing_settings_history_and_deletion(tmp_path):
    path = tmp_path / 'monitor.db'
    db = Database(path)
    assert all(db.get_device_by_mac(mac) for mac in BENCH)
    existing = db.get_device_by_mac(BENCH[1])
    db.update_device(existing['id'], {'name': '家里2', 'enabled': False, 'thresholds': {'temperature_warn': 50}})
    with db.connect() as conn:
        conn.execute("DELETE FROM migrations WHERE name='v0.6.14-bench-sensors'")
        conn.execute('DELETE FROM devices WHERE mac=?', (BENCH[2],))
    upgraded = Database(path)
    assert upgraded.get_device(existing['id']) == db.get_device(existing['id'])
    assert upgraded.get_device_by_mac(BENCH[2])
    assert len(upgraded.list_devices()) == 12
    upgraded.delete_device(upgraded.get_device_by_mac(BENCH[3])['id'])
    assert Database(path).get_device_by_mac(BENCH[3]) is None


def port(name, hwid, vid=None):
    return SimpleNamespace(device=name, description=name, hwid=hwid, vid=vid)


def test_port_selection_does_not_open_bluetooth_virtual_port(monkeypatch):
    ports = [port('COM3', 'BTHENUM\\virtual'), port('COM5', 'USB VID:PID=0403:6001', 0x403)]
    monkeypatch.setattr('app.serial_ports.list_ports.comports', lambda: ports)
    assert resolve_port('auto') == 'COM5'
    assert available_ports()[0]['device'] == 'COM5'
    monkeypatch.setattr('app.gateway.serial.Serial', lambda *a, **k: pytest.fail('must not open virtual COM'))
    g = SerialGateway('COM3', 115200)
    with pytest.raises(serial.SerialException, match='SERIAL_BLUETOOTH_PORT'):
        g.start()
    assert g.io_failed and not g.online
    assert 'SERIAL_BLUETOOTH_PORT' in g.poll()[0].message


def test_auto_port_requires_unique_usb_candidate(monkeypatch):
    for ports in ([], [port('COM5', 'USB', 1), port('COM7', 'USB', 2)]):
        monkeypatch.setattr('app.serial_ports.list_ports.comports', lambda: ports)
        with pytest.raises(serial.SerialException, match='SERIAL_PORT_SELECTION'):
            resolve_port('auto')
    assert resolve_port('COM1') == 'COM1'


def test_connection_timeout_retains_ble_failure_code():
    e = parse_gateway_line('+CONN:0,65535,D60F8E9D1899,22,TIMEOUT')
    assert e.kind == 'error' and e.message == 'TIMEOUT (BLE code 22)'


async def finish(g):
    while g._tasks:
        await asyncio.gather(*tuple(g._tasks))
        await asyncio.sleep(0)


def test_busy_ble_disconnect_is_queued_and_releases_only_requested_peer():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        second = MACS[1]
        class Slow(Client):
            async def connect(self):
                if self.device.address == second:
                    entered.set()
                    await release.wait()
                await super().connect()
        g = gateway(Slow)
        g.connect(MAC)
        await finish(g)
        first_client = g._clients[MAC]
        g.poll()
        g._detection(SimpleNamespace(address=second, name='second'), SimpleNamespace(rssi=-60, local_name='second'))
        g.connect(second)
        await entered.wait()
        g.disconnect(MAC)
        g.disconnect(MAC)
        assert g.diagnostics()['pending_disconnects'] == [MAC]
        assert first_client.disconnects == 0
        release.set()
        await finish(g)
        assert first_client.disconnects == 1
        assert MAC not in g.active_links and second in g.active_links
        assert len([e for e in g.poll() if e.kind == 'disconnected' and e.mac == MAC]) == 1
        await g.aclose()
    asyncio.run(run())


def test_pause_during_ble_subscription_is_not_lost():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow(Client):
            async def start_notify(self, *args):
                entered.set()
                await release.wait()
                await super().start_notify(*args)
        g = gateway(Slow)
        g.connect(MAC)
        await entered.wait()
        g.disconnect(MAC)
        release.set()
        await finish(g)
        assert not g.active_links and not g.busy
        assert [e.kind for e in g.poll()] == ['notify', 'connected', 'disconnected']
        await g.aclose()
    asyncio.run(run())


def test_old_ble_disconnect_cannot_close_new_session():
    async def run():
        g = gateway()
        g.connect(MAC)
        await finish(g)
        client = g._clients[MAC]
        await g._disconnect(MAC, expected_handle=g._handles[MAC] - 1)
        assert client.disconnects == 0 and MAC in g.active_links
        await g.aclose()
    asyncio.run(run())


def test_ble_waits_for_first_valid_data_before_adding_peer(phase_scheduler):
    s, clock = phase_scheduler
    s.settings.gateway_driver = 'ble'
    event(s, 'connected', MACS[0])
    s.states[MACS[0]].last_sample_at = None
    clock[0] += 3
    s._fill_connections()
    assert s.gateway.calls == []
