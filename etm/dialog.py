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
MARKER_WORDS = ("출하", "송장", "입고", "반납", "회수", "반품", "명세서")
MARKER_KIND = {"출하": "shipment", "송장": "shipment", "입고": "receipt", "반납": "return", "회수": "return", "반품": "return"}


def doc_marker(raw: str) -> str:
    """원문에 나타난 문서 종류 표지. 규칙 조건과 충돌 검사에 쓴다(보낸 업체 하나만으로 일치시키지 않기 위해)."""
    found = sorted({w for w in MARKER_WORDS if w in (raw or "")})
    return "·".join(found) or "없음"


def _marker_conflicts(raw: str, kind: str) -> list[str]:
    return sorted({w for w, k in MARKER_KIND.items() if w in (raw or "") and k != kind})


# --------------------------------------------------------------------------- 조회
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
        key_parts = [p for p in re.split(r"[|·]", r["decision_key"]) if p]
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
    learn.user(conn, actor)
    if not (text or "").strip():
        raise ValueError("입력 내용이 비어 있습니다")
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
    learn.user(conn, actor)
    d = _dialog(conn, dialog_id)
    if d["status"] not in ("failed", "asking"):
        raise Conflict("다시 해석할 수 있는 상태가 아닙니다")
    return run_turn(conn, dialog_id)


# --------------------------------------------------------------------------- API 턴
def run_turn(conn, did) -> dict:
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
    for attempt in (1, 2):
        try:
            out = llm.call(conn, dialog_id=did, revision=rev, messages=messages, evidence=evidence)
        except llm.LLMError as e:
            with Tx(conn):
                conn.execute("UPDATE dialog SET status='failed', status_reason=?, updated_at=? WHERE id=?",
                             (f"API 실패 — 반영 보류, 원문·답변 보존: {e}", now(), did))
            return detail(conn, did)
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
    with Tx(conn):
        d = _dialog(conn, did)
        if d["revision"] != rev:   # 새 답변으로 상태가 바뀐 뒤 늦게 온 응답은 버린다
            conn.execute("UPDATE llm_call SET status='stale' WHERE request_id=?", (out["request_id"],))
            return detail(conn, did)
        _process_response(conn, d, out["response"], out["request_id"])
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
        conn.execute("INSERT INTO learning_candidate(dialog_id, kind, text, scope, explicit_instruction, answer_id, status,"
                     " created_at) VALUES (?,?,?,?,?,?, 'suggested', ?)",
                     (did, c["kind"], c["text"], c.get("scope") or "", int(explicit), last_a["id"] if explicit else None, now()))
    pp = resp["processing_proposal"]
    # 사용자 답으로 종류가 확정된 경우(API 해석) — 원문 답변이 있어야만 인정
    kind = ACTION_TO_KIND.get(pp["action"])
    hk = (resp.get("document_hypothesis") or {}).get("kind")
    if pp["kind_source"] == "user_answer" and hk in KIND_TO_DOC and hk != kind:
        # 사용자 답의 해석(가설)과 처리 동작이 어긋나면 가설을 따른다. 반납은 처리 엔진이 없으므로 보관만.
        kind = hk
        pp = dict(pp, action={"shipment": "shipment_apply", "receipt": "receipt_record"}.get(hk, "keep_only"))
    if kind and pp["kind_source"] == "user_answer" and last_a is not None and "kind" not in state:
        state["kind"] = {"value": kind, "source": "user_answer", "answer_id": last_a["id"], "via": "api_interpretation"}
    if last_a is not None:
        for i, ln in enumerate(pp.get("lines") or []):
            ref = ln.get("item_ref")
            if ln.get("item_source") == "user_answer" and ref and str(i) not in state.get("items", {}):
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
    kind = ACTION_TO_KIND.get(pp["action"])
    if not kind:
        return None
    src = pp["kind_source"]
    if _marker_conflicts(d["raw_input"], kind):   # 원문 표지와 다른 방향은 확인 없이 확정하지 않는다
        return None
    if src == "document" and any(w in d["raw_input"] for w in KIND_WORDS.get(kind, ())):
        return {"value": kind, "source": "document"}
    if src == "rbk":
        sup = norm(pp.get("counterparty") or "")
        for ref in pp.get("kind_refs") or []:
            m = re.fullmatch(r"rule:(\d+)", ref)
            if not m:
                continue
            v = conn.execute("SELECT v.*, r.decision, r.company_id FROM rule_version v JOIN rule r ON r.id=v.rule_id"
                             " WHERE v.id=? AND v.status='active'", (int(m[1]),)).fetchone()
            if not v or v["decision"] != "doc_kind" or v["company_id"] not in (None, d["company_id"]):
                continue
            cond, act = json.loads(v["condition"]), json.loads(v["action"])
            if (cond.get("supplier") and cond["supplier"] == sup and cond.get("marker") == doc_marker(d["raw_input"])
                    and KIND_TO_DOC.get(kind) == act.get("value") and not _marker_conflicts(d["raw_input"], kind)):
                return {"value": kind, "source": "rbk", "rule_version_id": v["id"]}
    return None


def _item_known(conn, ln, idx, state, company=1, supplier=""):
    s = state.get("items", {}).get(str(idx))
    if s:
        return s
    facts = {"supplier": norm(supplier), "item_text": norm(ln["item_text"])}
    for v in conn.execute("SELECT v.* FROM rule_version v JOIN rule r ON r.id=v.rule_id WHERE v.status='active' AND"
                          " r.decision='item_map' AND (r.company_id=? OR r.company_id IS NULL)", (company,)):
        if json.loads(v["condition"]) == facts:
            act = json.loads(v["action"])
            return {"choice": act["choice"], "source": "rbk", "rule_version_id": v["id"]}
    ref = ln.get("item_ref")
    if ref and ln.get("item_source") == "master_exact":
        m = re.fullmatch(r"item:(\d+)", ref)
        r = conn.execute("SELECT * FROM item WHERE id=?", (int(m[1]),)).fetchone() if m else None
        if r and norm(ln["item_text"]) in [norm(x) for x in (r["name"], r["code"] or "", *json.loads(r["aliases"] or "[]")) if x]:
            return {"choice": ref, "source": "master_exact"}
    return None


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
    if pp.get("counterparty") and norm(pp["counterparty"]) in selfn:
        ext = json.loads(d["extracted"] or "{}")
        opts = [x for x in (pp.get("receiver"), ext.get("supplier"), ext.get("customer")) if x and norm(x) not in selfn]
        _ask(conn, did, d["revision"], "srv-counterparty", "counterparty", "conflict",
             f"상대방이 자사({pp['counterparty']})로 해석됐습니다. 이 자료의 상대 업체는 누구인가요?",
             list(dict.fromkeys(opts)), None, "server")
        return
    if pp["action"] == "none" and "kind" not in state:
        hyp = json.loads(d["hypothesis"] or "{}")
        _ask(conn, did, d["revision"], "srv-kind", "kind", "unknown_meaning", "이 자료는 무엇인가요?",
             list(KIND_CHOICES), None, "server")
        return
    known = _kind_known(conn, d, pp, state)
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
    if kind == "receipt":
        for i, ln in enumerate(pp.get("lines") or []):
            if ln.get("qty") is None:
                _set_state(conn, did, state)
                _ask(conn, did, d["revision"], f"srv-qty-{i}", f"lines.{i}.qty", "missing",
                     f"'{ln['item_text']}' 수량은 얼마인가요?", [], None, "server")
                return
            ik = _item_known(conn, ln, i, state, d["company_id"], pp.get("counterparty") or "")
            if not ik:
                _set_state(conn, did, state)
                opts = [f"{r['name']}" for r in conn.execute("SELECT name FROM item ORDER BY kind DESC, id")] + ["재고와 무관"]
                prop = ln.get("item_ref")
                _ask(conn, did, d["revision"], f"srv-item-{i}", f"lines.{i}.item", "confirm_inference" if prop else "missing",
                     f"'{ln['item_text']}'은(는) 어떤 품목인가요?" + (" (추정 제시)" if prop else ""), opts, prop, "server")
                return
            state.setdefault("items", {})[str(i)] = ik
    _set_state(conn, did, state)
    _apply(conn, did, pid, pp, state)


def _resolve_server_answer(conn, q, choice, text, state, aid) -> bool:
    """서버 확인 질문에 대한 선택·'맞음'을 결정적으로 반영. 처리했으면 True."""
    if q["origin"] != "server":
        return False
    val = (choice or text or "").strip()
    if q["target"] == "counterparty":
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
    m = re.fullmatch(r"lines\.(\d+)\.qty", q["target"])
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
        lines.append({"item": name, "qty": ln.get("qty"), "unit": ln.get("unit")})
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
                 (ev, jdump({"payload": p, "outcome": res["outcome"]}), pid))
    conn.execute("UPDATE dialog SET status=?, event_id=?, updated_at=? WHERE id=?",
                 ("kept" if doc_type == "other" else "applied", ev, now(), did))
    # 학습 기록: 확정 사례 (사건의 사실. 자동 일반 규칙 아님)
    sup = norm(pp.get("counterparty") or "")
    k = state["kind"]
    label = f"{pp.get('counterparty') or '상대 미상'} 자료의 종류"
    if k["source"] == "rbk":
        learn.record_application(conn, event_id=ev, version_id=k["rule_version_id"], target="doc_kind",
                                 basis={"supplier": sup, "dialog": did}, snapshot={"raw_head": d["raw_input"][:200]},
                                 result="performed", reason=KIND_LABEL[kind])
        cid = learn.record_case(conn, event_id=ev, check_id=None, kind="doc_kind", key=sup or "unknown", label=label,
                                answer=KIND_TO_DOC[kind], actor="system", version_id=k["rule_version_id"],
                                company_id=d["company_id"])
    else:
        cid = learn.record_case(conn, event_id=ev, check_id=None, kind="doc_kind", key=sup or "unknown", label=label,
                                answer=KIND_TO_DOC[kind], actor=d["created_by"], company_id=d["company_id"])
    conn.execute("UPDATE decision_case SET dialog_id=? WHERE id=?", (did, cid))
    for i, s in (state.get("items") or {}).items():
        if s.get("source") == "rbk":
            learn.record_application(conn, event_id=ev, version_id=s["rule_version_id"], target=f"lines.{i}.item",
                                     basis={"supplier": sup, "dialog": did}, snapshot={"item": pp["lines"][int(i)]["item_text"]},
                                     result="performed", reason=s["choice"])
        if s.get("source") == "user_answer":
            ln = pp["lines"][int(i)]
            r_ = conn.execute("SELECT name FROM item WHERE id=?", (int(s["choice"].split(":")[1]),)).fetchone() \
                if str(s["choice"]).startswith("item:") else None
            lab = r_["name"] if r_ else "재고와 무관"
            c2 = learn.record_case(conn, event_id=ev, check_id=None, kind="item_map", key=f"{sup}|{norm(ln['item_text'])}",
                                   label=f"{pp.get('counterparty') or ''} 자료의 품목 '{ln['item_text']}'",
                                   answer={"choice": s["choice"], "label": lab}, actor=d["created_by"],
                                   company_id=d["company_id"])
            conn.execute("UPDATE decision_case SET dialog_id=? WHERE id=?", (did, c2))
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
                        condition={"supplier": sup, "item_text": norm(ln["item_text"])},
                        action={"type": "map_item", "choice": ch, "label": nm},
                        title=f"{pp.get('counterparty')} 자료의 '{ln['item_text']}' → {nm}",
                        instruction=ans["raw_text"], answer_id=ans["id"], event_id=d["event_id"]))
            else:            # 그 밖에는 자료 종류(방향) 규칙
                made.append(learn.directive_rule(
                    conn, actor=actor, company_id=d["company_id"], decision="doc_kind",
                    condition={"supplier": sup, "marker": doc_marker(d["raw_input"])},
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
