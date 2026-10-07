"""Immutable daily release, persistent idempotency and a local progress/report portal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import threading
from contextlib import contextmanager
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quantlab.data.storage import ParquetStorage
from quantlab.scout.daily_budget import POLICY
from quantlab.scout.market import inspect_market_data
from quantlab.scout.models import SHANGHAI, fingerprint
from quantlab.scout.pipeline import read_config, report_timing, run_scout


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def exclusive(path, *, blocking=True):
    """Linux advisory process lock; no stale lock-file deletion on recovery."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def verify_release(settings):
    from importlib.metadata import version

    root = Path(settings["release_root"])
    manifest = read(root / "daily-manifest.json")
    for relative, expected in manifest["files"].items():
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("fixed_release_changed:" + relative)
    if fingerprint(settings) != manifest["settings_sha256"]:
        raise ValueError("fixed_runtime_settings_changed")
    for name, expected in manifest.get("dependencies", {}).items():
        if version(name) != expected:
            raise ValueError("fixed_dependency_changed:" + name)
    return manifest


def prediction_settings(settings):
    """A presentation release may retain an already accepted immutable engine."""
    verify_release(settings)
    if not settings.get("prediction_release_root"):
        return settings
    root = Path(settings["prediction_release_root"])
    digest = hashlib.sha256((root / "daily-manifest.json").read_bytes()).hexdigest()
    if digest != settings["prediction_manifest_sha256"]:
        raise ValueError("fixed_prediction_manifest_changed")
    engine = read(root / "daily-settings.json")
    if engine.get("prediction_release_root"):
        raise ValueError("nested_prediction_release_not_supported")
    for key in ("canonical_dir", "output_root", "state_root"):
        if engine[key] != settings[key]:
            raise ValueError("prediction_workspace_mismatch:" + key)
    verify_release(engine)
    return engine


def display_report(state):
    """Derive the user view from the intact archived report, never regenerate a model."""
    from quantlab.scout.html_report import render_html_report

    source = Path(state.get("report_json") or Path(state["report_path"]).with_name("report.json"))
    report = read(source)
    manifest = read(source.parent / "manifest.json")
    if fingerprint(report) != manifest["report_sha256"]:
        raise ValueError("Original report integrity check failed")
    return render_html_report(report).encode()


def precheck(settings, now=None):
    now = now or datetime.now(SHANGHAI)
    local = now.astimezone(SHANGHAI)
    manifest = verify_release(settings)
    config_path = Path(settings["release_root"]) / "config/scout_daily.fixed.json"
    nextday = bool(config_path.is_file() and read_config(config_path)["next_session_selection"])
    storage = ParquetStorage(Path(settings["canonical_dir"]))
    info = inspect_market_data(storage, now)
    open_days = [
        r.trade_date for r in storage.load_trading_calendar() if r.exchange == "SSE" and r.is_open
    ]
    is_open = local.date() in open_days
    data_ready = bool(info["live_partition_files_present"])
    if not data_ready and (not nextday or is_open):
        raise ValueError("行情数据未准备好；请更新孤立行情副本后再次打开入口")
    if not info.get("expected_session"):
        raise ValueError("交易日历缺少已完成行情日")
    asof = date.fromisoformat(info["expected_session"])
    timing = report_timing(open_days, now, asof, data_ready, nextday=nextday)
    if not timing["target_session"]:
        raise ValueError("交易日历缺少下一目标交易日")
    key = (
        fingerprint(
            {
                "runtime_contract": "one_paid_target_session_v1",
                "target_session": timing["target_session"],
            }
        )
        if nextday
        else fingerprint(
            {
                "release": manifest["commit"],
                "config": manifest["config_sha256"],
                "asof": asof.isoformat(),
                "target": timing["target_session"],
                "local_date": now.astimezone(SHANGHAI).date().isoformat(),
            }
        )
    )
    return key, {
        "market": info,
        "timing": timing,
        "holiday": not is_open,
        "version": manifest["commit"],
        "budget": dict(POLICY),
        "next_session_selection": nextday,
        "paid_allowed": bool(nextday and is_open and timing["primary_eligible"]),
        "data_ready": data_ready,
    }


def _target_job(root, target):
    """A release upgrade never replaces an existing paid target-day claim.

    Existing legacy claims are conservatively respected during migration. Choose
    the earliest durable claim by creation time, never by its eventual outcome.
    Malformed ledger entries stop lookup rather than permit a fresh paid task.
    """
    matches = []
    for path in (root / "jobs").glob("*/state.json"):
        value = read(path)
        if value.get("timing", {}).get("target_session") == target:
            matches.append((value.get("created_at", ""), str(path), path, value))
    if not matches:
        return None
    _, _, path, value = min(matches, key=lambda item: item[:2])
    return path, value


def _idle(details):
    if not details.get("next_session_selection"):
        return {**details, "status": "ready", "message": "检查通过，等待生成"}
    holiday = details["holiday"]
    allowed = details["paid_allowed"]
    return {
        **details,
        "status": "ready"
        if allowed
        else "non_trading_day"
        if holiday
        else "outside_publication_window",
        "message": "检查通过，等待生成"
        if allowed
        else "今天为非交易日，仅查看下一目标日的已有结果，不生成付费预测"
        if holiday
        else "当前不在固定盘前发布窗口，仅查看已有结果，不生成付费预测",
    }


def claim(settings, now=None):
    """Same date/release/session never produces a second paid job, even after a crash."""
    root = Path(settings["state_root"])
    with exclusive(root / "registry.lock"):
        key, details = precheck(settings) if now is None else precheck(settings, now)
        if details.get("next_session_selection"):
            existing = _target_job(root, details["timing"]["target_session"])
            if existing:
                path, saved = existing
                return (
                    path,
                    {
                        **saved,
                        "holiday": details["holiday"],
                        "paid_allowed": False,
                        "current_market": details["market"],
                    },
                    False,
                )
            if not details["paid_allowed"]:
                # View-only state does not consume the future trading day's claim.
                path = root / "views" / key / "state.json"
                value = {**_idle(details), "key": key, "events": [], "report_path": None}
                atomic(path, value)
                return path, value, False
        path = root / "jobs" / key / "state.json"
        if path.exists():
            return path, read(path), False
        current = root / "active.json"
        if current.exists():
            active = Path(read(current)["state_path"])
            if active.exists() and read(active)["status"] in {"claimed", "running"}:
                return active, read(active), False
        state = {
            **details,
            "key": key,
            "status": "claimed",
            "message": "任务已登记，准备运行",
            "created_at": datetime.now(SHANGHAI).isoformat(),
            "events": [],
            "report_path": None,
        }
        atomic(path, state)
        atomic(current, {"state_path": str(path)})
        return path, state, True


def update(path, message, **changes):
    value = read(path)
    value.update(changes)
    value["message"] = message
    value["updated_at"] = datetime.now(SHANGHAI).isoformat()
    value["events"].append({"at": value["updated_at"], "message": message})
    atomic(path, value)


def worker(settings, state_path, *, application_commit=None):
    # Both immutable settings and source are checked again in the paid worker.
    manifest = verify_release(settings)
    update(state_path, "检查固定版本与数据新鲜度", status="running", pid=os.getpid())
    try:
        current_key, details = precheck(settings)
        if details.get("next_session_selection") and (
            not details["paid_allowed"] or current_key != read(state_path)["key"]
        ):
            raise ValueError("Paid target/session or fixed publication window changed")
        run_dir, report = run_scout(
            Path(settings["canonical_dir"]),
            Path(settings["output_root"]),
            read_config(Path(settings["release_root"]) / "config/scout_daily.fixed.json"),
            online=True,
            daily_journal=state_path.parent / "requests",
            progress=lambda message: update(state_path, message),
            release_identity={
                "engine_commit": manifest["commit"],
                "application_commit": application_commit or manifest["commit"],
                "config_sha256": manifest["config_sha256"],
                "delivery_channel": "cloud" if application_commit else "local",
            },
        )
        verify_release(settings)
        okay = (
            report["status"] == "live_research_unvalidated"
            and report.get("opportunity", {}).get("validation", {}).get("status") == "complete"
        )
        update(
            state_path,
            "报告已保存；预测效果待前瞻观察"
            if okay
            else "本次未通过完整校验，已保存失败记录，不输出正式推荐",
            status="completed" if okay else "failed",
            report_path=str(run_dir / "report.html"),
            report_json=str(run_dir / "report.json"),
            daily_budget=report.get("daily_delivery"),
            completed_at=datetime.now(SHANGHAI).isoformat(),
        )
    except Exception as exc:
        # Never echo provider exceptions that might contain credentials/HTTP payloads.
        update(
            state_path,
            "运行失败；保留记录，本日重复点击不会重新付费",
            status="failed",
            error_type=type(exc).__name__,
            completed_at=datetime.now(SHANGHAI).isoformat(),
        )


def supervise(settings, state_path):
    log = state_path.parent / "worker.log"
    with log.open("xb") as stream:
        process = subprocess.Popen(
            [
                str(Path(settings["release_root"]) / ".venv/bin/python"),
                "-m",
                "quantlab.scout.daily_runtime",
                "--worker",
                str(state_path),
                "--settings",
                str(Path(settings["release_root"]) / "daily-settings.json"),
            ],
            cwd=settings["release_root"],
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
        try:
            process.wait(timeout=POLICY["run_timeout_seconds"])
        except subprocess.TimeoutExpired:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            update(
                state_path,
                "达到固定总超时；交付状态未知的请求保留，不自动重试",
                status="failed",
                error_type="run_timeout",
                delivery_status="unknown",
            )
        else:
            state = read(state_path)
            if state["status"] not in {"completed", "failed"}:
                update(
                    state_path,
                    "工作进程意外退出；保留记录，不自动重试",
                    status="failed",
                    error_type="worker_exit",
                    delivery_status="unknown",
                )


PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Scout 日常预测</title>
<style>body{font:16px/1.7 system-ui;background:#f3f5fa;color:#16243a;margin:0}
main{max-width:860px;margin:7vh auto;padding:32px;background:white;border-radius:18px}
[hidden]{display:none!important}button:disabled{background:#8595ab;cursor:default}
h1{margin-top:0}button,a{padding:12px 18px;border-radius:9px}button{background:#244e86;color:white;
border:0;font-size:17px;cursor:pointer}a{display:inline-block}pre{white-space:pre-wrap;
background:#f5f7fb;padding:18px;border-radius:10px}.muted{color:#637289}</style>
<main><h1>Scout 日常预测</h1><p id="timing">正在检查交易日与行情…</p>
<button id="run" onclick="start()">生成本日预测</button><a id="report" hidden>打开报告</a>
<h3 id="message">固定版本 · 有界运行</h3><pre id="events"></pre>
<p class="muted">报告是研究观察，收益与选股效果待前瞻跟踪。重复点击复用本日任务；
失败记录也保留，本入口不会无限纠错或重新付费。</p></main>
<script>const token='TOKEN';let redirected=false;
async function start(){document.querySelector('#run').disabled=true;
 await fetch('/run',{method:'POST',headers:{'X-Scout-Token':token}});await poll();}
async function poll(){const r=await fetch('/state');const s=await r.json();
 document.querySelector('#message').textContent=s.message||'可以开始';
 if(s.timing){document.querySelector('#timing').textContent=
 (s.holiday?'今天为非交易日。':'')+'行情截至 '+s.timing.asof_session+
 '；下一目标交易日 '+s.timing.target_session+'。固定版本 '+s.version.slice(0,8);}
 document.querySelector('#events').textContent=(s.events||[]).map(e=>e.message).join('\\n');
 document.querySelector('#run').disabled=['claimed','running','completed','failed',
 'non_trading_day','outside_publication_window'].includes(s.status);
 if(s.report_path){const a=document.querySelector('#report');a.hidden=false;a.href='/report';
 if(!redirected){redirected=true;location.assign('/report');}}
 if(s.status==='precheck_failed'){document.querySelector('#timing').textContent=s.message;}}
poll();setInterval(poll,2000);if(new URLSearchParams(location.search).has('start'))start();
</script></html>"""


def serve(settings, port):
    view_manifest = verify_release(settings)
    view_root = settings["release_root"]
    settings = prediction_settings(settings)
    state_root = Path(settings["state_root"])
    token = secrets.token_urlsafe(24)
    mutex = threading.Lock()
    current_path = None

    def state():
        if current_path and current_path.exists():
            current = read(current_path)
            if current["status"] in {"claimed", "running"}:
                return {**current, "presentation_version": view_manifest["commit"]}
        try:
            key, details = precheck(settings)
            if details.get("next_session_selection"):
                existing = _target_job(state_root, details["timing"]["target_session"])
                if existing:
                    _, saved = existing
                    return {
                        **saved,
                        "holiday": details["holiday"],
                        "paid_allowed": False,
                        "current_market": details["market"],
                        "presentation_version": view_manifest["commit"],
                    }
            path = state_root / "jobs" / key / "state.json"
            value = read(path) if path.exists() else _idle(details)
            return {**value, "presentation_version": view_manifest["commit"]}
        except ValueError:
            return {
                "status": "precheck_failed",
                "message": "行情、日历或固定版本未通过检查；请更新数据或修复后重新发布",
            }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, kind="application/json; charset=utf-8", status_code=200):
            self.send_response(status_code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.send(PAGE.replace("TOKEN", token).encode(), "text/html; charset=utf-8")
            elif path == "/state":
                self.send(json.dumps(state(), ensure_ascii=False).encode())
            elif path == "/report":
                current = state()
                report = current.get("report_path")
                if report and Path(report).is_file():
                    try:
                        self.send(display_report(current), "text/html; charset=utf-8")
                    except (ValueError, KeyError, OSError):
                        self.send(b'{"status":"report_integrity_failed"}', status_code=409)
                else:
                    self.send(b"{}", status_code=404)
            else:
                self.send(b"{}", status_code=404)

        def do_POST(self):
            nonlocal current_path
            if self.path != "/run" or self.headers.get("X-Scout-Token") != token:
                self.send(b"{}", status_code=403)
                return
            with mutex:
                try:
                    current_path, value, created = claim(settings)
                    if created:
                        threading.Thread(
                            target=supervise, args=(settings, current_path), daemon=True
                        ).start()
                    self.send(json.dumps(value, ensure_ascii=False).encode())
                except ValueError:
                    self.send(b'{"status":"precheck_failed"}', status_code=409)

    with exclusive(state_root / "portal.lock", blocking=False):
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        atomic(
            state_root / "portal.json",
            {
                "url": f"http://127.0.0.1:{server.server_port}",
                "pid": os.getpid(),
                "release": settings["release_root"],
                "presentation_release": view_root,
            },
        )
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    settings = read(args.settings)
    if args.worker:
        worker(settings, args.worker)
    elif args.serve:
        serve(settings, args.port)
    else:
        key, details = precheck(settings)
        print(json.dumps({"key": key, **details}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
