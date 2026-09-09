from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def normalize_mac(value: str) -> str:
    clean = "".join(char for char in value if char.isalnum()).upper()
    if len(clean) != 12 or any(char not in "0123456789ABCDEF" for char in clean):
        raise ValueError(f"invalid MAC address: {value}")
    return clean


def display_mac(value: str) -> str:
    clean = normalize_mac(value)
    return ":".join(clean[index : index + 2] for index in range(0, 12, 2)).lower()


def _i16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little", signed=True)


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little", signed=False)


@dataclass(slots=True)
class SensorSample:
    mac: str
    timestamp: str
    temperature: float | None = None
    velocity_x: float | None = None
    velocity_y: float | None = None
    velocity_z: float | None = None
    displacement_x: float | None = None
    displacement_y: float | None = None
    displacement_z: float | None = None
    frequency_x: float | None = None
    frequency_y: float | None = None
    frequency_z: float | None = None
    vibration_angle_x: float | None = None
    vibration_angle_y: float | None = None
    vibration_angle_z: float | None = None
    acceleration_x: float | None = None
    acceleration_y: float | None = None
    acceleration_z: float | None = None
    angular_velocity_x: float | None = None
    angular_velocity_y: float | None = None
    angular_velocity_z: float | None = None
    battery: float | None = None
    source: str = "gateway"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


ALARM_METRICS = {
    "temperature": ("温度", "°C"),
    "velocity": ("最大速度", "mm/s"),
    "displacement": ("最大位移", "μm"),
    "frequency": ("最大频率", "Hz"),
}


def evaluate_alarm(sample: SensorSample | dict[str, Any], thresholds: dict[str, Any] | None) -> dict[str, Any]:
    """Evaluate optional per-device warning/alarm limits against the current sample."""
    values = sample.as_dict() if isinstance(sample, SensorSample) else sample
    thresholds = thresholds or {}

    def axis_max(prefix: str) -> float | None:
        axes = [values.get(f"{prefix}_{axis}") for axis in "xyz"]
        finite = [abs(float(value)) for value in axes if value is not None]
        return max(finite) if finite else None

    measured = {
        "temperature": values.get("temperature"),
        "velocity": axis_max("velocity"),
        "displacement": axis_max("displacement"),
        "frequency": axis_max("frequency"),
    }
    level = "normal"
    reasons: list[dict[str, Any]] = []
    for metric, value in measured.items():
        if value is None:
            continue
        warn = thresholds.get(f"{metric}_warn")
        alarm = thresholds.get(f"{metric}_alarm")
        metric_level = None
        threshold = None
        if alarm is not None and float(value) >= float(alarm):
            metric_level, threshold, level = "alarm", float(alarm), "alarm"
        elif warn is not None and float(value) >= float(warn):
            metric_level, threshold = "warning", float(warn)
            if level == "normal":
                level = "warning"
        if metric_level:
            label, unit = ALARM_METRICS[metric]
            reasons.append({
                "metric": metric,
                "label": label,
                "unit": unit,
                "value": float(value),
                "threshold": threshold,
                "level": metric_level,
            })
    return {"level": level, "reasons": reasons}


def parse_wtvb01_frame(mac: str, frame: bytes, source: str = "gateway") -> SensorSample | None:
    """Parse the WTVB01-BT50 32-byte 0x55 0x61 notification frame."""
    if len(frame) < 32 or frame[0:2] != b"\x55\x61":
        return None
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return SensorSample(
        mac=normalize_mac(mac),
        timestamp=timestamp,
        velocity_x=float(_i16(frame, 2)),
        velocity_y=float(_i16(frame, 4)),
        velocity_z=float(_i16(frame, 6)),
        vibration_angle_x=round(_i16(frame, 8) / 32768.0 * 180.0, 4),
        vibration_angle_y=round(_i16(frame, 10) / 32768.0 * 180.0, 4),
        vibration_angle_z=round(_i16(frame, 12) / 32768.0 * 180.0, 4),
        temperature=round(_i16(frame, 14) / 100.0, 2),
        displacement_x=float(_i16(frame, 16)),
        displacement_y=float(_i16(frame, 18)),
        displacement_z=float(_i16(frame, 20)),
        frequency_x=float(_u16(frame, 22)),
        frequency_y=float(_u16(frame, 24)),
        frequency_z=float(_u16(frame, 26)),
        source=source,
    )


class WtvbStreamDecoder:
    """Reassembles WTVB packets when BLE notifications split or combine frames."""

    FRAME_LENGTHS = {0x61: 32, 0x71: 20}

    def __init__(self) -> None:
        self._buffers: dict[str, bytearray] = {}

    def forget(self, mac: str) -> None:
        self._buffers.pop(normalize_mac(mac), None)

    def feed(self, mac: str, payload: bytes, source: str = "gateway") -> list[SensorSample]:
        mac = normalize_mac(mac)
        buffer = self._buffers.setdefault(mac, bytearray())
        buffer.extend(payload)
        samples: list[SensorSample] = []
        while True:
            try:
                start = buffer.index(0x55)
            except ValueError:
                buffer.clear()
                break
            if start:
                del buffer[:start]
            if len(buffer) < 2:
                break
            frame_length = self.FRAME_LENGTHS.get(buffer[1])
            if frame_length is None:
                del buffer[0]
                continue
            if len(buffer) < frame_length:
                break
            frame = bytes(buffer[:frame_length])
            del buffer[:frame_length]
            if frame[1] == 0x61:
                sample = parse_wtvb01_frame(mac, frame, source=source)
                if sample:
                    samples.append(sample)
        return samples
