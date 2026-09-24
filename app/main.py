from __future__ import annotations

import asyncio
import csv
import io
import logging
import sqlite3
import os
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, project_root, static_root
from .database import Database
from .scheduler import Scheduler
from .diagnostics import VERSION, configure_logging, diagnostic_archive
from .protocol import normalize_mac


class DeviceCreate(BaseModel):
    mac: str
    name: str
    location: str = ""
    enabled: bool = True
    simulated: bool = False


class DeviceUpdate(BaseModel):
    mac: str | None = None
    name: str | None = None
    location: str | None = None
    enabled: bool | None = None
    thresholds: dict[str, float | None] | None = None


class SettingsUpdate(BaseModel):
    gateway_driver: Literal["simulator", "serial"] | None = None
    serial_port: str | None = Field(None, min_length=3, max_length=20)
    baudrate: int | None = Field(None, ge=1200, le=921600)
    connect_timeout_seconds: int | None = Field(None, ge=5, le=60)
    max_connections: int | None = Field(None, ge=1, le=7)
    serial_concurrency_limit: int | None = Field(None, ge=1, le=7)
    dwell_seconds: int | None = Field(None, ge=60, le=300)
    reconnect_base_seconds: int | None = Field(None, ge=5, le=600)
    focus_lease_seconds: int | None = Field(None, ge=10, le=300)
    persist_interval_seconds: int | None = Field(None, ge=1, le=60)
    web_refresh_hz: int | None = Field(None, ge=1, le=10)


class SocketHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def connect(self, socket: WebSocket) -> None:
        await socket.accept()
        self.clients.add(socket)

    def disconnect(self, socket: WebSocket) -> None:
        self.clients.discard(socket)

    async def publish(self, message: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for socket in list(self.clients):
            try:
                await socket.send_json(message)
            except Exception:
                stale.append(socket)
        for socket in stale:
            self.disconnect(socket)


configure_logging()
settings = Settings.load()
database = Database()
hub = SocketHub()
scheduler = Scheduler(database, settings, hub.publish)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="工业无线温振监测系统", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_root()), name="static")


@app.get("/")
def index():
    return FileResponse(static_root() / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "gateway": scheduler.snapshot()["gateway"]}


@app.get("/api/diagnostics")
def diagnostics():
    return {"version": VERSION, "log_path": str(project_root() / "logs" / "monitor.log"), "snapshot": scheduler.snapshot()}


@app.get("/api/diagnostics/download")
def download_diagnostics():
    return Response(
        diagnostic_archive(scheduler.snapshot(), settings.public_dict()),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="WTVB-diagnostics-v{VERSION}.zip"'},
    )


@app.post("/api/gateway/reconnect")
async def reconnect_gateway():
    logging.getLogger(__name__).info("User requested gateway reconnect")
    await scheduler.restart_gateway()
    return {"ok": True, "gateway": scheduler.snapshot()["gateway"]}


@app.get("/api/dashboard")
def dashboard():
    latest = database.latest_samples()
    snapshot = scheduler.snapshot()
    devices = []
    for device in database.list_devices():
        runtime = snapshot["devices"].get(device["mac"], {"status": "disabled", "is_focus": False})
        if not runtime.get("latest"):
            stored = latest.get(device["mac"])
            if stored and stored.get("source") == settings.gateway_driver:
                runtime["latest"] = stored
        devices.append({**device, "runtime": runtime})
    return {"settings": settings.public_dict(), "gateway": snapshot["gateway"],
            "focus_mac": snapshot["focus_mac"], "queue_order": snapshot["queue_order"],
            "devices": devices}


@app.post("/api/devices")
def add_device(payload: DeviceCreate):
    try:
        return database.add_device(payload.model_dump())
    except (ValueError, Exception) as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(409, "该MAC地址已经存在") from exc
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/devices/{device_id}")
async def update_device(device_id: int, payload: DeviceUpdate):
    device = database.get_device(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    values = payload.model_dump(exclude_none=True)
    try:
        if "mac" in values:
            values["mac"] = normalize_mac(values["mac"])
    except ValueError as exc:
        raise HTTPException(400, "MAC 地址格式无效，请输入 12 位十六进制地址") from exc
    changing_mac = "mac" in values and values["mac"] != device["mac"]
    state = scheduler.states.get(device["mac"])
    if changing_mac and state and state.status in {"connected", "connecting", "disconnecting"}:
        if scheduler.gateway.online and not getattr(scheduler.gateway, "_faulted", False):
            raise HTTPException(409, "设备正在连接或采集，请先停用设备，等待连接结束后再修改 MAC")
    try:
        result = database.update_device(device_id, values)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "该 MAC 地址已被其他设备使用") from exc
    if changing_mac:
        await scheduler.remove_device(device["mac"])
        scheduler._sync_devices()
        logging.getLogger(__name__).info("Device MAC updated id=%s old=%s new=%s", device_id, device["mac"], values["mac"])
    return result


@app.get("/api/devices/{device_id}/history")
def device_history(
    device_id: int,
    limit: int = Query(720, ge=1, le=20000),
    minutes: int | None = Query(None, ge=1, le=10080),
    start: str | None = None,
    end: str | None = None,
    max_points: int = Query(2000, ge=1, le=5000),
):
    if not database.get_device(device_id):
        raise HTTPException(404, "设备不存在")
    if start is not None or end is not None:
        start_at, end_at = _history_bounds(start, end)
        return database.history_range(device_id, start_at, end_at, max_points)
    if minutes is not None:
        end_at = datetime.now(timezone.utc)
        start_at = end_at - timedelta(minutes=minutes)
        return database.history_range(
            device_id, start_at.isoformat(), end_at.isoformat(), max_points
        )
    return database.history(device_id, limit)


def _history_bounds(start: str | None, end: str | None) -> tuple[str, str]:
    if not start or not end:
        raise HTTPException(400, "开始和结束时间必须同时提供")
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, "历史时间格式无效") from exc
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    start_at = start_at.astimezone(timezone.utc)
    end_at = end_at.astimezone(timezone.utc)
    if end_at <= start_at:
        raise HTTPException(400, "结束时间必须晚于开始时间")
    if end_at - start_at > timedelta(days=7):
        raise HTTPException(400, "单次历史查询不能超过 7 天")
    return start_at.isoformat(), end_at.isoformat()


@app.get("/api/devices/{device_id}/history/table")
def device_history_table(
    device_id: int,
    start: str,
    end: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    if not database.get_device(device_id):
        raise HTTPException(404, "设备不存在")
    start_at, end_at = _history_bounds(start, end)
    items, total = database.history_page(device_id, start_at, end_at, page, page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/api/devices/{device_id}/history/export")
def export_device_history(
    device_id: int,
    start: str,
    end: str,
):
    device = database.get_device(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    start_at, end_at = _history_bounds(start, end)
    columns = (
        "timestamp", "temperature", "velocity_x", "velocity_y", "velocity_z",
        "displacement_x", "displacement_y", "displacement_z", "frequency_x",
        "frequency_y", "frequency_z", "vibration_angle_x", "vibration_angle_y",
        "vibration_angle_z", "acceleration_x", "acceleration_y", "acceleration_z",
        "angular_velocity_x", "angular_velocity_y", "angular_velocity_z", "battery", "source",
    )

    def csv_rows():
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(columns)
        yield "\ufeff" + output.getvalue()
        output.seek(0)
        output.truncate(0)
        _, total = database.history_page(device_id, start_at, end_at, page=1, page_size=5000)
        pages = (total + 4999) // 5000
        for page in range(pages, 0, -1):
            rows, _ = database.history_page(device_id, start_at, end_at, page=page, page_size=5000)
            for row in rows:
                writer.writerow([row.get(column, "") for column in columns])
            if rows:
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

    filename = f"WTVB-{device['id']}-history.csv"
    return StreamingResponse(
        csv_rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/devices/{device_id}/disconnect")
async def disconnect_device(device_id: int):
    device = database.get_device(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    try:
        await scheduler.pause_device(device["mac"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/api/devices/{device_id}/reconnect")
async def reconnect_device(device_id: int):
    device = database.get_device(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    if not device["enabled"]:
        raise HTTPException(409, "设备已停用，请先在设备设置中启用")
    try:
        await scheduler.resume_device(device["mac"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: int):
    device = database.get_device(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    await scheduler.remove_device(device["mac"])
    database.delete_device(device_id)
    return {"ok": True}


@app.post("/api/devices/{device_id}/focus")
async def focus_device(device_id: int):
    device = database.get_device(device_id)
    if not device:
        raise HTTPException(404, "设备不存在")
    if not device["enabled"]:
        raise HTTPException(409, "设备已停用")
    if scheduler.states.get(device["mac"]) and scheduler.states[device["mac"]].manual_paused:
        raise HTTPException(409, "设备已手动断开，请先重新连接")
    scheduler.request_focus(device["mac"])
    await hub.publish({"type": "snapshot", "snapshot": scheduler.snapshot()})
    return {"ok": True, "focus_mac": device["mac"]}


@app.post("/api/focus/heartbeat")
def focus_heartbeat():
    scheduler.heartbeat_focus()
    return {"ok": True}


@app.delete("/api/focus")
async def clear_focus():
    scheduler.clear_focus()
    await hub.publish({"type": "snapshot", "snapshot": scheduler.snapshot()})
    return {"ok": True}


@app.patch("/api/settings")
async def update_settings(payload: SettingsUpdate):
    values = payload.model_dump(exclude_none=True)
    requires_gateway_restart = any(
        key in values and values[key] != getattr(settings, key)
        for key in ("gateway_driver", "serial_port", "baudrate", "connect_timeout_seconds")
    )
    try:
        settings.update(values)
        settings.save()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if requires_gateway_restart:
        await scheduler.restart_gateway()
    logging.getLogger(__name__).info("Settings updated: %s", values)
    return settings.public_dict()


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket):
    await hub.connect(socket)
    await socket.send_json({"type": "snapshot", "snapshot": scheduler.snapshot()})
    try:
        while True:
            message = await socket.receive_text()
            if message == "focus-heartbeat":
                scheduler.heartbeat_focus()
    except WebSocketDisconnect:
        hub.disconnect(socket)


def run() -> None:
    if os.environ.get("WTVB_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{settings.port}")).start()
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False, log_config=None)
