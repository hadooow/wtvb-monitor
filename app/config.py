from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def static_root() -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "app" / "static"
    return Path(__file__).resolve().parent / "static"


@dataclass(slots=True)
class Settings:
    gateway_driver: str = "simulator"
    serial_port: str = "COM3"
    baudrate: int = 115200
    max_connections: int = 5
    dwell_seconds: int = 180
    connect_timeout_seconds: int = 40
    reconnect_base_seconds: int = 10
    focus_lease_seconds: int = 30
    persist_interval_seconds: int = 5
    web_refresh_hz: int = 5
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or project_root() / "config" / "settings.json"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            settings = cls()
            path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
            return settings
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key in self.__dataclass_fields__:
                setattr(self, key, value)
        self.validate()

    def validate(self) -> None:
        if self.gateway_driver not in {"simulator", "serial"}:
            raise ValueError("gateway_driver must be simulator or serial")
        if not 1 <= int(self.max_connections) <= 7:
            raise ValueError("max_connections must be between 1 and 7")
        if not 60 <= int(self.dwell_seconds) <= 300:
            raise ValueError("dwell_seconds must be between 60 and 300")
        if not 5 <= int(self.connect_timeout_seconds) <= 60:
            raise ValueError("connect_timeout_seconds must be between 5 and 60")
        if not 1 <= int(self.persist_interval_seconds) <= 60:
            raise ValueError("persist_interval_seconds must be between 1 and 60")
        if not 1 <= int(self.web_refresh_hz) <= 10:
            raise ValueError("web_refresh_hz must be between 1 and 10")

    def save(self, path: Path | None = None) -> None:
        self.validate()
        path = path or project_root() / "config" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)
