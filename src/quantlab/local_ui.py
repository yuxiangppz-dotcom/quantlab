"""Owned, local-only Streamlit process lifecycle for the Windows launcher."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from quantlab.daily.service import PROJECT_ROOT

RUNTIME_ROOT = PROJECT_ROOT / "data" / "runtime" / "ui"
APP_PATH = PROJECT_ROOT / "src" / "quantlab" / "ui" / "app.py"


def _start_ticks(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _owned(state: dict, port: int) -> bool:
    pid = state.get("pid")
    if type(pid) is not int or pid <= 0 or state.get("port") != port:
        return False
    if not state.get("start_ticks") or _start_ticks(pid) != state["start_ticks"]:
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    except (OSError, UnicodeError):
        return False
    return all(
        arg in args
        for arg in (
            "streamlit",
            "run",
            str(APP_PATH),
            "--server.address=127.0.0.1",
            f"--server.port={port}",
        )
    )


def _healthy(port: int) -> bool:
    try:
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/_stcore/health",
            timeout=1,
        ) as response:
            return response.status == 200 and response.read(16).strip() == b"ok"
    except (OSError, URLError):
        return False


def _occupied(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def manage_ui(action: str, port: int = 8501, *, runtime_root: Path = RUNTIME_ROOT) -> dict:
    if action not in {"start", "stop", "status"}:
        raise ValueError("unsupported UI action")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("port must be an integer in [1024, 65535]")
    runtime_root.mkdir(parents=True, exist_ok=True)
    state_path = runtime_root / f"{port}.json"
    with (runtime_root / f"{port}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (OSError, ValueError) as exc:
            raise ValueError("UI ownership record cannot be read; no process was stopped") from exc
        owned = isinstance(state, dict) and _owned(state, port)
        url = f"http://127.0.0.1:{port}"
        if action == "status":
            return {"status": "running" if owned and _healthy(port) else "stopped", "url": url}
        if action == "stop":
            if owned:
                os.kill(state["pid"], signal.SIGTERM)
                for _ in range(50):
                    if not _owned(state, port):
                        break
                    time.sleep(0.1)
                if _owned(state, port):
                    raise RuntimeError("QuantLab did not stop gracefully; no force kill was used")
                state_path.unlink(missing_ok=True)
            return {"status": "stopped", "url": url, "stopped_owned_process": owned}
        if owned and _healthy(port):
            return {"status": "running", "url": url, "reused": True}
        if owned:
            raise RuntimeError("QuantLab is starting or unhealthy; check its local UI log")
        if _occupied(port):
            raise RuntimeError(f"port {port} belongs to an unverified process; it was not stopped")
        command = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP_PATH),
            "--server.address=127.0.0.1",
            f"--server.port={port}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ]
        with (runtime_root / f"{port}.log").open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        state = {"pid": process.pid, "port": port, "start_ticks": _start_ticks(process.pid)}
        state_path.write_text(json.dumps(state))
        for _ in range(150):
            if process.poll() is not None:
                raise RuntimeError("QuantLab exited during startup; check its local UI log")
            if _healthy(port) and _owned(state, port):
                return {"status": "running", "url": url, "reused": False}
            time.sleep(0.1)
        raise RuntimeError(
            "QuantLab startup timed out; ownership record was retained for safe stop"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    try:
        print(json.dumps(manage_ui(args.action, args.port)))
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
