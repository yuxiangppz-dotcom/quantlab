"""One bounded fixed-engine prediction per target day; independent of the phone UI."""

import argparse
import os
import signal
import subprocess
import threading
from datetime import datetime, time
from pathlib import Path

from quantlab.scout.cloud_artifacts import guarded, initialize, publish, status
from quantlab.scout.cloud_data import Fetcher, refresh_calendar, refresh_market
from quantlab.scout.cloud_push import notify, notify_failure, public_origin
from quantlab.scout.daily_budget import POLICY
from quantlab.scout.daily_runtime import atomic, claim, exclusive, prediction_settings, read, update
from quantlab.scout.models import SHANGHAI


def run_worker(engine, state_path, app):
    with (state_path.parent / "worker.log").open("xb") as log:
        process = subprocess.Popen(
            [
                str(Path(engine["release_root"]) / ".venv/bin/python"),
                str(Path(app) / "scripts/scout_cloud_worker.py"),
                "--settings",
                str(Path(engine["release_root"]) / "daily-settings.json"),
                "--state",
                str(state_path),
                "--application-commit",
                read(Path(app) / "daily-manifest.json")["commit"],
            ],
            cwd=engine["release_root"],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            process.wait(timeout=POLICY["run_timeout_seconds"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            update(
                state_path,
                "达到固定超时；送达未知不补发",
                status="failed",
                delivery_status="unknown",
            )
        if read(state_path)["status"] not in {"completed", "failed"}:
            update(
                state_path, "工作进程中断；不自动补发", status="failed", delivery_status="unknown"
            )


def tick(
    settings,
    now=None,
    *,
    enabled=False,
    fetcher_factory=Fetcher,
    execute=run_worker,
    clock=None,
    cancelled=lambda: False,
):
    root = guarded(Path(settings["canonical_dir"]).parent)
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if not enabled:
        return {"status": "schedule_disabled"}
    engine = prediction_settings(settings)
    origin = public_origin(os.environ["SCOUT_PUBLIC_URL"])
    # Do not invent late predictions. Startup catch-up is confined to this same window.
    if not time(8) <= now.time() < time(9):
        return {"status": "outside_morning_window"}
    with exclusive(root / "schedule.lock", blocking=False):
        schedule = root / "schedule" / (now.date().isoformat() + ".json")
        if schedule.exists():
            stored = read(schedule)
            if stored["status"] == "published":
                # Adding a missing push key later may deliver the already saved report.
                stored["notification"] = notify(root, stored["report"], origin)
                atomic(schedule, stored)
            elif stored["status"] == "failed":
                stored["notification"] = notify_failure(root, now.date().isoformat(), origin)
                atomic(schedule, stored)
            return stored
        result = {
            "status": "checking_calendar",
            "target_session": now.date().isoformat(),
            "started_at": now.isoformat(),
        }
        atomic(schedule, result)
        state_path = None
        try:
            fetcher = fetcher_factory(root, now)
            storage = refresh_calendar(root, fetcher, now)
            if not any(
                r.exchange == "SSE" and r.trade_date == now.date() and r.is_open
                for r in storage.load_trading_calendar()
            ):
                result.update(status="non_trading_day", local_date=now.date().isoformat())
                atomic(schedule, result)
                status(root, **result)
                return result
            result["status"] = "preparing_data"
            atomic(schedule, result)
            config = read(Path(engine["release_root"]) / "config/scout_daily.fixed.json")
            result["data"] = refresh_market(
                root,
                fetcher,
                now,
                history_sessions=120 if config.get("next_session_selection") else 21,
            )
            if cancelled():
                raise ValueError("Schedule stopped before claiming a paid model job")
            current = clock() if clock else datetime.now(SHANGHAI)
            if current.date() != now.date() or not time(8) <= current.time() < time(9):
                raise ValueError("Data preparation missed morning prediction window")
            state_path, state, created = claim(engine)
            if state["timing"]["target_session"] != now.date().isoformat():
                raise ValueError("Target changed before the worker started")
            result.update(status="running", state_key=state["key"])
            atomic(schedule, result)
            status(root, **result)
            if created:
                execute(engine, state_path, settings["release_root"])
            state = read(state_path)
            if state["status"] != "completed":
                raise ValueError("Prediction failed or delivery unknown; no automatic retry")
            result["report"] = publish(root, Path(state["report_json"]).parent)
            result["status"] = "published"
            atomic(schedule, result)
            result["notification"] = notify(root, result["report"], origin)
        except Exception as exc:
            result.update(
                status="failed",
                error_type=type(exc).__name__,
                note="保留失败/未知，不自动重跑预测；修复后需明确重新授权",
            )
            if state_path and read(state_path)["status"] == "claimed":
                update(state_path, "启动前检查失败，未调用模型", status="failed")
            result["notification"] = notify_failure(root, now.date().isoformat(), origin)
        atomic(schedule, result)
        status(root, **result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("/app/daily-settings.json"))
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--publish-run", type=Path, help="Publish an intact completed run; no model"
    )
    parser.add_argument(
        "--notify-run", type=Path, help="Publish and send one saved report notification"
    )
    args = parser.parse_args()
    settings = read(args.settings)
    root = Path(settings["canonical_dir"]).parent
    initialize(root)
    if args.publish_run or args.notify_run:
        metadata = publish(root, args.publish_run or args.notify_run)
        if args.notify_run:
            notify(root, metadata, os.environ["SCOUT_PUBLIC_URL"])
        return
    wake = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: wake.set())
    signal.signal(signal.SIGINT, lambda *_: wake.set())
    while not wake.is_set():
        try:
            tick(
                settings,
                enabled=os.environ.get("SCOUT_SCHEDULE_ENABLED") == "true",
                cancelled=wake.is_set,
            )
        except BlockingIOError:
            pass  # Another cloud scheduler owns the persistent lock.
        except Exception as exc:
            status(root, status="precheck_failed", error_type=type(exc).__name__)
        if not args.loop:
            break
        wake.wait(60)


if __name__ == "__main__":
    main()
