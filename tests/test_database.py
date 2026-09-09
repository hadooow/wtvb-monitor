from datetime import datetime, timedelta, timezone

from app.database import Database
from app.protocol import SensorSample


def test_history_range_and_device_delete(tmp_path):
    database = Database(tmp_path / "monitor.db")
    device = database.get_device_by_mac("E8C5C0B8917E")
    assert device is not None

    old = SensorSample(
        mac=device["mac"],
        timestamp=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        temperature=20.0,
    )
    recent = SensorSample(
        mac=device["mac"],
        timestamp=datetime.now(timezone.utc).isoformat(),
        temperature=28.5,
    )
    database.save_sample(old)
    database.save_sample(recent)

    history = database.history_since(device["id"], minutes=1)
    assert [sample["temperature"] for sample in history] == [28.5]

    assert database.delete_device(device["id"]) is True
    assert database.get_device(device["id"]) is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples WHERE device_id=?", (device["id"],)).fetchone()[0] == 0


def test_cbf1_upgrade_preserves_existing_device_and_does_not_resurrect_deleted(tmp_path):
    path = tmp_path / 'monitor.db'
    database = Database(path)
    cbf1 = database.get_device_by_mac('C2:37:21:02:DE:EF')
    assert cbf1 is not None
    database.update_device(cbf1['id'], {'name': '现场自定义名称', 'enabled': False})
    # Simulate an old database that has CBF1 registered manually.
    with database.connect() as conn:
        conn.execute('DELETE FROM migrations')
    upgraded = Database(path)
    existing = upgraded.get_device_by_mac(cbf1['mac'])
    assert existing['name'] == '现场自定义名称'
    assert existing['enabled'] is False
    assert len(upgraded.list_devices()) == 8
    upgraded.delete_device(existing['id'])
    assert Database(path).get_device_by_mac(cbf1['mac']) is None


def test_cbf1_is_added_to_nonempty_old_database(tmp_path):
    path = tmp_path / 'monitor.db'
    database = Database(path)
    database.delete_device(database.get_device_by_mac('C2372102DEEF')['id'])
    with database.connect() as conn:
        conn.execute('DELETE FROM migrations')
    assert Database(path).get_device_by_mac('C2372102DEEF')['name'] == 'CBF1-W31'


def test_address_upgrade_preserves_id_history_and_user_settings(tmp_path):
    path = tmp_path / 'monitor.db'
    db = Database(path)
    device = db.get_device_by_mac('E8C5C0B8917E')
    db.save_sample(SensorSample(timestamp='2026-09-09T15:00:00+00:00', mac=device['mac'], temperature=28.5))
    db.update_device(device['id'], {'mac': 'F8C5C0B8917E', 'name': '自定义CBF0', 'thresholds': {'temperature_max': 80}})
    original = db.get_device_by_mac('FE6DF407B3E4')
    db.update_device(original['id'], {'name': '原传感器', 'enabled': False})
    with db.connect() as conn:
        conn.execute("DELETE FROM migrations WHERE name='v0.6.2-default-addresses'")
    upgraded = Database(path)
    corrected = upgraded.get_device_by_mac('E8C5C0B8917E')
    assert corrected['id'] == device['id']
    assert corrected['name'] == '自定义CBF0'
    assert corrected['thresholds']['temperature_max'] == 80
    assert upgraded.history(device['id'])[0]['temperature'] == 28.5
    assert upgraded.get_device_by_mac('F8C5C0B8917E') is None
    assert upgraded.get_device(original['id'])['enabled'] is False
    assert upgraded.get_device(original['id'])['name'] == '原传感器'
    upgraded.delete_device(original['id'])
    assert Database(path).get_device_by_mac('FE6DF407B3E4') is None


def test_address_upgrade_adds_original_sensor_and_keeps_conflicting_histories(tmp_path):
    path = tmp_path / 'monitor.db'
    db = Database(path)
    corrected = db.get_device_by_mac('E8C5C0B8917E')
    wrong = db.add_device({'mac': 'F8C5C0B8917E', 'name': '旧地址'})
    db.save_sample(SensorSample(timestamp='2026-09-09T15:00:00+00:00', mac=wrong['mac'], temperature=20))
    db.delete_device(db.get_device_by_mac('FE6DF407B3E4')['id'])
    with db.connect() as conn:
        conn.execute("DELETE FROM migrations WHERE name='v0.6.2-default-addresses'")
    upgraded = Database(path)
    assert upgraded.get_device_by_mac('FE6DF407B3E4')['name'] == 'WTVB01-BT50'
    assert upgraded.get_device(corrected['id'])['enabled'] is True
    assert upgraded.get_device(wrong['id'])['enabled'] is False
    assert upgraded.history(wrong['id'])[0]['temperature'] == 20
