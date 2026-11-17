"""HTTP 서버(표준 라이브러리). 화면(static/index.html)과 JSON API 를 제공한다."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import contract, db, process, seed

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self, db_path: str):
        self.conn = db.connect(db_path)
        self.lock = threading.Lock()  # SQLite 단일 연결 직렬화

    def handle(self, method: str, path: str, body: dict | None):
        with self.lock:
            c = self.conn
            if method == "GET" and path == "/api/history":
                return 200, {"days": process.history(c)}
            if method == "GET" and path == "/api/summary":
                return 200, process.summary(c)
            if method == "GET" and path == "/api/examples":
                return 200, seed.examples()
            if method == "GET" and path == "/api/items":
                return 200, [dict(r) for r in c.execute("SELECT id, code, name, unit FROM item WHERE kind='product'")]
            m = re.fullmatch(r"/api/events/(\d+)", path)
            if method == "GET" and m:
                return 200, process.event_detail(c, int(m[1]))
            if method == "POST" and path == "/api/intake":
                return 200, process.intake(c, body or {})
            m = re.fullmatch(r"/api/events/(\d+)/answers", path)
            if method == "POST" and m:
                return 200, process.answer(c, int(m[1]), (body or {}).get("answers", {}))
            m = re.fullmatch(r"/api/events/(\d+)/revise", path)
            if method == "POST" and m:
                return 200, process.revise(c, int(m[1]), (body or {}).get("changes", {}))
            return 404, {"error": "not found"}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 조용히
            pass

        def _send(self, status, payload, ctype="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            path = urlparse(self.path).path
            if method == "GET" and not path.startswith("/api/"):
                f = STATIC / ("index.html" if path in ("/", "") else path.lstrip("/"))
                if f.is_file() and STATIC in f.resolve().parents:
                    ctype = "text/html; charset=utf-8" if f.suffix == ".html" else "text/plain; charset=utf-8"
                    return self._send(200, f.read_bytes(), ctype)
                return self._send(404, {"error": "not found"})
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": "JSON 형식이 아닙니다"})
            try:
                status, payload = app.handle(method, path, body)
            except contract.ContractError as e:
                status, payload = 400, {"error": "입력 형식 오류", "errors": e.errors}
            except (ValueError, KeyError) as e:
                status, payload = 400, {"error": str(e).strip("'")}
            except sqlite3.Error as e:
                status, payload = 500, {"error": f"저장 실패: {e}. 변경 내용은 반영되지 않았습니다"}
            self._send(status, payload)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000):
    app = App(db_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"ETM 실행 중: http://{host}:{port}  (DB: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
