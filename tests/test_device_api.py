from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from app import main
from app.config import Settings
from app.database import Database
from app.protocol import SensorSample
from app.scheduler import Scheduler


@pytest.fixture
def api(tmp_path, monkeypatch):
    db = Database(tmp_path / 'monitor.db')
    async def publish(_):
        pass
    scheduler = Scheduler(db, Settings(), publish)
    scheduler._sync_devices()
    monkeypatch.setattr(main, 'database', db)
    monkeypatch.setattr(main, 'scheduler', scheduler)
    return TestClient(main.app), db, scheduler


def test_mac_edit_preserves_history_and_replaces_runtime(api):
    client, db, scheduler = api
    device = db.get_device_by_mac('E8C5C0B8917E')
    db.save_sample(SensorSample(timestamp='2026-09-09T15:00:00+00:00', mac=device['mac'], temperature=31))
    scheduler.focus_mac = device['mac']
    scheduler.decoder.feed(device['mac'], b'\x55\x61')
    response = client.patch(f"/api/devices/{device['id']}", json={'mac': 'aa:bb:cc:dd:ee:01', 'thresholds': {'temperature_max': 60}})
    assert response.status_code == 200
    assert response.json()['mac'] == 'AABBCCDDEE01'
    assert response.json()['id'] == device['id']
    assert db.history(device['id'])[0]['temperature'] == 31
    assert db.get_device(device['id'])['thresholds']['temperature_max'] == 60
    assert device['mac'] not in scheduler.states
    assert device['mac'] not in scheduler.decoder._buffers
    assert 'AABBCCDDEE01' in scheduler.states
    assert scheduler.focus_mac is None


@pytest.mark.parametrize('mac,status', [('invalid', 400), ('FE:6D:F4:07:B3:E4', 409)])
def test_invalid_or_duplicate_mac_changes_nothing(api, mac, status):
    client, db, _ = api
    device = db.get_device_by_mac('E8C5C0B8917E')
    response = client.patch(f"/api/devices/{device['id']}", json={'mac': mac, 'name': '不应保存'})
    assert response.status_code == status
    assert db.get_device(device['id']) == device


def test_active_device_must_be_disabled_before_mac_change(api):
    client, db, scheduler = api
    device = db.get_device_by_mac('E8C5C0B8917E')
    scheduler.gateway.start()
    scheduler.states[device['mac']].status = 'connected'
    url = f"/api/devices/{device['id']}"
    assert client.patch(url, json={'mac': 'AA:BB:CC:DD:EE:01'}).status_code == 409
    assert client.patch(url, json={'mac': device['mac'], 'enabled': False}).status_code == 200
    scheduler.states[device['mac']].status = 'waiting'
    scheduler._sync_devices()
    assert client.patch(url, json={'mac': 'AA:BB:CC:DD:EE:01'}).status_code == 200


def test_manual_disconnect_stays_paused_until_reconnect(api):
    client, db, scheduler = api
    device = db.get_device_by_mac('E8C5C0B8917E')
    url = f"/api/devices/{device['id']}"
    assert client.post(f'{url}/disconnect').status_code == 200
    state = scheduler.states[device['mac']]
    assert state.manual_paused and state.status == 'paused'
    scheduler._fill_connections()
    assert state.status == 'paused'
    assert db.get_device(device['id'])['enabled']
    assert client.post(f'{url}/reconnect').status_code == 200
    assert not state.manual_paused and state.status == 'queued'


def test_dashboard_exposes_upcoming_queue_order(api):
    client, db, scheduler = api
    dashboard = client.get('/api/dashboard').json()
    assert dashboard['queue_order'] == scheduler.queue_order()
    assert set(dashboard['queue_order']) == {
        device['mac'] for device in dashboard['devices'] if device['enabled']
    }


def test_history_chart_limit_table_and_csv_export(api):
    client, db, _ = api
    device = db.get_device_by_mac('E8C5C0B8917E')
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    for n in range(12):
        db.save_sample(SensorSample(
            timestamp=(start + timedelta(minutes=n)).isoformat(),
            mac=device['mac'], temperature=float(n),
        ))
    query = {'start': start.isoformat(), 'end': (start + timedelta(hours=1)).isoformat()}
    path = f"/api/devices/{device['id']}/history"
    chart = client.get(path, params={**query, 'max_points': 4})
    assert chart.status_code == 200
    assert len(chart.json()) <= 5
    assert chart.json()[0]['temperature'] == 0
    assert chart.json()[-1]['temperature'] == 11
    page = client.get(f'{path}/table', params={**query, 'page_size': 5})
    assert page.status_code == 200
    assert page.json()['total'] == 12
    assert [row['temperature'] for row in page.json()['items']] == [7, 8, 9, 10, 11]
    export = client.get(f'{path}/export', params=query)
    assert export.status_code == 200
    lines = export.content.decode('utf-8-sig').splitlines()
    assert len(lines) == 13
    assert lines[1].split(',')[1] == '0.0'
    assert lines[-1].split(',')[1] == '11.0'
