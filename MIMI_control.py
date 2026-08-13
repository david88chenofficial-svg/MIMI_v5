"""Local runtime state and authenticated control for the MIMI web server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
CONTROL_DIRECTORY = Path(os.environ.get("LOCALAPPDATA", ROOT)) / "MIMI"
SERVER_STATE_PATH = CONTROL_DIRECTORY / "server_state.json"


def write_server_state(*, pid: int, port: int, token: str) -> None:
    CONTROL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    state = {
        "pid": pid,
        "port": port,
        "token": token,
        "started_at": time.time(),
    }
    temporary_path = SERVER_STATE_PATH.with_suffix(f".{pid}.tmp")
    temporary_path.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_path, SERVER_STATE_PATH)


def read_server_state() -> dict | None:
    try:
        state = json.loads(SERVER_STATE_PATH.read_text(encoding="utf-8"))
        pid = int(state["pid"])
        port = int(state["port"])
        token = str(state["token"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if pid <= 0 or port not in range(8000, 8100) or not token:
        return None
    return {
        "pid": pid,
        "port": port,
        "token": token,
        "started_at": state.get("started_at"),
    }


def clear_server_state(pid: int) -> None:
    state = read_server_state()
    if not state or state["pid"] != pid:
        return
    try:
        SERVER_STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def get_server_status(timeout: float = 0.25) -> dict | None:
    state = read_server_state()
    if not state:
        return None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{state['port']}/status",
            timeout=timeout,
        ) as response:
            web_state = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return {
        **state,
        "web": web_state,
    }


def stop_server(timeout: float = 2.0) -> bool:
    state = get_server_status()
    if not state:
        return False
    request = urllib.request.Request(
        f"http://127.0.0.1:{state['port']}/shutdown",
        method="POST",
        headers={"X-MIMI-Control-Token": state["token"]},
        data=b"",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 202
    except (OSError, urllib.error.URLError):
        return False

