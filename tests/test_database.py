from datetime import datetime, timedelta, timezone

from app.database import Database
from app.protocol import SensorSample


def test_history_range_and_device_delete(tmp_path):
    database = Database(tmp_path / "monitor.db")
    device = database.get_device_by_mac("F8C5C0B8917E")
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
    assert len(upgraded.list_devices()) == 7
    upgraded.delete_device(existing['id'])
    assert Database(path).get_device_by_mac(cbf1['mac']) is None


def test_cbf1_is_added_to_nonempty_old_database(tmp_path):
    path = tmp_path / 'monitor.db'
    database = Database(path)
    database.delete_device(database.get_device_by_mac('C2372102DEEF')['id'])
    with database.connect() as conn:
        conn.execute('DELETE FROM migrations')
    assert Database(path).get_device_by_mac('C2372102DEEF')['name'] == 'CBF1-W31'
