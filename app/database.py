from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import project_root
from .protocol import SensorSample, display_mac, normalize_mac


SAMPLE_COLUMNS = [
    "temperature", "velocity_x", "velocity_y", "velocity_z",
    "displacement_x", "displacement_y", "displacement_z",
    "frequency_x", "frequency_y", "frequency_z",
    "vibration_angle_x", "vibration_angle_y", "vibration_angle_z",
    "acceleration_x", "acceleration_y", "acceleration_z",
    "angular_velocity_x", "angular_velocity_y", "angular_velocity_z", "battery",
]


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or project_root() / "data" / "monitor.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    simulated INTEGER NOT NULL DEFAULT 0,
                    thresholds TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id),
                    timestamp TEXT NOT NULL,
                    temperature REAL,
                    velocity_x REAL, velocity_y REAL, velocity_z REAL,
                    displacement_x REAL, displacement_y REAL, displacement_z REAL,
                    frequency_x REAL, frequency_y REAL, frequency_z REAL,
                    vibration_angle_x REAL, vibration_angle_y REAL, vibration_angle_z REAL,
                    acceleration_x REAL, acceleration_y REAL, acceleration_z REAL,
                    angular_velocity_x REAL, angular_velocity_y REAL, angular_velocity_z REAL,
                    battery REAL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_samples_device_time
                    ON samples(device_id, timestamp DESC);
                CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY);
                """
            )
            count = connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            if count == 0:
                now = datetime.now(timezone.utc).isoformat()
                seed = [
                    ("E8C5C0B8917E", "CBF0-W31", "7-100", 0),
                    ("C2372102DEEF", "CBF1-W31", "7-100", 0),
                    ("E358B14B81B5", "CBF2-W31", "7-100", 0),
                    ("E91470052EF9", "CBF3-W31", "7-100", 0),
                    ("D5E44E23860B", "CBF4-W31", "7-100", 0),
                    ("F80D11C2A52E", "CBF5-W31", "7-100", 0),
                    ("F6D0D817D976", "CBF6-W31", "7-100", 0),
                    ("FE6DF407B3E4", "WTVB01-BT50", "", 0),
                ]
                connection.executemany(
                    "INSERT INTO devices(mac,name,location,simulated,created_at) VALUES(?,?,?,?,?)",
                    [(mac, name, location, simulated, now) for mac, name, location, simulated in seed],
                )
            # Upgrade existing databases once; preserve user names/settings,
            # and respect subsequent explicit deletion of CBF1.
            migration = "v0.6.0-add-cbf1"
            if not connection.execute("SELECT 1 FROM migrations WHERE name=?", (migration,)).fetchone():
                connection.execute(
                    "INSERT OR IGNORE INTO devices(mac,name,location,simulated,created_at) VALUES(?,?,?,?,?)",
                    ("C2372102DEEF", "CBF1-W31", "7-100", 0, datetime.now(timezone.utc).isoformat()),
                )
                connection.execute("INSERT INTO migrations(name) VALUES(?)", (migration,))

            migration = "v0.6.2-default-addresses"
            if not connection.execute("SELECT 1 FROM migrations WHERE name=?", (migration,)).fetchone():
                connection.execute(
                    "INSERT OR IGNORE INTO devices(mac,name,location,simulated,created_at) VALUES(?,?,?,?,?)",
                    ("FE6DF407B3E4", "WTVB01-BT50", "", 0, datetime.now(timezone.utc).isoformat()),
                )
                if connection.execute("SELECT 1 FROM devices WHERE mac='E8C5C0B8917E'").fetchone():
                    # Retain both histories if the user already registered the
                    # corrected MAC; disable only the confirmed incorrect one.
                    connection.execute("UPDATE devices SET enabled=0 WHERE mac='F8C5C0B8917E'")
                else:
                    connection.execute("UPDATE devices SET mac='E8C5C0B8917E' WHERE mac='F8C5C0B8917E'")
                connection.execute("INSERT INTO migrations(name) VALUES(?)", (migration,))

    def list_devices(self, include_simulated: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM devices"
        params: tuple[Any, ...] = ()
        if not include_simulated:
            query += " WHERE simulated=0"
        query += " ORDER BY id"
        with self._lock, self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._device_dict(row) for row in rows]

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        return self._device_dict(row) if row else None

    def get_device_by_mac(self, mac: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as connection:
            row = connection.execute("SELECT * FROM devices WHERE mac=?", (normalize_mac(mac),)).fetchone()
        return self._device_dict(row) if row else None

    def add_device(self, values: dict[str, Any]) -> dict[str, Any]:
        mac = normalize_mac(values["mac"])
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO devices(mac,name,location,enabled,simulated,thresholds,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    mac,
                    values.get("name") or display_mac(mac),
                    values.get("location", ""),
                    int(values.get("enabled", True)),
                    int(values.get("simulated", False)),
                    json.dumps(values.get("thresholds", {}), ensure_ascii=False),
                    now,
                ),
            )
            device_id = cursor.lastrowid
        return self.get_device(int(device_id))

    def update_device(self, device_id: int, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"mac", "name", "location", "enabled", "thresholds"}
        assignments: list[str] = []
        parameters: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            if key == "mac":
                value = normalize_mac(value)
            if key == "thresholds":
                value = json.dumps(value or {}, ensure_ascii=False)
            if key == "enabled":
                value = int(bool(value))
            assignments.append(f"{key}=?")
            parameters.append(value)
        if assignments:
            parameters.append(device_id)
            with self._lock, self.connect() as connection:
                connection.execute(f"UPDATE devices SET {','.join(assignments)} WHERE id=?", parameters)
        return self.get_device(device_id)

    def delete_device(self, device_id: int) -> bool:
        """Delete a registered device and its measurements as one transaction."""
        with self._lock, self.connect() as connection:
            exists = connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone()
            if not exists:
                return False
            connection.execute("DELETE FROM samples WHERE device_id=?", (device_id,))
            connection.execute("DELETE FROM devices WHERE id=?", (device_id,))
        return True

    def save_sample(self, sample: SensorSample) -> None:
        device = self.get_device_by_mac(sample.mac)
        if not device:
            return
        values = sample.as_dict()
        columns = ["device_id", "timestamp", *SAMPLE_COLUMNS, "source"]
        row = [device["id"], values["timestamp"], *[values[name] for name in SAMPLE_COLUMNS], values["source"]]
        placeholders = ",".join("?" for _ in columns)
        with self._lock, self.connect() as connection:
            connection.execute(
                f"INSERT INTO samples({','.join(columns)}) VALUES({placeholders})",
                row,
            )

    def latest_samples(self) -> dict[str, dict[str, Any]]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.mac, s.* FROM devices d
                JOIN samples s ON s.id=(SELECT id FROM samples WHERE device_id=d.id ORDER BY timestamp DESC LIMIT 1)
                """
            ).fetchall()
        return {row["mac"]: {key: row[key] for key in row.keys() if key != "mac"} for row in rows}

    def history(self, device_id: int, limit: int = 720) -> list[dict[str, Any]]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM samples WHERE device_id=? ORDER BY timestamp DESC LIMIT ?",
                (device_id, min(max(limit, 1), 5000)),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def history_since(self, device_id: int, minutes: int, limit: int = 20000) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM samples WHERE device_id=? AND timestamp>=? ORDER BY timestamp DESC LIMIT ?",
                (device_id, cutoff, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _device_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["simulated"] = bool(result["simulated"])
        result["thresholds"] = json.loads(result["thresholds"] or "{}")
        result["mac_display"] = display_mac(result["mac"])
        return result
