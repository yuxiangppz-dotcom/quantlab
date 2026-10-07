"""Read-only authenticated mobile report server; there is no paid run endpoint."""

import argparse
import getpass
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from quantlab.scout.cloud_artifacts import RUN_ID, guarded, history, read_view
from quantlab.scout.cloud_push import public_origin

TTL = 7 * 86400


def password_hash(password, salt=None):
    if len(password) < 12:
        raise ValueError("Use a viewing password of at least 12 characters")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 300_000).hex()
    return salt + ":" + digest


def password_matches(password, expected):
    try:
        salt, digest = expected.split(":")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 300_000).hex()
        return hmac.compare_digest(actual, digest)
    except ValueError:
        return False


def signed_cookie(secret, now=None):
    value = str(int(now if now is not None else time.time()) + TTL)
    return value + "." + hmac.new(secret.encode(), value.encode(), "sha256").hexdigest()


def valid_cookie(value, secret, now=None):
    try:
        expiry, signature = value.split(".")
        current = int(now if now is not None else time.time())
        expected = hmac.new(secret.encode(), expiry.encode(), "sha256").hexdigest()
        return current < int(expiry) <= current + TTL and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def page(title, content):
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>"
        "body{font:16px/1.7 system-ui;margin:0;background:#f4f6f8;color:#17212b}"
        "main{max-width:800px;margin:auto;padding:24px 18px}h1{font-size:27px}"
        ".card{display:block;background:white;margin:16px 0;padding:20px;border-radius:14px;"
        "color:inherit;text-decoration:none;border:1px solid #dce3e8}"
        "input,button{font:inherit;box-sizing:border-box;padding:12px;width:100%;margin:8px 0}"
        "button{background:#172b3b;color:white;border:0;border-radius:8px}"
        ".muted{color:#607180}</style><main>"
        f"<h1>{escape(title)}</h1>{content}</main></html>"
    ).encode()


def return_path(value):
    if value == "/" or (
        value.startswith("/reports/") and RUN_ID.fullmatch(value.removeprefix("/reports/"))
    ):
        return value
    return "/"


def make_server(root, origin, password, secret, host="127.0.0.1", port=8080, *, local=False):
    root = guarded(root)
    if local:
        if host != "127.0.0.1" or urlsplit(origin).hostname != "127.0.0.1":
            raise ValueError("Local HTTP test must bind loopback")
    else:
        public_origin(origin)
    if len(secret) < 32:
        raise ValueError("Missing web authentication configuration")
    if not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{64}", password):
        raise ValueError("Invalid web password hash")
    attempts = []
    login_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass

        def respond(self, body=b"", code=200, **headers):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # no-referrer makes a native form POST send Origin:null in browsers,
            # which conflicts with the strict same-origin login/logout check.
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                return "scout_session" in cookie and valid_cookie(
                    cookie["scout_session"].value, secret
                )
            except Exception:
                return False

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self.respond(b"ok")
            elif path == "/login":
                next_path = return_path(parse_qs(urlsplit(self.path).query).get("next", ["/"])[0])
                self.respond(
                    page(
                        "Scout 选股报告",
                        "<p>输入查看密码，打开已保存的报告。</p>"
                        "<form method='post' action='/login'><label for='password'>查看密码</label>"
                        f"<input type='hidden' name='next' value='{escape(next_path, quote=True)}'>"
                        "<input id='password' type='password' name='password' required "
                        "autocomplete='current-password' maxlength='256'>"
                        "<button>登录查看</button></form>",
                    )
                )
            elif not self.authenticated():
                self.respond(code=303, Location="/login?next=" + quote(return_path(path), safe=""))
            elif path == "/":
                cards = "".join(
                    "<a class='card' href='/reports/"
                    + escape(row["run_id"], quote=True)
                    + "'><strong>"
                    + escape(row["target_session"])
                    + " 选股报告</strong><br><span class='muted'>行情截至 "
                    + escape(row["asof_session"])
                    + " · 生成于 "
                    + escape(row["generated_at"])
                    + "</span></a>"
                    for row in history(root)
                )
                self.respond(
                    page(
                        "Scout 选股报告",
                        (cards or "<p>暂时没有已完成的报告。</p>")
                        + "<form method='post' action='/logout'><button>退出登录</button></form>",
                    )
                )
            elif path.startswith("/reports/"):
                try:
                    self.respond(read_view(root, path.removeprefix("/reports/")))
                except (ValueError, OSError, KeyError):
                    self.respond(page("报告暂不可用", "<p>报告不存在或未通过完整性核验。</p>"), 404)
            else:
                self.respond(code=404)

        def do_POST(self):
            if self.headers.get("Origin") != origin.rstrip("/"):
                self.respond(code=403)
                return
            if self.path == "/logout":
                self.respond(
                    code=303,
                    Location="/login",
                    Set_Cookie="scout_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
                )
                return
            if self.path != "/login":
                self.respond(code=404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError("Invalid body size")
                fields = parse_qs(self.rfile.read(size).decode())
                password_value = fields["password"][0]
                next_path = return_path(fields.get("next", ["/"])[0])
                if len(password_value) > 256:
                    raise ValueError("Password too long")
            except (ValueError, KeyError, UnicodeError):
                self.respond(code=400)
                return
            now = time.time()
            with login_lock:
                attempts[:] = [at for at in attempts if now - at < 300]
                limited = len(attempts) >= 10
                if not limited:
                    attempts.append(now)
            if limited:
                self.respond(page("请稍后再试", "<p>登录尝试较多，请五分钟后重试。</p>"), 429)
            elif not password_matches(password_value, password):
                self.respond(page("密码不正确", "<p><a href='/login'>返回登录</a></p>"), 401)
            else:
                cookie = (
                    f"scout_session={signed_cookie(secret)}; Path=/; Max-Age={TTL}; "
                    "HttpOnly; SameSite=Lax" + ("" if local else "; Secure")
                )
                self.respond(code=303, Location=next_path, Set_Cookie=cookie)

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/scout"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--local-http", action="store_true")
    parser.add_argument("--create-web-secrets", type=Path)
    args = parser.parse_args()
    if args.create_web_secrets:
        password = getpass.getpass("设置手机报告查看密码（至少12字符）：")
        if password != getpass.getpass("再次输入："):
            raise ValueError("Passwords differ")
        fd = os.open(args.create_web_secrets, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("SCOUT_WEB_PASSWORD_HASH=" + password_hash(password) + "\n")
            stream.write("SCOUT_SESSION_SECRET=" + secrets.token_hex(32) + "\n")
        return
    server = make_server(
        args.root,
        os.environ["SCOUT_PUBLIC_URL"],
        os.environ["SCOUT_WEB_PASSWORD_HASH"],
        os.environ["SCOUT_SESSION_SECRET"],
        args.host,
        args.port,
        local=args.local_http,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
