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
