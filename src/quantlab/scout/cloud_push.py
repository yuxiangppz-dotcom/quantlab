"""ServerChan Turbo notification outbox: provider acceptance is not phone delivery."""

import json
import os
import re
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quantlab.scout.cloud_artifacts import guarded
from quantlab.scout.daily_runtime import atomic, exclusive, read
from quantlab.scout.models import SHANGHAI, fingerprint


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_origin(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Public report origin must be HTTPS without credentials or a path")
    return value.rstrip("/")


def send_http(key, payload):
    request = Request(
        f"https://sctapi.ftqq.com/{key}.send",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        return json.loads(response.read(64_000))


def notify(root, metadata, origin, *, transport=None):
    root = guarded(root)
    transport = transport or send_http
    origin = public_origin(origin)
    key = os.environ.get("SERVERCHAN_SENDKEY", "")
    if not key:
        return {"status": "not_configured"}
    if not re.fullmatch(r"SCT[A-Za-z0-9]{10,200}", key):
        return {"status": "invalid_turbo_sendkey"}
    identity = fingerprint([metadata["source_report_sha256"], fingerprint(key), "turbo"])
    path = root / "outbox" / (identity + ".json")
    with exclusive(root / "notification.lock"):
        if path.exists():
            return read(path)
        receipt = {
            "run_id": metadata["run_id"],
            "status": "delivery_unknown",
            "attempted_at": datetime.now(SHANGHAI).isoformat(),
            "note": "Intent persisted before send; unknown delivery is never auto-retried",
        }
        atomic(path, receipt)
        if metadata.get("failure"):
            payload = {
                "title": f"Scout {metadata['target_session']} 预测未完成",
                "desp": f"本次未发布新候选，旧报告保留。\n\n[查看已保存报告]({origin}/)",
                "short": "预测失败或状态未知，未发布新候选",
            }
        else:
            payload = {
                "title": f"Scout {metadata['target_session']} 选股报告已生成",
                "desp": f"行情截至 {metadata['asof_session']}。\n\n"
                f"[查看手机报告]({origin}/reports/{metadata['run_id']})\n\n"
                "需要登录。研究候选，效果待前瞻观察。",
                "short": "报告已保存，点击查看候选和理由",
            }
        try:
            result = transport(key, payload)
            receipt["status"] = "provider_accepted" if result.get("code") == 0 else "rejected"
        except HTTPError as exc:
            receipt["status"] = "rejected" if 400 <= exc.code < 500 else "delivery_unknown"
            receipt["http_status"] = exc.code
        except Exception as exc:
            receipt["error_type"] = type(exc).__name__
        atomic(path, receipt)
        return receipt


def notify_failure(root, target, origin):
    return notify(
        root,
        {
            "failure": True,
            "target_session": target,
            "run_id": "failed-" + target,
            "source_report_sha256": fingerprint(["failed", target]),
        },
        origin,
    )
