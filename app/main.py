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


@app.get("/