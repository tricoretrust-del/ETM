"""API 기반 문답·사건 처리 (DEV-004 v0.2).

흐름: 입력 → analyze(API 해석) → 질문 → 사용자 답변(자연어, 원문 보존) → 재호출 → 서버 검증 → 기존 엔진으로 반영
      → 확정 사례 보존 → 다음 입력에서 사례·활성 RBK 를 근거로 검색해 API 요청에 포함.

책임 분리
- API: 의미 해석·질문·처리안·학습 후보 '제안'.
- 서버: 회사 범위·근거 ID 실존·리비전·'아는 값' 기준·지원 동작 검사, 트랜잭션 반영.
- 사용자: 불확실한 사실과 일반 규칙의 판정.

'아는 값' 기준 (CLD 제안, TRNK 확인 대상 — CLD-REQ-004)
  자료 종류(방향)와 품목 연결은 아래 경우에만 확정으로 본다.
  (a) 원문에 명시(종류는 원문에 '출하/송장/입고/반납' 표기가 있을 때만)  (b) 이번 문답의 사용자 답
  (c) 서버가 조건 일치를 확인한 활성 RBK  (d) 품목은 기준정보 이름·코드·별칭 정확 일치(기존 코드 기본 동작)
  과거 사례·모델 추론(case_inference / model_inference)은 '제안'으로 보여 주고 확인을 받는다.
"""
from __future__ import annotations

import json
import re
import threading

from . import contract, learn, llm, process, textparse
from .learn import Conflict, Forbidden
from .process import Tx, jdump, norm, now

KIND_TO_DOC = {"shipment": "shipment_invoice", "receipt": "receipt", "return": "other", "other": "other"}
ACTION_TO_KIND = {"shipment_apply": "shipment", "receipt_record": "receipt", "keep_only": "other"}
KIND_CHOICES = {"출하": "shipment", "입고": "receipt", "반납": "return", "재고와 무관": "other"}
KIND_LABEL = {"shipment": "출하 (공장 → 거래처)", "receipt": "입고 (→ 공장)", "return": "반납", "other": "재고와 무관 (보관만)"}
KIND_WORDS = {"shipment": ("출하", "송장"), "receipt": ("입고",), "return": ("반납", "회수")}
YES = re.compile(r"^\s*(맞(음|아|아요|습니다)?|네|예|응|ㅇㅇ|ok|yes|그래|그렇다|맞다)\s*[.!]?\s*$", re.I)
YES_PREFIX = re.compile(r"^\s*(맞(음|아|아요|습니다)?|네|예|응|그래|맞다)\s*[.,!]\s*\S", re.I)
NO_EFFECT_ITEM = "none"
# 단일 SQLite 연결을 여러 요청 스레드가 함께 쓰므로 DB 작업은 이 잠금 안에서만 한다(서버도 같은 잠금을 쓴다).
# API 응답을 기다리는 동안에는 잠금을 잡지 않는다.
LOCK = threading.RLock()
INFLIGHT: set[int] = set()   # API 호출이 진행 중인 문답
doc_marker = learn.doc_marker
_marker_conflicts = learn.marker_conflicts


def _dialog(conn, did):
    d = conn.execute("SELECT * FROM dialog WHERE id=?", (did,)).fetchone()
    if not d:
        raise ValueError("문답이 없습니다")
    return d


def _state(d) -> dict:
    return json.loads(d["state"] or "{}")


def _set_state(conn, did, state, **cols):
    sets = ", ".join(["state=?", "updated_at=?"] + [f"{k}=?" for k in cols])
    conn.execute(f"UPDATE dialog SET {sets} WHERE id=?", (jdump(state), now(), *cols.values(), did))


def _bump(conn, did):
    conn.execute("UPDATE dialog SET revision = revision + 1, updated_at=? WHERE id=?", (now(), did))


def detail(conn, did) -> dict:
    d = dict(_dialog(conn, did))
    d["state"] = json.loads(d["state"] or "{}")
    d["extracted"] = json.loads(d["extracted"] or "{}")
    d["hypothesis"] = json.loads(d["hypothesis"]) if d["hypothesis"] else None
    d["questions"] = [dict(q) | {"options": json.loads(q["options"])} for q in conn.execute(
        "SELECT * FROM dialog_question WHERE dialog_id=? ORDER BY id", (did,))]
    d["answers"] = [dict(a) for a in conn.execute("SELECT * FROM dialog_answer WHERE dialog_id=? ORDER BY id", (did,))]
    d["open_question"] = next((q for q in d["questions"] if q["status"] == "open"), None)
    d["candidates"] = [dict(c) for c in conn.execute("SELECT * FROM learning_candidate WHERE dialog_id=? ORDER BY id", (did,))]
    d["calls"] = [dict(c) for c in conn.execute(
        "SELECT id, request_id, provider, model, status, error, latency_ms, usage, dialog_revision, created_at"
        " FROM llm_call WHERE dialog_id=? ORDER BY id", (did,))]
    d["proposals"] = [dict(p) for p in conn.execute("SELECT * FROM dialog_proposal WHERE dialog_id=? ORDER BY id", (did,))]
    return d


# --------------------------------------------------------------------------- 근거 검색 (회사 범위 먼저, 관련된 것만)
def _context(conn, d) -> tuple[dict, list[str]]:
    company = d["company_id"]
    raw_n = norm(d["raw_input"])
    ev = []
    items = [{"ref": f"item:{r['id']}", "name": r["name"], "code": r["code"], "unit": r["unit"], "kind": r["kind"],
              "aliases": json.loads(r["aliases"] or "[]")}
             for r in conn.execute("SELECT * FROM item ORDER BY kind, id LIMIT 50")]
    parties = [{"ref": f"party:{r['id']}", "name": r["name"], "kind": r["kind"]}
               for r in conn.execute("SELECT * FROM party ORDER BY id LIMIT 30")]
    cases = []
    for r in conn.execute("SELECT * FROM decision_case WHERE company_id=? AND decision_kind IN ('doc_kind','item_map')"
                          " ORDER BY id DESC LIMIT 200", (company,)):
        f = learn.facts_from_key(r["decision_kind"], r["decision_key"])
        key_parts = [str(v) for k, v in f.items() if k != "marker" and v]
        if not any(len(p) >= 2 and norm(p) in raw_n for p in key_parts):
            continue
        cases.append({"ref": f"case:{r['id']}", "decision": r["decision_kind"], "situation": r["label"],
                      "answer": json.loads(r["answer"]), "by": "규칙" if r["rule_version_id"] else "사용자 확정",
                      "event": r["event_id"]})
        if len(cases) >= 10:
            break
    rules = []
    for v in conn.execute(
            "SELECT v.*, r.decision, r.event_kind FROM rule_version v JOIN rule r ON r.id=v.rule_id WHERE v.status='active'"
            " AND (r.company_id=? OR r.company_id IS NULL) AND r.decision IN ('doc_kind','item_map') ORDER BY v.id LIMIT 20",
            (company,)):
        rules.append({"ref": f"rule:{v['id']}", "rule": f"{v['rule_id']} v{v['version_no']}", "decision": v["decision"],
                      "condition": json.loads(v["condition"]), "action": json.loads(v["action"]),
                      "description": v["description"], "approved": True})
    crow = conn.execute("SELECT name, aliases FROM company WHERE id=?", (company,)).fetchone()
    company_name = crow["name"]
    company_names = [company_name, *json.loads(crow["aliases"] or "[]")]
    ev = [x["ref"] for x in cases] + [x["ref"] for x in rules]
    return {"company": {"id": company, "name": company_name, "names_in_documents": company_names,
                        "note": "자사(우리 회사). 문서에 이 이름이 받는 곳이면 입고, 보내는 곳이면 출하일 가능성이 높다. "
                                "counterparty 에는 자사가 아닌 상대방만 넣는다(입고=보낸 업체, 출하=받는 거래처)."},
            "items": items, "parties": parties, "past_confirmed_cases": cases, "active_rules": rules,
            "document_marker": doc_marker(d["raw_input"]),
            "supported_actions": ["shipment_apply: 출하 반영(재고 차감·받을 돈)", "receipt_record: 입고 기록(공장으로 이동)",
                                  "keep_only: 재고와 무관(원본·의미만 보관)",
                                  "반납·회수·기타 방향: 현재 처리 엔진 없음 → 사용자가 반납이라고 답하면 keep_only(보관만)로 제안하고 더 묻지 않는다"],
            "server_required_checks": ["손글씨 차량번호·기사전화 확인", "출하 팔레트 송장·상차 메시지 대조"]}, ev


def _messages(conn, d, ctx) -> list[dict]:
    qa = []
    for q in conn.execute("SELECT * FROM dialog_question WHERE dialog_id=? ORDER BY id", (d["id"],)):
        a = conn.execute("SELECT * FROM dialog_answer WHERE question_id=? ORDER BY id DESC LIMIT 1", (q["id"],)).fetchone()
        qa.append({"question_id": q["qkey"], "question": q["text"], "target": q["target"], "proposed": q["proposed"],
                   "answer_ref": f"answer:{a['id']}" if a else None, "answer": a["raw_text"] if a else None})
    allowed = [x["ref"] for x in ctx["items"] + ctx["parties"] + ctx["past_confirmed_cases"] + ctx["active_rules"]]
    allowed += [x["answer_ref"] for x in qa if x["answer_ref"]]
    payload = {"input_raw": d["raw_input"], "assist_extraction": json.loads(d["extracted"] or "{}"),
               "dialog": qa, "confirmed_state": _state(d), "context": ctx,
               "allowed_evidence_refs": allowed,
               "evidence_rule": "근거 ID 는 allowed_evidence_refs 에 있는 것만 쓴다. 원문 항목은 'source:항목명'으로 쓴다."}
    return [{"role": "system", "content": llm.SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


# --------------------------------------------------------------------------- 공개 명령
def analyze_input(conn, *, text: str, actor: str, company_id: int = 1) -> dict:
    """새 자료로 문답 시작 → API 해석 1회."""
    with LOCK:
        learn.user(conn, actor)
        if not (text or "").strip():
            raise ValueError("입력 내용이 비어 있습니다")
        same = conn.execute("SELECT id FROM dialog WHERE company_id=? AND raw_input=? AND status IN ('asking','failed')"
                            " ORDER BY id DESC LIMIT 1", (company_id, text)).fetchone()
        if same:   # 같은 자료를 다시 보냄(중복 클릭·재접속) → 진행 중인 문답으로 이어 간다. API 재호출 없음
            return detail(conn, same["id"])
        try:
            ext, _ = textparse.to_payload(text, process.customer_names(conn))
        except ValueError:
            ext = {}
        with Tx(conn):
            t = now()
            did = conn.execute("INSERT INTO dialog(company_id, status, raw_input, extracted, state, created_by, created_at,"
                               " updated_at) VALUES (?, 'asking', ?, ?, '{}', ?, ?, ?)",
                               (company_id, text, jdump({k: v for k, v in ext.items() if k != "raw_text"}), actor, t, t)).lastrowid
            process.log(conn, None, "dialog", f"문답 #{did} 시작 · {text.strip().splitlines()[0][:40]}")
    return run_turn(conn, did)


def answer_question(conn, *, dialog_id: int, question_id: int, text: str | None = None, choice: str | None = None,
                    actor: str, expected_revision: int | None = None) -> dict:
    """사용자 답변을 먼저 저장하고 서버 상태에 반영한다. 서버가 결정적으로 처리할 수 있으면 API 없이 진행,
    아니면 재호출."""
    with LOCK:
        learn.user(conn, actor)
        raw = (text if text is not None else choice) or ""
        if not raw.strip():
            raise ValueError("답변이 비어 있습니다")
        need_api = True
        with Tx(conn):
            d = _dialog(conn, dialog_id)
            if expected_revision is not None and int(expected_revision) != d["revision"]:
                raise Conflict("문답이 그사이 바뀌었습니다. 새로 불러와 다시 답하세요")
            q = conn.execute("SELECT * FROM dialog_question WHERE id=? AND dialog_id=?", (question_id, dialog_id)).fetchone()
            if not q or q["status"] != "open":
                raise Conflict("이미 답했거나 없는 질문입니다")
            aid = conn.execute("INSERT INTO dialog_answer(dialog_id, question_id, raw_text, actor, at) VALUES (?,?,?,?,?)",
                               (dialog_id, question_id, raw, actor, now())).lastrowid
            conn.execute("UPDATE dialog_question SET status='answered' WHERE id=?", (question_id,))
            _bump(conn, dialog_id)
            state = _state(d)
            # 서버가 만든 확인 질문: 선택·'맞음'은 그 질문이 제시한 값에만 적용(대상 없는 승인은 만들지 않음)
            resolved = _resolve_server_answer(conn, q, choice, text, state, aid)
            if resolved:
                _set_state(conn, dialog_id, state)
                # 긍정 뒤에 말이 더 있으면(예: '맞아. 다음에도 …') 일반화 지시일 수 있으므로 API 로 해석도 받는다
                need_api = bool(text and YES_PREFIX.match(text))
        if not need_api:
            return _continue_after_server_answer(conn, dialog_id)
    return run_turn(conn, dialog_id)


def retry(conn, *, dialog_id: int, actor: str) -> dict:
    with LOCK:
        learn.user(conn, actor)
        d = _dialog(conn, dialog_id)
        if d["status"] not in ("failed", "asking"):
            raise Conflict("다시 해석할 수 있는 상태가 아닙니다")
    return run_turn(conn, dialog_id)


# --------------------------------------------------------------------------- API 턴
def run_turn(conn, did) -> dict:
    """API 턴. DB 작업만 잠금(LOCK) 안에서 하고, API 를 기다리는 동안에는 잠금을 풀어 다른 요청을 막지 않는다.
    같은 문답에 대한 API 호출이 이미 진행 중이면 거부한다(외부 호출 중복 방지). 늦게 온 응답은 리비전으로 버린다."""
    with LOCK:
        if did in INFLIGHT:
            raise Conflict("이 자료를 해석하는 중입니다. 잠시 후 다시 확인하세요")
        d = _dialog(conn, did)
        n_calls = conn.execute("SELECT COUNT(*) FROM llm_call WHERE dialog_id=?", (did,)).fetchone()[0]
        if n_calls >= llm.MAX_CALLS_PER_DIALOG:
            with Tx(conn):
                conn.execute("UPDATE dialog SET status='failed', status_reason=?, updated_at=? WHERE id=?",
                             (f"API 호출 한도({llm.MAX_CALLS_PER_DIALOG}회) 도달 — 반영 보류", now(), did))
            return detail(conn, did)
        rev = d["revision"]
        ctx, evidence = _context(conn, d)
        messages = _messages(conn, d, ctx)
        INFLIGHT.add(did)
    try:
        for attempt in (1, 2):
            try:
                out = llm.call(conn, dialog_id=did, revision=rev, messages=messages, evidence=evidence, lock=LOCK)
            except llm.LLMError as e:
                with LOCK, Tx(conn):
                    conn.execute("UPDATE dialog SET status='failed', status_reason=?, updated_at=? WHERE id=?",
                                 (f"API 실패 — 반영 보류, 원문·답변 보존: {e}", now(), did))
                return detail_locked(conn, did)
            with LOCK:
                bad = [r for r in _all_refs(out["response"]) if not _ref_ok(conn, r, d["company_id"])]
                if not bad:
                    break
                conn.execute("UPDATE llm_call SET status='invalid', error=? WHERE request_id=?",
                             (f"없거나 다른 회사의 근거 ID: {', '.join(bad[:5])}", out["request_id"]))
                if attempt == 2 or conn.execute("SELECT COUNT(*) FROM llm_call WHERE dialog_id=?",
                                                (did,)).fetchone()[0] >= llm.MAX_CALLS_PER_DIALOG:
                    with Tx(conn):
                        conn.execute("UPDATE dialog SET status='failed', status_reason=?, updated_at=? WHERE id=?",
                                     (f"API 응답 검증 실패(근거 ID {', '.join(bad[:3])}) — 반영 보류", now(), did))
                    return detail(conn, did)
            messages = messages + [{"role": "assistant", "content": json.dumps(out["response"], ensure_ascii=False)},
                                   {"role": "user", "content": f"서버 검증 실패: 존재하지 않는 근거 ID {bad}. "
                                                               "allowed_evidence_refs 에 있는 ID 만 쓰고 다시 답하라."}]
        with LOCK:
            with Tx(conn):
                d = _dialog(conn, did)
                if d["revision"] != rev:   # 새 답변으로 상태가 바뀐 뒤 늦게 온 응답은 버린다
                    conn.execute("UPDATE llm_call SET status='stale' WHERE request_id=?", (out["request_id"],))
                else:
                    _process_response(conn, d, out["response"], out["request_id"])
            return detail(conn, did)
    finally:
        with LOCK:
            INFLIGHT.discard(did)


def detail_locked(conn, did) -> dict:
    with LOCK:
        return detail(conn, did)


def _ref_ok(conn, ref: str, company: int) -> bool:
    m = re.fullmatch(r"(case|rule|item|party|answer|event):(\d+)", ref or "")
    if not m:
        return ref.startswith("source:")
    kind, i = m[1], int(m[2])
    if kind == "case":
        return bool(conn.execute("SELECT 1 FROM decision_case WHERE id=? AND company_id=?", (i, company)).fetchone())
    if kind == "rule":
        return bool(conn.execute("SELECT 1 FROM rule_version v JOIN rule r ON r.id=v.rule_id WHERE v.id=? AND"
                                 " (r.company_id=? OR r.company_id IS NULL)", (i, company)).fetchone())
    if kind == "event":
        return bool(conn.execute("SELECT 1 FROM event WHERE id=? AND company_id=?", (i, company)).fetchone())
    if kind == "answer":
        return bool(conn.execute("SELECT 1 FROM dialog_answer a JOIN dialog d ON d.id=a.dialog_id WHERE a.id=?"
                                 " AND d.company_id=?", (i, company)).fetchone())
    table = {"item": "item", "party": "party"}[kind]
    return bool(conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (i,)).fetchone())


def _all_refs(resp) -> list[str]:
    refs = list(resp.get("evidence_refs") or [])
    for f in resp.get("field_proposals") or []:
        refs += f.get("evidence_refs") or []
    pp = resp.get("processing_proposal") or {}
    refs += pp.get("kind_refs") or []
    refs += [ln["item_ref"] for ln in pp.get("lines") or [] if ln.get("item_ref")]
    refs += [ln["owner_ref"] for ln in pp.get("lines") or [] if ln.get("owner_ref") and ln["owner_ref"] != "own"]
    refs += [c["target_ref"] for c in resp.get("learning_candidates") or [] if c.get("target_ref")]
    return refs


def _process_response(conn, d, resp, request_id):
    did, company = d["id"], d["company_id"]
    bad = [r for r in _all_refs(resp) if not _ref_ok(conn, r, company)]
    if bad:
        conn.execute("UPDATE llm_call SET status='invalid', error=? WHERE request_id=?",
                     (f"없거나 다른 회사의 근거 ID: {', '.join(bad[:5])}", request_id))
        conn.execute("UPDATE dialog SET status='failed', status_reason=?, updated_at=? WHERE id=?",
                     (f"API 응답 검증 실패(근거 ID {', '.join(bad[:3])}) — 반영 보류", now(), did))
        return
    conn.execute("UPDATE dialog SET hypothesis=?, status_reason=NULL WHERE id=?", (jdump(resp["document_hypothesis"]), did))
    state = _state(d)
    last_a = conn.execute("SELECT * FROM dialog_answer WHERE dialog_id=? ORDER BY id DESC LIMIT 1", (did,)).fetchone()
    ai = resp.get("answer_interpretation")
    if ai and last_a:
        conn.execute("UPDATE dialog_answer SET interpretation=? WHERE id=?", (jdump(ai), last_a["id"]))
    for c in resp.get("learning_candidates") or []:
        explicit = bool(c.get("explicit_user_instruction")) and bool(ai and ai.get("generalize")) and last_a is not None
        payload = None
        if c["kind"] == "alias":
            # 기준정보 별칭: 사용자가 이번 문답에서 직접 말한 사실만. 별칭 글자가 사용자 답에 그대로 있어야 한다
            if last_a is None or not c.get("alias") or not str(c.get("target_ref") or "").startswith("item:") \
                    or norm(c["alias"]) not in norm(last_a["raw_text"]):
                continue
            payload = jdump({"target_ref": c["target_ref"], "alias": c["alias"].strip()})
            explicit = True
        conn.execute("INSERT INTO learning_candidate(dialog_id, kind, text, scope, explicit_instruction, answer_id, status,"
                     " created_at, payload) VALUES (?,?,?,?,?,?, 'suggested', ?, ?)",
                     (did, c["kind"], c["text"], c.get("scope") or "", int(explicit),
                      last_a["id"] if explicit else None, now(), payload))
    pp = resp["processing_proposal"]
    # 사용자 답으로 종류가 확정된 경우(API 해석) — 원문 답변이 있어야만 인정
    kind = ACTION_TO_KIND.get(pp["action"])
    hk = (resp.get("document_hypothesis") or {}).get("kind")
    kind_q = last_a is not None and conn.execute("SELECT 1 FROM dialog_question WHERE id=? AND target='kind'",
                                                 (last_a["question_id"],)).fetchone()
    if (pp["kind_source"] == "user_answer" or kind_q) and hk in KIND_TO_DOC and hk != kind:
        # 사용자 답의 해석(가설)과 처리 동작이 어긋나면 가설을 따른다(예: 반납인데 동작은 '보관만').
        # 반납은 처리 기준이 없어 동작은 보관이지만 종류는 '반납'으로 남겨 장부 반영 미결로 표시한다.
        kind = hk
        pp = dict(pp, action={"shipment": "shipment_apply", "receipt": "receipt_record"}.get(hk, "keep_only"))
    if kind and (pp["kind_source"] == "user_answer" or kind_q) and last_a is not None and "kind" not in state:
        # 방금 '종류' 질문에 사용자가 답했고 그 해석으로 처리안이 나왔으면 사용자 답으로 본다(같은 질문 반복 방지)
        state["kind"] = {"value": kind, "source": "user_answer", "answer_id": last_a["id"], "via": "api_interpretation"}
    if last_a is not None:
        for i, ln in enumerate(pp.get("lines") or []):
            ref = ln.get("item_ref")
            answered_this = conn.execute("SELECT 1 FROM dialog_question WHERE id=? AND target=?",
                                         (last_a["question_id"], f"lines.{i}.item")).fetchone()
            oref = ln.get("owner_ref")
            owner_q = conn.execute("SELECT 1 FROM dialog_question WHERE id=? AND target=?",
                                   (last_a["question_id"], f"lines.{i}.owner")).fetchone()
            if oref and (ln.get("owner_source") == "user_answer" or owner_q) and str(i) not in state.get("owners", {}):
                lab = "자사" if oref == "own" else conn.execute("SELECT name FROM party WHERE id=?",
                                                               (int(oref.split(":")[1]),)).fetchone()["name"]
                state.setdefault("owners", {})[str(i)] = {"value": oref, "label": lab, "source": "user_answer",
                                                          "answer_id": last_a["id"], "via": "api_interpretation"}
            if ref and (ln.get("item_source") == "user_answer" or answered_this) and str(i) not in state.get("items", {}):
                state.setdefault("items", {})[str(i)] = {"choice": ref, "source": "user_answer", "answer_id": last_a["id"],
                                                          "via": "api_interpretation"}
    if resp["status"] in ("unsupported", "no_effect") and "kind" not in state and pp["action"] == "none":
        pp = dict(pp, action="keep_only", kind_source="model_inference")
    pid = conn.execute("INSERT INTO dialog_proposal(dialog_id, revision, content, status, created_at) VALUES (?,?,?,"
                       " 'proposed', ?)", (did, d["revision"], jdump(pp), now())).lastrowid
    conn.execute("UPDATE dialog_proposal SET status='superseded' WHERE dialog_id=? AND id<>? AND status='proposed'",
                 (did, pid))
    _set_state(conn, did, state)
    nq = resp.get("next_question")
    if resp["status"] == "needs_user" and nq and _repeated(conn, did, nq):
        # API 가 이미 답한 질문을 다시 묻는다 → 서버의 결정적 질문으로 넘긴다(무한 문답 방지)
        _ask(conn, did, d["revision"], "srv-kind", "kind", "unknown_meaning",
             "이 자료는 무엇인가요? (앞의 답으로 처리 방법을 정하지 못했습니다)", list(KIND_CHOICES), None, "server")
        return
    if resp["status"] == "needs_user" and nq:
        _ask(conn, did, d["revision"], nq["question_id"], nq["target"], nq["reason"], nq["text"], nq["options"],
             nq.get("proposed"), "api")
        return
    _gate_and_apply(conn, did, pid)


def _repeated(conn, did, nq) -> bool:
    """같은 대상/같은 문장으로 이미 답을 받은 API 질문이면 True."""
    for q in conn.execute("SELECT q.text, q.target, q.qkey FROM dialog_question q WHERE q.dialog_id=? AND q.origin='api' AND"
                          " EXISTS (SELECT 1 FROM dialog_answer a WHERE a.question_id=q.id)", (did,)):
        if q["text"] == nq["text"] or q["qkey"] == nq["question_id"] or (q["target"] == nq["target"] and nq["target"] == "kind"):
            return True
    return False


def _ask(conn, did, rev, qkey, target, reason, text, options, proposed, origin):
    conn.execute("UPDATE dialog_question SET status='superseded' WHERE dialog_id=? AND status='open'", (did,))
    conn.execute("INSERT INTO dialog_question(dialog_id, revision, qkey, target, reason, text, options, proposed, origin,"
                 " status, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'open', ?)",
                 (did, rev, qkey, target, reason, text, jdump(options or []), proposed, origin, now()))
    conn.execute("UPDATE dialog SET status='asking', updated_at=? WHERE id=?", (now(), did))


# --------------------------------------------------------------------------- 서버 검증 ('아는 값' 기준)
def _kind_known(conn, d, pp, state):
    if "kind" in state:
        k = state["kind"]
        if k.get("via") == "api_interpretation" and _marker_conflicts(d["raw_input"], k["value"]):
            return None   # 모델이 해석한 답이 원문 표지와 충돌 → 다시 확인
        return k
    rk = _rule_kind(conn, d, pp)
    if rk and "value" in rk:   # 승인된 규칙의 조건 범위 안 → 재사용 처리 근거
        return rk
    kind = ACTION_TO_KIND.get(pp["action"])
    marks = {k for w, k in learn.MARKER_KIND.items() if w in d["raw_input"]}
    if len(marks) == 1 and pp["action"] in ("none", "receipt_record", "shipment_apply") \
            and (kind is None or kind in marks):
        # 원문 종류 표기가 한 방향뿐이고 모델 제안과 어긋나지 않음 → 서버가 원문 근거로 확정(모델이 '모름'이어도)
        mk = next(iter(marks))
        if mk in ("receipt", "shipment"):
            return {"value": mk, "source": "document"}
    if not kind:
        return None
    src = pp["kind_source"]
    if _marker_conflicts(d["raw_input"], kind):   # 원문 표지와 다른 방향은 확인 없이 확정하지 않는다
        return None
    if src == "document" and any(w in d["raw_input"] for w in KIND_WORDS.get(kind, ())):
        return {"value": kind, "source": "document"}
    return None


def _doc_facts(d, pp) -> dict:
    """규칙 조건 사실(learn.facts_for 단일 정의) + 이전 형식 규칙 호환용 양식 키."""
    f = learn.facts_for("doc_kind", supplier=pp.get("counterparty") or "", raw=d["raw_input"])
    fk = json.loads(d["extracted"] or "{}").get("form_key")
    return {**f, "form_key": fk} if fk else f


def _rule_kind(conn, d, pp):
    """서버가 직접 활성 규칙을 찾는다(모델이 규칙을 인용했는지와 무관). 경로(직접 지시/LAG 판정)와 무관하게 같은 절차."""
    status, action, ver, basis = learn.decide(conn, event={"company_id": d["company_id"], "trade_date": pp.get("trade_date")},
                                              decision="doc_kind", facts=_doc_facts(d, pp), raw=d["raw_input"])
    if status == "rule":
        kind = learn.DOC_TO_KIND.get(action["value"])
        if kind:
            return {"value": kind, "source": "rbk", "rule_version_id": ver["id"], "basis": basis}
    if status == "conflict":
        return {"conflict": basis}
    return None


def _item_known(conn, ln, idx, state, company=1, supplier=""):
    s = state.get("items", {}).get(str(idx))
    if s:
        return s
    facts = learn.facts_for("item_map", supplier=supplier, item_text=ln["item_text"])
    status, action, ver, basis = learn.decide(conn, event={"company_id": company, "trade_date": None},
                                              decision="item_map", facts=facts)
    if status == "rule":
        return {"choice": action["choice"], "source": "rbk", "rule_version_id": ver["id"], "basis": basis}
    if status == "conflict":
        return None
    from .receipt import _exact_item   # 기준정보 정확 일치는 서버가 직접 확인(모델의 item_ref 를 믿지 않음)
    r = _exact_item(conn, ln["item_text"])
    if r:
        return {"choice": f"item:{r['id']}", "source": "master_exact"}
    return None


def _named_parties(conn, d, selfn) -> list[str]:
    """원문에 이름이 그대로 나오는 알려진 상대 업체(기준정보 거래처 + 사용자가 확정한 사례의 업체)."""
    raw_n = norm(d["raw_input"])
    names = {}
    for r in conn.execute("SELECT name, aliases FROM party"):
        for x in [r["name"], *json.loads(r["aliases"] or "[]")]:
            if x and len(norm(x)) >= 2 and norm(x) in raw_n:
                names[norm(r["name"])] = r["name"]
    for r in conn.execute("SELECT decision_kind, decision_key FROM decision_case WHERE company_id=? AND rule_version_id IS NULL",
                          (d["company_id"],)):
        sup = learn.facts_from_key(r["decision_kind"], r["decision_key"]).get("supplier")
        if sup and len(sup) >= 2 and sup in raw_n and sup not in names:
            m = re.search(re.escape(sup), d["raw_input"], re.I)   # 원문에 적힌 표기 그대로 쓴다
            names[sup] = m[0] if m else sup
    return [v for k, v in names.items() if k not in selfn]


def _self_names(conn, company):
    r = conn.execute("SELECT name, aliases FROM company WHERE id=?", (company,)).fetchone()
    return {norm(x) for x in [r["name"], *json.loads(r["aliases"] or "[]")] if x}


def _gate_and_apply(conn, did, pid):
    d = _dialog(conn, did)
    state = _state(d)
    pp = json.loads(conn.execute("SELECT content FROM dialog_proposal WHERE id=?", (pid,)).fetchone()[0])
    if state.get("counterparty"):
        pp["counterparty"] = state["counterparty"]["value"]
    selfn = _self_names(conn, d["company_id"])
    if not (pp.get("counterparty") or "").strip() and pp["action"] != "keep_only" \
            and (state.get("kind") or {}).get("value") != "other":
        named = _named_parties(conn, d, selfn)
        if len(named) == 1:   # 원문에 이름이 그대로 적힌 알려진 업체가 하나뿐 → 원문 명시로 본다
            pp["counterparty"] = named[0]
            state["counterparty"] = {"value": named[0], "source": "document_name"}
            _set_state(conn, did, state)
        else:                 # 상대 업체는 규칙 조건의 핵심. 추측하지 않고 묻는다
            ext = json.loads(d["extracted"] or "{}")
            opts = [x for x in (ext.get("supplier"), ext.get("customer"), *named) if x and norm(x) not in selfn]
            _ask(conn, did, d["revision"], "srv-counterparty", "counterparty", "missing",
                 "이 자료의 상대 업체(보낸 곳 또는 받는 곳)는 누구인가요?", list(dict.fromkeys(opts)), None, "server")
            return
    if pp.get("counterparty") and norm(pp["counterparty"]) in selfn:
        ext = json.loads(d["extracted"] or "{}")
        opts = [x for x in (pp.get("receiver"), ext.get("supplier"), ext.get("customer")) if x and norm(x) not in selfn]
        _ask(conn, did, d["revision"], "srv-counterparty", "counterparty", "conflict",
             f"상대방이 자사({pp['counterparty']})로 해석됐습니다. 이 자료의 상대 업체는 누구인가요?",
             list(dict.fromkeys(opts)), None, "server")
        return
    known = _kind_known(conn, d, pp, state)
    if not known and pp["action"] == "none":
        _ask(conn, did, d["revision"], "srv-kind", "kind", "unknown_meaning", "이 자료는 무엇인가요?",
             list(KIND_CHOICES), None, "server")
        return
    if not known:
        kind = ACTION_TO_KIND.get(pp["action"], "other")
        hyp = json.loads(d["hypothesis"] or "{}")
        why = {"case_inference": "과거 사례로 추정", "model_inference": "자료 해석으로 추정"}.get(pp["kind_source"], "추정")
        _ask(conn, did, d["revision"], "srv-kind-confirm", "kind", "confirm_inference",
             f"이 자료를 '{KIND_LABEL[kind]}'(으)로 볼까요? ({why}: {hyp.get('description', '')})",
             ["맞음", *KIND_CHOICES], kind, "server")
        return
    state["kind"] = known
    kind = known["value"]
    cp = state.get("counterparty")
    if not cp and pp.get("counterparty"):
        if norm(pp["counterparty"]) and norm(pp["counterparty"]) in norm(d["raw_input"]):
            cp = {"value": pp["counterparty"], "source": "document_name"}   # 모델이 읽은 이름이 원문에 그대로 있음
        elif kind in ("receipt", "shipment"):
            _set_state(conn, did, state)
            _ask(conn, did, d["revision"], "srv-counterparty", "counterparty", "confirm_inference",
                 f"상대 업체가 '{pp['counterparty']}'가 맞나요? (원문에 그대로 적혀 있지 않음)", ["맞음"],
                 pp["counterparty"], "server")
            return
    conf = {"kind": _ev(known), "counterparty": _ev(cp)}
    if kind == "receipt":
        for i, ln in enumerate(pp.get("lines") or []):
            # 칸별 확정: 수량 → 품목 → (임대품이면) 소유자. 각 칸은 그 칸을 확정할 수 있는 근거로만 채운다(SLOT_EVIDENCE)
            qk = _qty_known(d, ln, i, state)
            if not qk:
                _set_state(conn, did, state)
                q = ln.get("qty")
                _ask(conn, did, d["revision"], f"srv-qty-{i}", f"lines.{i}.qty", "confirm_inference" if q is not None else "missing",
                     f"'{ln['item_text']}' 수량이 {_fmt(q)}개가 맞나요? (원문에서 확인되지 않음)" if q is not None
                     else f"'{ln['item_text']}' 수량은 얼마인가요?", ["맞음"] if q is not None else [], _fmt(q) if q is not None else None, "server")
                return
            conf[f"lines.{i}.qty"] = _ev(qk)
            ik = _item_known(conn, ln, i, state, d["company_id"], pp.get("counterparty") or "")
            if not ik:
                _set_state(conn, did, state)
                opts = [f"{r['name']}" for r in conn.execute("SELECT name FROM item ORDER BY kind DESC, id")] + ["재고와 무관"]
                prop = ln.get("item_ref")
                _ask(conn, did, d["revision"], f"srv-item-{i}", f"lines.{i}.item", "confirm_inference" if prop else "missing",
                     f"'{ln['item_text']}'은(는) 어떤 품목인가요?" + (" (추정 제시)" if prop else ""), opts, prop, "server")
                return
            state.setdefault("items", {})[str(i)] = ik
            conf[f"lines.{i}.item"] = _ev(ik)
            ok = _owner_known(conn, d, pp, ln, i, state, ik)
            if ok is False:
                continue                  # 임대품 아님 → 소유자 칸 없음
            if not ok:
                _set_state(conn, did, state)
                item = conn.execute("SELECT * FROM item WHERE id=?", (int(ik["choice"].split(":")[1]),)).fetchone()
                mo = conn.execute("SELECT name FROM party WHERE id=?", (item["owner_party_id"],)).fetchone()
                opts = [x for x in (mo["name"] if mo else None, pp.get("counterparty")) if x] + ["자사"]
                _ask(conn, did, d["revision"], f"srv-owner-{i}", f"lines.{i}.owner", "missing",
                     f"{pp.get('counterparty')}에서 온 '{item['name']}' {_fmt(qk['value'])}개는 누구 소유인가요?",
                     list(dict.fromkeys(opts)), None, "server")
                return
            state.setdefault("owners", {})[str(i)] = ok
            conf[f"lines.{i}.owner"] = _ev(ok)
    state["confirmations"] = conf
    _set_state(conn, did, state)
    _apply(conn, did, pid, pp, state)


# 근거별 확정 범위 — 근거가 확정할 수 있는 칸(이 밖의 칸은 그 근거로 채우지 않는다)
SLOT_EVIDENCE = {
    "document": "원문 종류 표기 → 문서가 주장하는 이동 종류",
    "document_name": "원문에 적힌 알려진 업체명 → 상대 업체",
    "document_value": "원문에 적힌 숫자 → 그 줄의 수량",
    "master_exact": "기준정보 이름·코드·별칭 정확 일치 → 품목(수량·방향·소유자는 아님)",
    "sender_is_owner": "보낸 업체가 그 임대품의 등록 소유자 → 소유자",
    "user_answer": "이번 문답의 사용자 답 → 그 질문의 칸, 이 사건에 한함",
    "rbk": "승인된 활성 규칙 → 조건이 맞는 범위에서 규칙이 다루는 칸",
}


def _ev(s):
    if not s:
        return None
    v = s.get("value", s.get("choice"))
    out = {"value": v, "evidence": s.get("source")}
    if s.get("rule_version_id"):
        out["rule_version_id"] = s["rule_version_id"]
    if s.get("answer_id"):
        out["answer_id"] = s["answer_id"]
    return out


def _fmt(q):
    return None if q is None else (str(int(q)) if float(q).is_integer() else str(q))


def _qty_known(d, ln, i, state):
    s = (state.get("qty") or {}).get(str(i))
    if s:
        return s
    q = ln.get("qty")
    if q is None:
        return None
    digits = _fmt(q)
    raw = d["raw_input"].replace(",", "")
    if re.search(rf"(?<![\d.]){re.escape(digits)}(?![\d])", raw):
        return {"value": float(q), "source": "document_value"}
    return None   # 모델이 읽은 수가 원문에 없다 → 확인


def _owner_known(conn, d, pp, ln, i, state, ik):
    """임대품 소유자. False=임대품 아님(칸 없음), None=모름(질문), dict=확정."""
    if ik["choice"] in (NO_EFFECT_ITEM, None) or not str(ik["choice"]).startswith("item:"):
        return False
    item = conn.execute("SELECT * FROM item WHERE id=?", (int(ik["choice"].split(":")[1]),)).fetchone()
    if not item or item["kind"] != "rental":
        return False
    s = (state.get("owners") or {}).get(str(i))
    if s:
        return s
    facts = learn.facts_for("owner_map", supplier=pp.get("counterparty") or "", item_text=ln["item_text"])
    status, action, ver, basis = learn.decide(conn, event={"company_id": d["company_id"], "trade_date": None},
                                              decision="owner_map", facts=facts)
    if status == "rule":
        return {"value": action["owner"], "source": "rbk", "rule_version_id": ver["id"], "basis": basis}
    if item["owner_party_id"]:
        p = conn.execute("SELECT name, aliases FROM party WHERE id=?", (item["owner_party_id"],)).fetchone()
        names = {norm(x) for x in [p["name"], *json.loads(p["aliases"] or "[]")] if x}
        if norm(pp.get("counterparty") or "") in names:
            return {"value": f"party:{item['owner_party_id']}", "source": "sender_is_owner"}
    return None


def _is_name_answer(conn, q, choice, val, loose=True) -> bool:
    """이름(업체)으로 바로 받아도 되는 답인가: 버튼 선택, 제시한 선택지, 알려진 업체명·별칭, 또는 한 단어."""
    if choice or val in json.loads(q["options"] or "[]") or val == "자사":
        return True
    n = norm(val)
    for r in conn.execute("SELECT name, aliases FROM party"):
        if n in [norm(x) for x in [r["name"], *json.loads(r["aliases"] or "[]")] if x]:
            return True
    # 상대 업체 칸만: 공백 없는 짧은 이름(예: LOGISALL). 소유자 칸은 거래 상대를 새로 만들 수 있어 더 엄격하게
    return loose and bool(re.fullmatch(r"[\w().&-]{1,30}", val.strip()))


def _resolve_server_answer(conn, q, choice, text, state, aid) -> bool:
    """서버 확인 질문에 대한 선택·'맞음'을 결정적으로 반영. 처리했으면 True."""
    if q["origin"] != "server":
        return False
    val = (choice or text or "").strip()
    if q["target"] == "counterparty":
        if q["proposed"] and (YES.match(val) or YES_PREFIX.match(val)):
            val = q["proposed"]
        elif not _is_name_answer(conn, q, choice, val):
            return False   # 문장으로 답함 → 해석(API)
        if val:
            state["counterparty"] = {"value": val, "source": "user_answer", "answer_id": aid}
            return True
        return False
    if q["target"] == "kind":
        pick = KIND_CHOICES.get(val)
        if pick is None and (YES.match(val) or YES_PREFIX.match(val)) and q["proposed"]:
            pick = q["proposed"]
        if pick:
            state["kind"] = {"value": pick, "source": "user_answer", "answer_id": aid, "question_id": q["id"]}
            return True
        return False
    m = re.fullmatch(r"lines\.(\d+)\.item", q["target"])
    if m:
        idx = m[1]
        if val == "재고와 무관":
            state.setdefault("items", {})[idx] = {"choice": NO_EFFECT_ITEM, "source": "user_answer", "answer_id": aid}
            return True
        if (YES.match(val) or YES_PREFIX.match(val)) and q["proposed"]:
            state.setdefault("items", {})[idx] = {"choice": q["proposed"], "source": "user_answer", "answer_id": aid}
            return True
        r = conn.execute("SELECT id FROM item WHERE name=?", (val,)).fetchone()
        if r:
            state.setdefault("items", {})[idx] = {"choice": f"item:{r['id']}", "source": "user_answer", "answer_id": aid}
            return True
        return False
    m = re.fullmatch(r"lines\.(\d+)\.owner", q["target"])
    if m and val and not _is_name_answer(conn, q, choice, val, loose=False):
        return False   # 문장으로 답함 → 이름으로 저장하지 않고 해석(API)에 넘긴다
    if m and val:
        if val == "자사":
            owner, label = "own", "자사"
        else:
            r = conn.execute("SELECT id, name FROM party WHERE name=?", (val,)).fetchone() or next(
                (x for x in conn.execute("SELECT id, name, aliases FROM party")
                 if norm(val) in [norm(y) for y in [x["name"], *json.loads(x["aliases"] or "[]")]]), None)
            if r is None:   # 처음 보는 소유 업체: 사용자 답을 근거로 거래 상대 기준정보에 추가(기록 남김)
                pid_ = conn.execute("INSERT INTO party(name, kind, aliases, is_test) VALUES (?, 'lessor', '[]', 0)",
                                    (val,)).lastrowid
                process.log(conn, None, "master", f"거래 상대 추가: {val} (문답 답변 #{aid})")
                r = {"id": pid_, "name": val}
            owner, label = f"party:{r['id']}", r["name"]
        state.setdefault("owners", {})[m[1]] = {"value": owner, "label": label, "source": "user_answer", "answer_id": aid}
        return True
    m = re.fullmatch(r"lines\.(\d+)\.qty", q["target"])
    if m and q["proposed"] and (YES.match(val) or YES_PREFIX.match(val)):
        state.setdefault("qty", {})[m[1]] = {"value": float(q["proposed"]), "source": "user_answer", "answer_id": aid}
        return True
    if m and re.fullmatch(r"\d+(\.\d+)?", val.replace(",", "")):
        state.setdefault("qty", {})[m[1]] = {"value": float(val.replace(",", "")), "source": "user_answer", "answer_id": aid}
        return True
    return False


def _continue_after_server_answer(conn, did) -> dict:
    with Tx(conn):
        p = conn.execute("SELECT id, content FROM dialog_proposal WHERE dialog_id=? AND status='proposed' ORDER BY id DESC"
                         " LIMIT 1", (did,)).fetchone()
        if p:
            st = _state(_dialog(conn, did))
            pp = json.loads(p["content"])
            for i, q in (st.get("qty") or {}).items():   # 사용자가 답한 수량을 처리안에 반영
                pp["lines"][int(i)]["qty"] = q["value"]
            conn.execute("UPDATE dialog_proposal SET content=? WHERE id=?", (jdump(pp), p["id"]))
            _gate_and_apply(conn, did, p["id"])
    return detail(conn, did)


# --------------------------------------------------------------------------- 반영 (기존 엔진)
def _apply(conn, did, pid, pp, state):
    d = _dialog(conn, did)
    kind = state["kind"]["value"]
    doc_type = KIND_TO_DOC[kind]
    raw = d["raw_input"]
    try:
        js = json.loads(raw)
        base = js if isinstance(js, dict) else None
    except json.JSONDecodeError:
        base = None
    if base is None:
        try:
            base, _ = textparse.to_payload(raw, process.customer_names(conn), doc_type=doc_type)
        except ValueError:
            base = {"channel": "manual", "raw_text": raw}
    p = dict(base)
    p.update(doc_type=doc_type, input_id=f"dialog-{did}", source_ref=f"dialog:{did}", company_id=d["company_id"])
    p.setdefault("channel", "manual")
    for k_src, k_dst in (("doc_no", "doc_no"), ("trade_date", "trade_date"), ("vehicle_no", "vehicle_no"),
                         ("driver_phone", "driver_phone"), ("site", "site")):
        if pp.get(k_src) is not None:
            p[k_dst] = pp[k_src]
    if kind == "shipment":
        if pp.get("counterparty"):
            p["customer"] = pp["counterparty"]
        c = dict(p.get("contact") or {})
        if pp.get("contact_name"):
            c["name"] = pp["contact_name"]
        if pp.get("contact_phone"):
            c["phone"] = pp["contact_phone"]
        if c:
            p["contact"] = c
        if pp.get("pallets_invoice") is not None or pp.get("pallets_message") is not None:
            p["pallets"] = {**(p.get("pallets") or {}), "invoice": pp.get("pallets_invoice"), "message": pp.get("pallets_message")}
    elif kind == "receipt":
        p["supplier"] = pp.get("counterparty")
        p["receiver"] = pp.get("receiver")
        p.pop("customer", None)
    lines, meta = [], dict(p.get("meta") or {})
    for i, ln in enumerate(pp.get("lines") or []):
        name = ln["item_text"]
        s = (state.get("items") or {}).get(str(i))
        if s and s["choice"] not in (NO_EFFECT_ITEM, None):
            r = conn.execute("SELECT name FROM item WHERE id=?", (int(s["choice"].split(":")[1]),)).fetchone()
            if r:
                meta[f"lines.{len(lines)}.item"] = {"raw": ln["item_text"]}
                name = r["name"]
        elif s and s["choice"] == NO_EFFECT_ITEM:
            continue
        o = (state.get("owners") or {}).get(str(i))
        lines.append({"item": name, "qty": ln.get("qty"), "unit": ln.get("unit"),
                      **({"owner": o["value"]} if o else {})})
    if pp.get("lines"):
        p["lines"] = lines
    if meta:
        p["meta"] = meta
    p.pop("doc_type_guess", None)
    p.pop("form_key", None)
    try:
        contract.normalize(p)
    except contract.ContractError as e:
        conn.execute("UPDATE dialog_proposal SET status='rejected', validation=? WHERE id=?",
                     (jdump({"errors": e.errors}), pid))
        conn.execute("UPDATE dialog SET status='failed', status_reason=?, updated_at=? WHERE id=?",
                     (f"처리안 검증 실패: {'; '.join(e.errors)}", now(), did))
        return
    res = process._intake_tx(conn, p)
    ev = res["event_id"]
    conn.execute("UPDATE dialog_proposal SET status='applied', event_id=?, validation=? WHERE id=?",
                 (ev, jdump({"payload": p, "outcome": res["outcome"],
                             "confirmations": state.get("confirmations") or {}}), pid))
    status = "applied"
    if kind == "return":
        # 반납·회수: 처리 기준(누구 소유가 어디로)이 정해지지 않아 장부는 바꾸지 않는다. 완료로 보이지 않게 미결로 남긴다
        status = "pending_ledger"
        items = ", ".join(f"{ln['item_text']} {_fmt(ln.get('qty')) or '?'}" for ln in pp.get("lines") or [])
        conn.execute("INSERT INTO obligation(event_id, kind, state, note, updated_at) VALUES (?, 'ledger_pending', 'open', ?, ?)",
                     (ev, f"반납 · {pp.get('counterparty') or '상대 미상'} · {items} — 반납 처리 기준 미정, 장부 미반영", now()))
    elif doc_type == "other":
        status = "kept"
    conn.execute("UPDATE dialog SET status=?, event_id=?, updated_at=? WHERE id=?", (status, ev, now(), did))
    # 학습 기록: 확정 사례 (사건의 사실. 자동 일반 규칙 아님)
    sup = norm(pp.get("counterparty") or "")
    k = state["kind"]
    label = f"{pp.get('counterparty') or '상대 미상'} 자료의 종류"
    dkey = learn.case_key(learn.facts_for("doc_kind", supplier=pp.get("counterparty") or "", raw=raw))
    cid = None
    if k["source"] == "rbk":
        learn.record_application(conn, event_id=ev, version_id=k["rule_version_id"], target="doc_kind",
                                 basis={**(k.get("basis") or {}), "dialog": did}, snapshot={"raw_head": raw[:200]},
                                 result="performed", reason=KIND_LABEL[kind])
        cid = learn.record_case(conn, event_id=ev, check_id=None, kind="doc_kind", key=dkey, label=label,
                                answer=KIND_TO_DOC[kind], actor="system", version_id=k["rule_version_id"],
                                company_id=d["company_id"])
    elif k["source"] == "user_answer":   # 사용자가 판단한 것만 사례(학습 재료)로 남긴다. 원문 표기로 정해진 것은 사례 아님
        cid = learn.record_case(conn, event_id=ev, check_id=None, kind="doc_kind", key=dkey, label=label,
                                answer=KIND_TO_DOC[kind], actor=d["created_by"], company_id=d["company_id"])
    if cid:
        conn.execute("UPDATE decision_case SET dialog_id=? WHERE id=?", (did, cid))
    for i, s in (state.get("items") or {}).items():
        if s.get("source") == "rbk":
            learn.record_application(conn, event_id=ev, version_id=s["rule_version_id"], target=f"lines.{i}.item",
                                     basis={**(s.get("basis") or {}), "dialog": did},
                                     snapshot={"item": pp["lines"][int(i)]["item_text"]},
                                     result="performed", reason=s["choice"])
        if s.get("source") == "user_answer":
            ln = pp["lines"][int(i)]
            r_ = conn.execute("SELECT name FROM item WHERE id=?", (int(s["choice"].split(":")[1]),)).fetchone() \
                if str(s["choice"]).startswith("item:") else None
            lab = r_["name"] if r_ else "재고와 무관"
            c2 = learn.record_case(conn, event_id=ev, check_id=None, kind="item_map",
                                   key=learn.case_key(learn.facts_for("item_map", supplier=pp.get("counterparty") or "",
                                                                      item_text=ln["item_text"])),
                                   label=f"{pp.get('counterparty') or ''} 자료의 품목 '{ln['item_text']}'",
                                   answer={"choice": s["choice"], "label": lab}, actor=d["created_by"],
                                   company_id=d["company_id"])
            conn.execute("UPDATE decision_case SET dialog_id=? WHERE id=?", (did, c2))
    for i, s in (state.get("owners") or {}).items():
        ln = pp["lines"][int(i)]
        f = learn.facts_for("owner_map", supplier=pp.get("counterparty") or "", item_text=ln["item_text"])
        if s.get("source") == "rbk":
            learn.record_application(conn, event_id=ev, version_id=s["rule_version_id"], target=f"lines.{i}.owner",
                                     basis={**(s.get("basis") or {}), "dialog": did}, snapshot={"item": ln["item_text"]},
                                     result="performed", reason=s["value"])
        if s.get("source") == "user_answer":
            c3 = learn.record_case(conn, event_id=ev, check_id=None, kind="owner_map", key=learn.case_key(f),
                                   label=f"{pp.get('counterparty') or ''} 자료 '{ln['item_text']}'의 소유자",
                                   answer={"owner": s["value"], "label": s.get("label")}, actor=d["created_by"],
                                   company_id=d["company_id"])
            conn.execute("UPDATE decision_case SET dialog_id=? WHERE id=?", (did, c3))
    process.log(conn, ev, "dialog", f"문답 #{did}로 처리 · {KIND_LABEL[kind]}"
                + (" · 규칙 적용" if k["source"] == "rbk" else ""))


# --------------------------------------------------------------------------- 학습 후보 수락
def accept_candidate(conn, *, candidate_id: int, actor: str) -> dict:
    u = learn.user(conn, actor)
    with Tx(conn):
        c = conn.execute("SELECT * FROM learning_candidate WHERE id=?", (candidate_id,)).fetchone()
        if not c or c["status"] != "suggested":
            raise Conflict("수락할 수 있는 후보가 아닙니다")
        d = _dialog(conn, c["dialog_id"])
        result = {}
        if c["kind"] == "alias":
            # 기준정보 별칭 추가: 조건 없는 사실이므로 업체 범위 규칙이 아니라 기준정보를 고친다(승인권자만, 기록 남김)
            if u["role"] not in learn.ROLE_CAN_APPROVE:
                raise Forbidden("기준정보 변경은 승인권자만 할 수 있습니다")
            pl = json.loads(c["payload"] or "{}")
            iid = int(pl["target_ref"].split(":")[1])
            it = conn.execute("SELECT * FROM item WHERE id=?", (iid,)).fetchone()
            if not it:
                raise Conflict("없는 품목입니다")
            aliases = json.loads(it["aliases"] or "[]")
            if norm(pl["alias"]) not in [norm(x) for x in [it["name"], it["code"] or "", *aliases] if x]:
                aliases.append(pl["alias"])
                conn.execute("UPDATE item SET aliases=? WHERE id=?", (jdump(aliases), iid))
            ans = conn.execute("SELECT raw_text FROM dialog_answer WHERE id=?", (c["answer_id"],)).fetchone()
            process.log(conn, d["event_id"], "master",
                        f"기준정보 별칭 추가: '{pl['alias']}' → {it['name']} (문답 #{d['id']} 답변: {ans['raw_text'] if ans else ''})")
            result, linked = {"item_id": iid, "alias": pl["alias"]}, f"item:{iid}#alias"
            conn.execute("UPDATE learning_candidate SET status='accepted', linked=? WHERE id=?", (linked, candidate_id))
            return result
        if c["kind"] == "rbk":
            if not c["explicit_instruction"]:
                raise Conflict("사용자의 명시적 일반화 지시가 없는 규칙 후보는 LAG 로 등록해 검증하세요")
            st = _state(d)
            if "kind" not in st or not d["event_id"]:
                raise Conflict("이 자료의 종류가 확정·반영된 뒤에 규칙으로 등록할 수 있습니다")
            pp = json.loads(conn.execute("SELECT content FROM dialog_proposal WHERE dialog_id=? AND status='applied'"
                                         " ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()[0])
            sup = norm(pp.get("counterparty") or "")
            if not sup:
                raise Conflict("보낸 업체가 확정되지 않아 규칙 범위를 정할 수 없습니다")
            ans = conn.execute("SELECT * FROM dialog_answer WHERE id=?", (c["answer_id"],)).fetchone()
            made = []
            item_lines = [(i, ln) for i, ln in enumerate(pp.get("lines") or [])
                          if ln["item_text"] and ln["item_text"] in c["text"] and str(i) in (st.get("items") or {})]
            if item_lines:   # 후보 문장이 확정된 품목을 가리키면 품목 연결 규칙
                for i, ln in item_lines:
                    ch = st["items"][str(i)]["choice"]
                    nm = conn.execute("SELECT name FROM item WHERE id=?", (int(ch.split(":")[1]),)).fetchone()["name"] \
                        if str(ch).startswith("item:") else "재고와 무관"
                    made.append(learn.directive_rule(
                        conn, actor=actor, company_id=d["company_id"], decision="item_map",
                        condition=learn.facts_for("item_map", supplier=pp.get("counterparty") or "",
                                                  item_text=ln["item_text"]),
                        action={"type": "map_item", "choice": ch, "label": nm},
                        title=f"{pp.get('counterparty')} 자료의 '{ln['item_text']}' → {nm}",
                        instruction=ans["raw_text"], answer_id=ans["id"], event_id=d["event_id"]))
            else:            # 그 밖에는 자료 종류(방향) 규칙
                made.append(learn.directive_rule(
                    conn, actor=actor, company_id=d["company_id"], decision="doc_kind",
                    condition=learn.facts_for("doc_kind", supplier=pp.get("counterparty") or "", raw=d["raw_input"]),
                    action={"type": "set_doc_kind", "value": KIND_TO_DOC[st["kind"]["value"]]},
                    title=f"{pp.get('counterparty')} 자료 → {KIND_LABEL[st['kind']['value']]}",
                    instruction=ans["raw_text"], answer_id=ans["id"], event_id=d["event_id"]))
            result = made[0] if len(made) == 1 else {"rules": made, "rule_version_id": made[0]["rule_version_id"]}
            linked = f"rule_version:{result['rule_version_id']}"
        elif c["kind"] == "lag":
            lid = learn.create_lag(conn, actor=actor, question=c["text"], context={"dialog": d["id"], "scope": c["scope"]},
                                   event_ids=[d["event_id"]] if d["event_id"] else [], company_id=d["company_id"])
            result, linked = {"lag_id": lid}, f"lag:{lid}"
        else:
            from . import plan
            pid = plan.create(conn, actor=actor, title=c["text"][:80], purpose=c["scope"] or None,
                              links=[{"role": "target", "kind": "event", "id": d["event_id"]}] if d["event_id"] else [],
                              company_id=d["company_id"])
            result, linked = {"plan_id": pid}, f"plan:{pid}"
        conn.execute("UPDATE learning_candidate SET status='accepted', linked=? WHERE id=?", (linked, candidate_id))
    return result


def dismiss_candidate(conn, *, candidate_id: int, actor: str):
    learn.user(conn, actor)
    with Tx(conn):
        conn.execute("UPDATE learning_candidate SET status='dismissed' WHERE id=? AND status='suggested'", (candidate_id,))


def list_dialogs(conn, company_id=1, limit=30):
    return [dict(r) for r in conn.execute(
        "SELECT id, status, event_id, substr(raw_input,1,60) head, status_reason, updated_at FROM dialog WHERE company_id=?"
        " ORDER BY id DESC LIMIT ?", (company_id, limit))]


def metrics(conn, company_id=1) -> dict:
    """DEV-004 측정: 문답당 질문 수·API 호출·지연·토큰, 규칙이 처리한 판단 수."""
    ds = conn.execute("SELECT id, status FROM dialog WHERE company_id=?", (company_id,)).fetchall()
    qs = conn.execute("SELECT COUNT(*) FROM dialog_question q JOIN dialog d ON d.id=q.dialog_id WHERE d.company_id=?"
                      " AND q.status<>'superseded'", (company_id,)).fetchone()[0]
    calls = conn.execute("SELECT l.status, l.latency_ms, l.usage FROM llm_call l JOIN dialog d ON d.id=l.dialog_id"
                         " WHERE d.company_id=?", (company_id,)).fetchall()
    toks = sum((json.loads(c["usage"]) or {}).get("total_tokens", 0) for c in calls if c["usage"])
    lat = [c["latency_ms"] for c in calls if c["latency_ms"] is not None]
    auto = conn.execute("SELECT COUNT(*) FROM rule_application a JOIN event e ON e.id=a.event_id WHERE e.company_id=?"
                        " AND (a.target='doc_kind' OR a.target LIKE 'lines.%')", (company_id,)).fetchone()[0]
    return {"dialogs": len(ds), "by_status": {s: sum(1 for d in ds if d["status"] == s) for s in {d["status"] for d in ds}},
            "questions": qs, "questions_per_dialog": round(qs / len(ds), 2) if ds else 0,
            "api_calls": len(calls), "api_failed": sum(1 for c in calls if c["status"] != "ok"),
            "tokens": toks, "latency_avg_ms": int(sum(lat) / len(lat)) if lat else None,
            "latency_max_ms": max(lat) if lat else None, "rule_decisions": auto}
