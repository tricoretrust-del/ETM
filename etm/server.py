"""HTTP 서버(표준 라이브러리). 화면(static/index.html)과 JSON API 를 제공한다."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import contract, db, dialog, learn, llm, plan, process, seed, textparse

STATIC = Path(__file__).parent / "static"


class App:
    def __init__(self, db_path: str):
        self.conn = db.connect(db_path)
        self.lock = dialog.LOCK  # SQLite 단일 연결 직렬화(문답 모듈과 같은 잠금 — API 대기 중에는 풀린다)

    def learning_command(self, cmd: str, b: dict, actor: str):
        c = self.conn
        fns = {
            "create_lag": lambda: {"lag_id": learn.create_lag(
                c, actor=actor, question=b.get("question", ""), context=b.get("context") or {},
                case_ids=b.get("case_ids") or [], event_ids=b.get("event_ids") or [])},
            "verify": lambda: {"verification_id": learn.add_verification(
                c, lag_id=int(b["lag_id"]), actor=actor, method=b.get("method") or "사례 대조",
                case_ids=b.get("case_ids") or [], expected=b.get("expected") or "", limits=b.get("limits") or "",
                observed=b.get("observed"), plan_ids=b.get("plan_ids") or [], event_ids=b.get("event_ids") or [],
                intake_ids=b.get("intake_ids") or [])},
            "judge": lambda: learn.judge_lag(c, lag_id=int(b["lag_id"]), actor=actor, kind=b.get("kind", ""),
                                             reason=b.get("reason") or "", scope=b.get("scope"), action=b.get("action")),
            "approve": lambda: learn.approve_version(c, version_id=int(b["version_id"]), actor=actor,
                                                     reason=b.get("reason") or ""),
            "set_status": lambda: learn.set_version_status(c, version_id=int(b["version_id"]), actor=actor,
                                                           to=b.get("to", ""), reason=b.get("reason") or ""),
        }
        if cmd not in fns:
            raise KeyError("알 수 없는 명령")
        with process.Tx(c):
            result = learn.idempotent(c, b.get("request_id"), cmd, fns[cmd])
        return {"result": result, **learn.overview(c)}

    def plan_command(self, cmd: str, b: dict, actor: str):
        c = self.conn
        ev = b.get("expected_version")
        fns = {
            "create": lambda: {"plan_id": plan.create(
                c, actor=actor, title=b.get("title", ""), purpose=b.get("purpose"), action=b.get("action"),
                done_criteria=b.get("done_criteria"), assignee=b.get("assignee"), due_date=b.get("due_date"),
                links=b.get("links") or [], execute_now=bool(b.get("execute_now")))},
            "approve": lambda: plan.approve(c, plan_id=int(b["plan_id"]), actor=actor, expected_version=ev,
                                            reason=b.get("reason") or ""),
            "transition": lambda: plan.transition(c, plan_id=int(b["plan_id"]), actor=actor, to=b.get("to", ""),
                                                  reason=b.get("reason") or "", expected_version=ev),
            "complete": lambda: plan.complete(c, plan_id=int(b["plan_id"]), actor=actor, result=b.get("result") or "",
                                              evidence=b.get("evidence") or [], expected_version=ev),
            "reopen": lambda: plan.reopen(c, plan_id=int(b["plan_id"]), actor=actor, reason=b.get("reason") or "",
                                          expected_version=ev),
            "edit": lambda: plan.edit(c, plan_id=int(b["plan_id"]), actor=actor, changes=b.get("changes") or {},
                                      expected_version=ev, reason=b.get("reason") or ""),
            "link": lambda: {"link_id": plan.link(c, plan_id=int(b["plan_id"]), actor=actor, role=b.get("role", "basis"),
                                                  kind=b.get("kind", ""), target_id=b.get("id"), note=b.get("note"))},
            "unlink": lambda: plan.unlink(c, link_id=int(b["link_id"]), actor=actor, reason=b.get("reason") or "") or {},
        }
        if cmd not in fns:
            raise KeyError("알 수 없는 명령")
        with process.Tx(c):
            result = learn.idempotent(c, b.get("request_id"), f"plan.{cmd}", fns[cmd])
        return {"result": result, **learn.overview(c)}

    def dialog_api_route(self, method, path, body, actor):
        """문답 시작·답변·다시 해석. DB 작업만 잠그고 API 응답 대기 중에는 다른 요청을 막지 않는다."""
        c = self.conn
        if method != "POST":
            return None
        if path == "/api/dialogs":
            return 200, dialog.analyze_input(c, text=(body or {}).get("text", ""), actor=actor)
        m = re.fullmatch(r"/api/dialogs/(\d+)/answer", path)
        if m:
            b = body or {}
            return 200, dialog.answer_question(c, dialog_id=int(m[1]), question_id=int(b["question_id"]),
                                               text=b.get("text"), choice=b.get("choice"), actor=actor,
                                               expected_revision=b.get("revision"))
        m = re.fullmatch(r"/api/dialogs/(\d+)/retry", path)
        if m:
            return 200, dialog.retry(c, dialog_id=int(m[1]), actor=actor)
        return None

    def handle(self, method: str, path: str, body: dict | None, actor: str = "owner"):
        process.ACTOR.set(actor)
        api = self.dialog_api_route(method, path, body, actor)   # 외부 API 를 부르는 요청: 잠금을 스스로 관리
        if api is not None:
            return api
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
            if method == "GET" and path == "/api/learning/timeline":
                return 200, learn.timeline(c)
            if method == "GET" and path == "/api/learning":
                return 200, learn.overview(c)
            m = re.fullmatch(r"/api/learning/(\w+)", path)
            if method == "POST" and m:
                return 200, self.learning_command(m[1], body or {}, actor)
            if method == "GET" and path == "/api/plans":
                return 200, {"plans": plan.list_plans(c)}
            m = re.fullmatch(r"/api/plans/(\d+)", path)
            if method == "GET" and m:
                return 200, plan.detail(c, int(m[1]))
            m = re.fullmatch(r"/api/plans/(\w+)", path)
            if method == "POST" and m:
                return 200, self.plan_command(m[1], body or {}, actor)
            m = re.fullmatch(r"/api/rule-versions/(\d+)/trace", path)
            if method == "GET" and m:
                return 200, learn.trace_version(c, int(m[1]))
            m = re.fullmatch(r"/api/events/(\d+)", path)
            if method == "GET" and m:
                return 200, process.event_detail(c, int(m[1]))
            # ---- DEV-004 v0.2 문답 조회·후보 (API 를 부르는 요청은 dialog_api_route)
            if method == "GET" and path == "/api/dialogs":
                cfg = llm.config()
                return 200, {"dialogs": dialog.list_dialogs(c), "provider": cfg["provider"], "model": cfg["model"],
                             "metrics": dialog.metrics(c)}
            m = re.fullmatch(r"/api/dialogs/(\d+)", path)
            if method == "GET" and m:
                return 200, dialog.detail(c, int(m[1]))
            m = re.fullmatch(r"/api/candidates/(\d+)/(accept|dismiss)", path)
            if method == "POST" and m:
                if m[2] == "accept":
                    return 200, dialog.accept_candidate(c, candidate_id=int(m[1]), actor=actor)
                dialog.dismiss_candidate(c, candidate_id=int(m[1]), actor=actor)
                return 200, {"ok": True}
            if method == "POST" and path == "/api/intake":
                body = body or {}
                notes: list[str] = []
                if "text" in body and "input_id" not in body:
                    # JSON 이 아닌 입력: 입력 계약 형식으로 자동 변환 후 접수
                    body, notes = textparse.to_payload(body["text"], process.customer_names(c))
                res = process.intake(c, body)
                res["converted"] = bool(notes) or "raw_text" in body
                res["notes"] = notes
                return 200, res
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
                status, payload = app.handle(method, path, body, self.headers.get("X-ETM-User") or "owner")
            except learn.Forbidden as e:
                status, payload = 403, {"error": str(e)}
            except learn.Conflict as e:
                status, payload = 409, {"error": str(e)}
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
