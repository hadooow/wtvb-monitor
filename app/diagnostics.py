from __future__ import annotations

import io
import json
import logging
import platform
import sys
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import project_root

VERSION = "0.6.2"


def configure_logging(directory: Path | None = None) -> Path:
    directory = directory or project_root() / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    path = (directory / "monitor.log").resolve()
    if not any(isinstance(h, RotatingFileHandler) and h.baseFilename == str(path) for h in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s %(message)s"))
        root.addHandler(handler)
    logging.getLogger(__name__).info("WTVB-Monitor v%s started Python=%s OS=%s logs=%s", VERSION, sys.version.split()[0], platform.platform(), path)
    return path


def diagnostic_archive(snapshot: dict, settings: dict, directory: Path | None = None) -> bytes:
    """Export bounded logs and runtime configuration, never the database."""
    directory = directory or project_root() / "logs"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps({
            "version": VERSION, "platform": platform.platform(),
            "settings": settings, "snapshot": snapshot,
        }, ensure_ascii=False, indent=2))
        # Hold the logging lock while copying each handler's files to avoid a
        # rotation changing filenames partway through the snapshot.
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
        for handler in handlers:
            handler.acquire()
        try:
            for handler in handlers:
                handler.flush()
            for suffix in ("", ".1", ".2", ".3", ".4", ".5"):
                path = directory / f"monitor.log{suffix}"
                if path.is_file():
                    archive.write(path, f"logs/{path.name}")
        finally:
            for handler in reversed(handlers):
                handler.release()
    return output.getvalue()
