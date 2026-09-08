from __future__ import annotations

import asyncio
import os
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, project_root, static_root
from .database import Database
from .scheduler import Scheduler


class DeviceCreate(BaseModel):
    mac: str
    name: str
    location: str = ""
    enabled: bool = True
    simulated: bool = False


class DeviceUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    enabled: bool | None = None
    thresholds: dict[str, float | None] | None = None


class SettingsUpdate(BaseModel):
    gateway_driver: Literal["simulator", "serial"] | None = None
    serial_port: str | None = Field(None, min_length=3, max_length=20)
    max_connections: int | None = Field(None, ge=1, le=7)
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


settings = Settings.load()
database = Database()
hub = SocketHub()
scheduler = Scheduler(database, settings, hub.publish)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="工业无线温振监测系统", version="0.5.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_root()), name="static")


@app.get("/")
def index():
    return FileResponse(static_root() / "index.html")


@app.get("/health")
def health():
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
    return {"settings": settings.public_dict(), "gateway": snapshot["gateway"], "focus_mac": snapshot["focus_mac"], "devices": devices}


@app.post("/api/devices")
def add_device(payload: DeviceCreate):
    try:
        return database.add_device(payload.model_dump())
    except (ValueError, Exception) as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(409, "该MAC地址已经存在") from exc
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/devices/{device_id}")
def update_device(device_id: int, payload: DeviceUpdate):
    result = database.update_device(device_id, payload.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(404, "设备不存在")
    return result


@app.get("/api/devices/{device_id}/history")
def device_history(
    device_id: int,
    limit: int = Query(720, ge=1, le=20000),
    minutes: int | None = Query(None, ge=1, le=10080),
):
    if not database.get_device(device_id):
        raise HTTPException(404, "设备不存在")
    if minutes is not None:
        return database.history_since(device_id, minutes, limit)
    return database.history(device_id, limit)


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
        for key in ("gateway_driver", "serial_port")
    )
    try:
        settings.update(values)
        settings.save()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if requires_gateway_restart:
        await scheduler.restart_gateway()
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
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
