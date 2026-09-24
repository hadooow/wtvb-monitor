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
                    [(mac, name, location, simulated, now) for mac, name, locat