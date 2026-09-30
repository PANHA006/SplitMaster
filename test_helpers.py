"""Shared helpers for the Splitify integration test scripts."""

import os
import sys
import threading
import time

from ffmpeg_tools import find_binary


def get_ffmpeg_bin() -> str:
    return find_binary("ffmpeg") or "ffmpeg"


def get_ffprobe_bin() -> str:
    return find_binary("ffprobe") or "ffprobe"


def require_ffmpeg_binaries():
    """Fail fast with a clear message when FFmpeg is not available."""
    missing = [name for name, path in (("ffmpeg", find_binary("ffmpeg")), ("ffprobe", find_binary("ffprobe"))) if not path]
    if missing:
        raise RuntimeError(
            f"Missing binaries: {', '.join(missing)}. "
            "Install FFmpeg (winget install Gyan.FFmpeg), set FFMPEG_BIN/FFPROBE_BIN, "
            "or drop the .exe files into a 'bin/' folder next to main.py."
        )


def start_test_server(port: int):
    """Start the FastAPI app on a background thread for integration testing."""
    import uvicorn
    from main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.2)
    return server
