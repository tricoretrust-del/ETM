"""운영 RBK·LAG — 판단 위임과 학습 메커니즘 (DEV-002).

학습 경로
  사건·증빙 → 사용자 확인·수정(decision_case / confirmed_case) → 사례 보존
  → (사용자가 등록) LAG → 검증 기록 → 사용자 판정
  → 규칙 신설·새 버전(승인 대기) 또는 기각·보류 → 권한자 승인 → 활성
  → 후속 사건에 적용(rule_application) → 결과를 다시 관찰

원칙
- 프로그램은 활성 규칙 버전이 없으면 판단하지 않고 사용자에게 묻는다.
- 같은 답이 쌓이면 '후보'를 화면에 제시만 한다. LAG·규칙은 자동으로 만들지 않는다.
- 규칙 버전 내용은 바꿀 수 없다. 수정은 새 버전. 승인은 정확한 버전만, 오래된 버전 승인은 거부.
- 실행 동작은 ACTIONS 허용 목록과 형식 검증을 통과한 JSON 만. 자연어는 실행하지 않는다.
- 같은 대상에 서로 다른 동작의 활성 규칙이 겹치면 해당 처리만 보류하고 이유를 남긴다(우선순위 정책 미정).
- 규칙 승인만으로 재고·돈을 바꾸지 않는다. 반영은 사건 처리 절차가 한다. 과거 사건은 재계산하지 않는다.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

CANDIDATE_AFTER = 3
# ---------------------------------------------------------------------------- 판단 조건(사실) — 단일 정의
# 사례 저장·LAG 판정 초안·사용자 지시 규칙·규칙 적용(문답/기존 접수) 이 모두 이 함수로 조건을 만든다.
# 같은 판단이면 어느 경로로 만든 규칙이든 같은 모양의 조건이 되어 같은 절차(decide)로 적용된다.
MARKER_WORDS = ("출하", "송장", "입고", "반납", "회수", "반품", "명세서")
MARKER_KIND = {"출하": "shipment", "송장": "shipment", "입고": "receipt", "반납": "return", "회수": "return", "반품": "return"}


def _norm(s):
    from .process import norm
    return norm(s or "")


def doc_marker(raw: str) -> str:
    """원문에 나타난 문서 종류 표지(예: '입고', '반납', '출하·송장'). 없으면 '없음'."""
    return "·".join(sorted({w for w in MARKER_WORDS if w in (raw or "")})) or "없음"


def facts_for(decision: str, *, supplier: str = "", item_text: str = "", raw: str = "") -> dict:
    """규칙 조건·사례 키로 쓰는 표준 사실. doc_kind = {상대 업체, 원문 표지}, item_map = {상대 업체, 품목 원문}."""
    if decision == "doc_kind":
        return {"supplier": _norm(supplier), "marker": doc_marker(raw)}
    if decision == "item_map":
        return {"supplier": _norm(supplier), "item_text": _norm(item_text)}
    raise ValueError(f"사실을 만들 수 없는 판단 종류: {decision}")


DOC_TO_KIND = {"shipment_invoice": "shipment", "receipt": "receipt", "other": "other"}


def marker_conflicts(raw: str, kind_or_doc: str | None) -> list[str]:
    """원문 표지 중 주어진 방향과 다른 것. kind 는 shipment/receipt/return/other 또는 문서 종류값."""
    kind = DOC_TO_KIND.get(kind_or_doc, kind_or_doc)
    if kind == "other":   # 보관만은 장부를 바꾸지 않으므로 어느 표지와도 충돌하지 않는다
        return []
    return sorted({w for w, k in MARKER_KIND.items() if w in (raw or "") and k != kind})


def case_key(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False, sort_keys=True)


def facts_from_key(kind, key):
    """사례 키 → 조건 사실. 표준 키는 JSON. 0007 이전 키는 호환 해석(양식 키 / '업체|품목')."""
    if key and key.startswith("{"):
        return json.loads(key)
    if kind == "doc_kind":
        return {"form_key": key}
    if kind == "item_map":
        sup, _, item = (key or "").partition("|")
        return {"supplier": sup, "item_text": item}
    return {}


def require_specific(decision: str, condition: dict) -> None:
    """범위가 사실상 '모든 자료'가 되는 규칙은 만들지 않는다."""
    if decision == "doc_kind" and not (condition.get("supplier") or condition.get("form_key")):
        raise Conflict("상대 업체가 확정되지 않은 사례라 규칙 범위를 정할 수 없습니다")
    if decision == "item_map" and not condition.get("item_text"):
        raise Conflict("품목 원문이 없어 규칙 범위를 정할 수 없습니다")  # 후보 제시 기준(잠정). 승격 기준 자체는 LAG-001 / TRNK 결정 사항

DECISIONS = {
    "doc_kind": {"label": "자료 종류(가치 이동 방향)", "event_kind": "intake"},
    "item_map": {"label": "입고 품목 연결", "event_kind": "receipt"},
    "pallet_check": {"label": "출하 팔레트 대조", "event_kind": "shipment"},
}
SCOPE_KEYS = ("party_id", "site_id", "item_id")
ROLE_CAN_APPROVE = {"approver", "admin"}


class Forbidden(PermissionError):
    pass


class Conflict(ValueError):
    pass


# --------------------------------------------------------------------------- 공통
def _now():
    from .process import now
    return now()


def _j(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def user(conn, actor: str):
    u = conn.execute("SELECT * FROM app_user WHERE id=?", (actor,)).fetchone()
    if not u:
        raise Forbidden(f"알 수 없는 사용자: {actor}")
    return u


def idempotent(conn, request_id: str | None, command: str, fn):
    """같은 request_id 로 다시 오면 처음 결과를 그대로 돌려준다(중복 효과 없음)."""
    if request_id:
        r = conn.execute("SELECT command, result FROM command_log WHERE request_id=?", (request_id,)).fetchone()
        if r:
            if r["command"] != command:
                raise Conflict("같은 요청 ID 가 다른 명령에 이미 쓰였습니다")
            return json.loads(r["result"])
    result = fn()
    if request_id:
        conn.execute("INSERT INTO command_log(request_id, command, result, at) VALUES (?,?,?,?)",
                     (request_id, command, _j(result), _now()))
    return result


# --------------------------------------------------------------------------- 실행 동작 허용 목록
def validate_action(conn, decision: str, action: dict) -> dict:
    if not isinstance(action, dict):
        raise ValueError("동작은 JSON 객체여야 합니다")
    t = action.get("type")
    if decision == "pallet_check" and t == "compare_pallets":
        if action.get("on_match") not in ("accept", "require_confirm") or action.get("on_mismatch") != "require_confirm":
            raise ValueError("compare_pallets: on_match=accept|require_confirm, on_mismatch=require_confirm")
        return {"type": t, "on_match": action["on_match"], "on_mismatch": "require_confirm"}
    if decision == "doc_kind" and t == "set_doc_kind":
        from .contract import DOC_KIND_CHOICES
        if action.get("value") not in DOC_KIND_CHOICES:
            raise ValueError("set_doc_kind: 허용되지 않는 종류")
        return {"type": t, "value": action["value"]}
    if decision == "item_map" and t == "map_item":
        ch = action.get("choice")
        if ch != "none":
            if not (isinstance(ch, str) and ch.startswith("item:") and ch[5:].isdigit()
                    and conn.execute("SELECT 1 FROM item WHERE id=?", (int(ch[5:]),)).fetchone()):
                raise ValueError("map_item: 존재하는 품목이어야 합니다")
        return {"type": t, "choice": ch, "label": str(action.get("label") or ch)}
    raise ValueError(f"허용되지 않은 동작: {decision}/{t}")


def validate_scope(scope: dict) -> dict:
    if not isinstance(scope, dict) or set(scope) - set(SCOPE_KEYS):
        raise ValueError(f"범위는 {', '.join(SCOPE_KEYS)} 만 지정할 수 있습니다")
    return {k: int(v) for k, v in scope.items() if v is not None}


# --------------------------------------------------------------------------- 규칙 조회·적용
def matching_versions(conn, *, company_id: int, event_kind: str, decision: str, facts: dict, on_date: str | None):
    """회사·사건 종류·조건·범위·유효기간에 맞는 활성 버전과 매칭 근거."""
    out = []
    for v in conn.execute(
            "SELECT v.*, r.company_id, r.event_kind, r.decision, r.title FROM rule_version v JOIN rule r ON r.id=v.rule_id"
            " WHERE v.status='active' AND r.decision=? AND r.event_kind=? AND (r.company_id=? OR r.company_id IS NULL)",
            (decision, event_kind, company_id)):
        cond, scope = json.loads(v["condition"]), json.loads(v["scope"])
        if any(facts.get(k) != val for k, val in cond.items()):
            continue
        if any(facts.get(k) != val for k, val in scope.items()):
            continue
        d = on_date or _now()[:10]
        if (v["valid_from"] and d < v["valid_from"]) or (v["valid_to"] and d > v["valid_to"]):
            continue
        out.append((v, {"company_id": company_id, "event_kind": event_kind, "condition": cond, "scope": scope,
                        "facts": {k: facts.get(k) for k in [*cond, *scope]}, "date": d}))
    return out


def decide(conn, *, event, decision: str, facts: dict, raw: str | None = None):
    """규칙 적용의 단일 절차(문답·기존 접수 공통).
    ('rule', action, version, basis) | ('conflict', [versions], None, reason) | ('none', None, None, None)
    raw 를 주면 doc_kind 규칙의 동작이 원문 표지와 다른 방향일 때 그 규칙은 적용하지 않는다."""
    found = matching_versions(conn, company_id=event["company_id"], event_kind=DECISIONS[decision]["event_kind"],
                              decision=decision, facts=facts, on_date=event["trade_date"] if "trade_date" in event.keys() else None)
    if decision == "doc_kind" and raw is not None:
        found = [(v, b) for v, b in found if not marker_conflicts(raw, json.loads(v["action"]).get("value"))]
    if not found:
        return "none", None, None, None
    actions = {_j(json.loads(v["action"])) for v, _ in found}
    if len(actions) > 1:
        names = ", ".join(f"{v['rule_id']} v{v['version_no']}" for v, _ in found)
        return "conflict", [v for v, _ in found], None, f"활성 규칙 충돌({names}) — 우선순위 정책이 없어 이 처리만 보류"
    v, basis = found[0]
    return "rule", json.loads(v["action"]), v, basis


def record_application(conn, *, event_id, version_id, target, basis, snapshot, result, reason=""):
    rev = conn.execute("SELECT revision FROM event WHERE id=?", (event_id,)).fetchone()[0]
    conn.execute("INSERT INTO rule_application(event_id, event_revision, rule_version_id, target, match_basis,"
                 " input_snapshot, result, reason, at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (event_id, rev, version_id, target, _j(basis), _j(snapshot), result, reason, _now()))


def version_label(v) -> str:
    return f"{v['rule_id']} v{v['version_no']}"


# --------------------------------------------------------------------------- 확인 기록(사례)
def record_case(conn, *, event_id, check_id, kind, key, label, answer, actor="owner", version_id=None, company_id=1):
    rule_id = None
    if version_id:
        rule_id = conn.execute("SELECT rule_id FROM rule_version WHERE id=?", (version_id,)).fetchone()[0]
    return conn.execute(
        "INSERT INTO decision_case(event_id, check_id, decision_kind, decision_key, label, answer, rule_id, rule_version_id,"
        " actor, company_id, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, check_id, kind, key, label, _j(answer), rule_id, version_id, actor, company_id, _now())).lastrowid


def suggestion(conn, kind: str, key: str):
    """지난 답(사용자가 직접 답한 사례)을 제안으로만 돌려준다."""
    c = Counter(r["answer"] for r in conn.execute(
        "SELECT answer FROM decision_case WHERE decision_kind=? AND decision_key=? AND rule_version_id IS NULL",
        (kind, key)))
    if not c:
        return None
    ans, n = c.most_common(1)[0]
    return {"answer": json.loads(ans), "count": n, "total": sum(c.values()), "conflict": len(c) > 1}


def candidates(conn, company_id=1) -> list[dict]:
    """같은 상황에서 같은 답이 반복된 사례 묶음. 저장하지 않고 제시만 한다(LAG 자동 생성 금지)."""
    groups: dict[tuple, list] = defaultdict(list)
    for r in conn.execute("SELECT * FROM decision_case WHERE rule_version_id IS NULL AND company_id=? ORDER BY id",
                          (company_id,)):
        groups[(r["decision_kind"], r["decision_key"])].append(r)
    linked = {r[0] for r in conn.execute(
        "SELECT dc.decision_kind || '|' || dc.decision_key FROM lag_link l JOIN decision_case dc ON dc.id=l.decision_case_id"
        " JOIN lag ON lag.id=l.lag_id WHERE lag.status <> 'closed' OR lag.outcome IN ('rule','narrowed')")}
    out = []
    for (kind, key), rows in groups.items():
        answers = Counter(r["answer"] for r in rows)
        if f"{kind}|{key}" in linked or len(rows) < CANDIDATE_AFTER:
            continue
        if matching_versions(conn, company_id=company_id, event_kind=DECISIONS[kind]["event_kind"], decision=kind,
                             facts=facts_from_key(kind, key), on_date=None):
            continue
        out.append({"decision": kind, "key": key, "label": rows[-1]["label"], "cases": [r["id"] for r in rows],
                    "answers": [{"answer": json.loads(a), "count": n} for a, n in answers.most_common()],
                    "consistent": len(answers) == 1})
    return out




# --------------------------------------------------------------------------- LAG
def _lag_status(conn, lag_id, to, actor, note, **cols):
    row = conn.execute("SELECT * FROM lag WHERE id=?", (lag_id,)).fetchone()
    if not row:
        raise ValueError("LAG 항목이 없습니다")
    sets = ", ".join(["status=?", "updated_at=?"] + [f"{k}=?" for k in cols])
    conn.execute(f"UPDATE lag SET {sets} WHERE id=?", (to, _now(), *cols.values(), lag_id))
    conn.execute("INSERT INTO lag_log(lag_id, from_status, to_status, note, actor, at) VALUES (?,?,?,?,?,?)",
                 (lag_id, row["status"], to, note, actor, _now()))
    return row


def create_lag(conn, *, actor, question, context, case_ids=(), event_ids=(), company_id=1):
    u = user(conn, actor)
    if u["role"] == "system":
        raise Forbidden("시스템은 LAG 를 만들 수 없습니다")
    if not question.strip():
        raise ValueError("질문·문제를 적어 주세요")
    t = _now()
    lid = conn.execute("INSERT INTO lag(company_id, question, context, status, created_by, created_at, updated_at)"
                       " VALUES (?,?,?, 'open', ?,?,?)", (company_id, question.strip(), _j(context), actor, t, t)).lastrowid
    conn.execute("INSERT INTO lag_log(lag_id, from_status, to_status, note, actor, at) VALUES (?,NULL,'open','등록',?,?)",
                 (lid, actor, t))
    link_lag(conn, lid, case_ids=case_ids, event_ids=event_ids)
    return lid


def link_lag(conn, lag_id, *, case_ids=(), event_ids=()):
    for cid in case_ids:
        ev = conn.execute("SELECT event_id FROM decision_case WHERE id=?", (cid,)).fetchone()
        if not ev:
            raise ValueError(f"확인 기록 #{cid} 이(가) 없습니다")
        if not conn.execute("SELECT 1 FROM lag_link WHERE lag_id=? AND decision_case_id=?", (lag_id, cid)).fetchone():
            conn.execute("INSERT INTO lag_link(lag_id, event_id, decision_case_id) VALUES (?,?,?)", (lag_id, ev[0], cid))
    for eid in event_ids:
        if not conn.execute("SELECT 1 FROM lag_link WHERE lag_id=? AND event_id=? AND decision_case_id IS NULL",
                            (lag_id, eid)).fetchone():
            conn.execute("INSERT INTO lag_link(lag_id, event_id) VALUES (?,?)", (lag_id, eid))


def add_verification(conn, *, lag_id, actor, method, case_ids=(), expected, observed=None, limits="",
                     plan_ids=(), event_ids=(), intake_ids=()):
    """검증 기록. 근거는 판단 기록(case_ids), 완료된 실행계획(plan_ids), 사건·증빙(event_ids, intake_ids) 중 하나 이상.
    관찰 결과(observed)를 비우면 사례의 답·계획의 실행 결과에서 그대로 가져온다(다시 입력시키지 않음)."""
    import sqlite3
    u = user(conn, actor)
    if u["role"] == "system":
        raise Forbidden("시스템은 검증 기록을 남길 수 없습니다")
    lag = conn.execute("SELECT * FROM lag WHERE id=?", (lag_id,)).fetchone()
    if not lag or lag["status"] in ("closed",):
        raise ValueError("종료된 LAG 에는 검증을 추가할 수 없습니다. 먼저 재개하세요")
    rows = [conn.execute("SELECT * FROM decision_case WHERE id=?", (c,)).fetchone() for c in case_ids]
    if any(r is None for r in rows):
        raise ValueError("없는 확인 기록이 있습니다")
    plans = [conn.execute("SELECT * FROM plan WHERE id=?", (p,)).fetchone() for p in plan_ids]
    if any(p is None for p in plans):
        raise ValueError("없는 실행계획이 있습니다")
    for p in plans:
        if p["status"] != "DONE":
            raise ValueError(f"PLN-{p['id']} 은(는) 완료되지 않아 실행 결과가 없습니다")
    if not (rows or plans or event_ids or intake_ids):
        raise ValueError("검증 근거(확인 기록·완료된 실행계획·사건·증빙)가 하나 이상 필요합니다")
    if observed is None or not str(observed).strip():
        parts = []
        if rows:
            c = Counter(r["answer"] for r in rows)
            parts.append(f"사례 {len(rows)}건: " + ", ".join(
                f"{_answer_text(rows[0]['decision_kind'], json.loads(a))} {n}건" for a, n in c.most_common()))
        for p in plans:
            parts.append(f"PLN-{p['id']} 결과: {p['result']}")
        if not parts:
            raise ValueError("사건·증빙만으로 검증할 때는 관찰 결과를 적어 주세요")
        observed = " / ".join(parts)
    vid = conn.execute("INSERT INTO verification(lag_id, method, expected, observed, limits, actor, at)"
                       " VALUES (?,?,?,?,?,?,?)", (lag_id, method, expected, observed, limits, actor, _now())).lastrowid
    for r in rows:
        conn.execute("INSERT INTO verification_case(verification_id, decision_case_id) VALUES (?,?)", (vid, r["id"]))
    try:
        for col, ids in (("plan_id", [p["id"] for p in plans]), ("event_id", event_ids), ("intake_id", intake_ids)):
            for x in ids:
                conn.execute(f"INSERT INTO verification_evidence(verification_id, {col}) VALUES (?,?)", (vid, int(x)))
    except sqlite3.IntegrityError as e:
        if "회사 경계" in str(e):
            raise Forbidden("다른 회사의 기록은 검증 근거로 쓸 수 없습니다") from e
        raise ValueError(str(e)) from e
    # LAG 근거에도 연결(계획이 증빙으로 쓴 사건 포함)
    ev_ids = list(event_ids)
    for p in plans:
        ev_ids += [r[0] for r in conn.execute(
            "SELECT event_id FROM plan_link WHERE plan_id=? AND event_id IS NOT NULL AND removed_at IS NULL", (p["id"],))]
    link_lag(conn, lag_id, case_ids=[r["id"] for r in rows], event_ids=sorted(set(ev_ids)))
    if lag["status"] in ("open", "verifying"):
        _lag_status(conn, lag_id, "awaiting", actor, f"검증 기록 #{vid}")
    return vid


def verification_evidence(conn, vid) -> list[str]:
    out = []
    for r in conn.execute("SELECT * FROM verification_evidence WHERE verification_id=? ORDER BY id", (vid,)):
        out.append(f"PLN-{r['plan_id']}" if r["plan_id"] else f"사건#{r['event_id']}" if r["event_id"] else f"증빙#{r['intake_id']}")
    out += [f"사례#{r[0]}" for r in conn.execute("SELECT decision_case_id FROM verification_case WHERE verification_id=?", (vid,))]
    return out


def judge_lag(conn, *, lag_id, actor, kind, reason="", scope=None, action=None):
    """kind: rule(규칙 반영) | narrowed(범위 축소해 반영) | rejected(기각) | hold(보류) | reopen(재개)."""
    u = user(conn, actor)
    if u["role"] == "system":
        raise Forbidden("시스템은 판정할 수 없습니다")
    lag = conn.execute("SELECT * FROM lag WHERE id=?", (lag_id,)).fetchone()
    if not lag:
        raise ValueError("LAG 항목이 없습니다")
    result = {"lag_id": lag_id, "kind": kind}
    if kind == "reopen":
        if lag["status"] not in ("closed", "held"):
            raise Conflict("종료·보류된 LAG 만 재개할 수 있습니다")
        _lag_status(conn, lag_id, "open", actor, reason or "재개", outcome=None)
    elif kind == "hold":
        if lag["status"] == "closed":
            raise Conflict("종료된 LAG 는 보류할 수 없습니다")
        _lag_status(conn, lag_id, "held", actor, reason, status_reason=reason)
    elif kind in ("rejected", "rule", "narrowed"):
        if lag["status"] != "awaiting":
            raise Conflict("검증 기록이 있는 '판정 대기' 상태에서만 판정할 수 있습니다")
        if kind in ("rule", "narrowed"):
            result["rule_version_id"] = _draft_from_lag(conn, lag, actor, scope or {}, action, reason)
        _lag_status(conn, lag_id, "closed", actor, reason, outcome=kind)
    else:
        raise ValueError("판정 종류: rule | narrowed | rejected | hold | reopen")
    conn.execute("INSERT INTO judgment(lag_id, kind, reason, evidence, actor, role, at) VALUES (?,?,?,?,?,?,?)",
                 (lag_id, kind, reason, _evidence(conn, lag_id), actor, u["role"], _now()))
    return result


def _evidence(conn, lag_id):
    v = [f"검증#{r['id']}" for r in conn.execute("SELECT id FROM verification WHERE lag_id=?", (lag_id,))]
    c = [f"사례#{r[0]}" for r in conn.execute(
        "SELECT decision_case_id FROM lag_link WHERE lag_id=? AND decision_case_id IS NOT NULL", (lag_id,))]
    return ", ".join(v + c)


def _draft_from_lag(conn, lag, actor, scope, action, reason):
    ctx = json.loads(lag["context"])
    kind, key = ctx.get("decision"), ctx.get("key")
    if kind not in DECISIONS:
        raise ValueError("이 LAG 는 실행 규칙으로 바꿀 판단 종류가 없습니다. 규칙 없이 종료하세요(기각 등)")
    if action is None:
        cases = conn.execute(
            "SELECT answer FROM decision_case dc JOIN verification_case vc ON vc.decision_case_id=dc.id"
            " JOIN verification v ON v.id=vc.verification_id WHERE v.lag_id=?", (lag["id"],)).fetchall()
        answers = {r[0] for r in cases}
        if len(answers) != 1:
            raise Conflict("검증 사례의 답이 하나로 모이지 않습니다. 동작을 직접 지정하거나 범위를 나누세요")
        action = _action_from_answer(kind, json.loads(answers.pop()))
    action = validate_action(conn, kind, action)
    scope = validate_scope(scope)
    require_specific(kind, facts_from_key(kind, key))
    rule = conn.execute("SELECT * FROM rule WHERE decision=? AND company_id=? AND id IN"
                        " (SELECT rule_id FROM rule_version WHERE condition=?)",
                        (kind, lag["company_id"], _j(facts_from_key(kind, key)))).fetchone()
    if rule is None:
        n = conn.execute("SELECT COUNT(*) FROM rule WHERE id LIKE 'R-L%'").fetchone()[0] + 1
        rid = f"R-L{n:03d}"
        conn.execute("INSERT INTO rule(id, company_id, event_kind, decision, title, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                     (rid, lag["company_id"], DECISIONS[kind]["event_kind"], kind, ctx.get("label") or lag["question"],
                      actor, _now()))
    else:
        rid = rule["id"]
    return create_version(conn, rule_id=rid, actor=actor, condition=facts_from_key(kind, key), action=action,
                          scope=scope, basis=f"LAG#{lag['id']} 판정({reason or '규칙 반영'}) · {_evidence(conn, lag['id'])}",
                          description=f"{ctx.get('label') or lag['question']} → {_answer_text(kind, action)}",
                          lag_id=lag["id"])


def _action_from_answer(kind, answer):
    if kind == "doc_kind":
        return {"type": "set_doc_kind", "value": answer}
    if kind == "item_map":
        return {"type": "map_item", "choice": answer["choice"], "label": answer.get("label")}
    raise ValueError("이 판단은 사례로부터 동작을 만들 수 없습니다")


# --------------------------------------------------------------------------- 규칙 버전
def create_version(conn, *, rule_id, actor, condition, action, scope, basis, description, lag_id=None,
                   valid_from=None, valid_to=None):
    rule = conn.execute("SELECT * FROM rule WHERE id=?", (rule_id,)).fetchone()
    if not rule:
        raise ValueError("규칙이 없습니다")
    action = validate_action(conn, rule["decision"], action)
    scope = validate_scope(scope)
    n = conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM rule_version WHERE rule_id=?", (rule_id,)).fetchone()[0]
    return conn.execute(
        "INSERT INTO rule_version(rule_id, version_no, status, description, condition, action, scope, valid_from, valid_to,"
        " basis, lag_id, created_by, created_at) VALUES (?,?, 'pending', ?,?,?,?,?,?,?,?,?,?)",
        (rule_id, n, description, _j(condition), _j(action), _j(scope), valid_from, valid_to, basis, lag_id, actor,
         _now())).lastrowid


def approve_version(conn, *, version_id, actor, reason=""):
    """정확한 버전을 승인·활성화한다. 이전 활성 버전은 같은 트랜잭션에서 대체 처리."""
    u = user(conn, actor)
    v = conn.execute("SELECT v.*, r.company_id FROM rule_version v JOIN rule r ON r.id=v.rule_id WHERE v.id=?",
                     (version_id,)).fetchone()
    if not v:
        raise ValueError("규칙 버전이 없습니다")
    if u["role"] not in ROLE_CAN_APPROVE:
        raise Forbidden(f"{u['name']}은(는) 규칙을 승인할 권한이 없습니다")
    if v["company_id"] is None and u["role"] != "admin":
        raise Forbidden("공통 규칙은 admin 권한으로만 승인할 수 있습니다")
    if v["company_id"] is not None and u["role"] != "admin" and u["company_id"] != v["company_id"]:
        raise Forbidden("다른 회사의 규칙은 승인할 수 없습니다")
    if v["status"] != "pending":
        raise Conflict(f"승인 대기 상태가 아닙니다(현재 {v['status']})")
    newer = conn.execute("SELECT version_no FROM rule_version WHERE rule_id=? AND version_no>? AND status IN"
                         " ('pending','active','draft')", (v["rule_id"], v["version_no"])).fetchone()
    if newer:
        raise Conflict(f"더 새로운 버전 v{newer[0]}이(가) 있습니다. 오래된 버전은 승인할 수 없습니다")
    t = _now()
    conn.execute("UPDATE rule_version SET status='superseded' WHERE rule_id=? AND status='active'", (v["rule_id"],))
    from . import process
    if process.FAULT_HOOK:
        process.FAULT_HOOK()
    conn.execute("UPDATE rule_version SET status='active' WHERE id=?", (version_id,))
    conn.execute("INSERT INTO judgment(rule_version_id, kind, reason, evidence, actor, role, at) VALUES (?,?,?,?,?,?,?)",
                 (version_id, "approve", reason, v["basis"], actor, u["role"], t))
    return {"version_id": version_id, "rule_id": v["rule_id"], "version_no": v["version_no"], "status": "active"}


def set_version_status(conn, *, version_id, actor, to, reason=""):
    """활성 버전 중지(suspend) 또는 폐기(retire). 과거 적용 기록·거래는 그대로 둔다."""
    u = user(conn, actor)
    if u["role"] not in ROLE_CAN_APPROVE:
        raise Forbidden(f"{u['name']}은(는) 규칙 상태를 바꿀 권한이 없습니다")
    v = conn.execute("SELECT * FROM rule_version WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise ValueError("규칙 버전이 없습니다")
    allowed = {"suspended": ("active",), "retired": ("active", "suspended", "pending"), "active": ("suspended",)}
    if to not in allowed or v["status"] not in allowed[to]:
        raise Conflict(f"{v['status']} → {to} 전환은 허용되지 않습니다")
    if to == "active":
        return _resume(conn, v, u, reason)
    conn.execute("UPDATE rule_version SET status=? WHERE id=?", (to, version_id))
    conn.execute("INSERT INTO judgment(rule_version_id, kind, reason, evidence, actor, role, at) VALUES (?,?,?,?,?,?,?)",
                 (version_id, "suspend" if to == "suspended" else "retire", reason, "", actor, u["role"], _now()))
    return {"version_id": version_id, "status": to}


def _resume(conn, v, u, reason):
    newer = conn.execute("SELECT 1 FROM rule_version WHERE rule_id=? AND version_no>? AND status IN ('pending','active')",
                         (v["rule_id"], v["version_no"])).fetchone()
    if newer:
        raise Conflict("더 새로운 버전이 있어 이 버전을 다시 활성화할 수 없습니다")
    conn.execute("UPDATE rule_version SET status='active' WHERE id=?", (v["id"],))
    conn.execute("INSERT INTO judgment(rule_version_id, kind, reason, evidence, actor, role, at) VALUES (?,?,?,?,?,?,?)",
                 (v["id"], "resume", reason, "", u["id"], u["role"], _now()))
    return {"version_id": v["id"], "status": "active"}


# --------------------------------------------------------------------------- 조회
def _answer_text(kind, answer) -> str:
    from . import contract
    if isinstance(answer, dict) and answer.get("type"):
        if answer["type"] == "set_doc_kind":
            return contract.DOC_KIND_CHOICES.get(answer["value"], answer["value"])
        if answer["type"] == "map_item":
            return answer.get("label") or answer["choice"]
        if answer["type"] == "compare_pallets":
            return "일치하면 확정, 다르면 확인 필수"
    if kind == "doc_kind":
        return contract.DOC_KIND_CHOICES.get(answer, str(answer))
    if kind == "item_map":
        return answer.get("label") if isinstance(answer, dict) else str(answer)
    return str(answer)


def overview(conn, company_id=1) -> dict:
    rules = []
    for r in conn.execute("SELECT * FROM rule WHERE company_id=? OR company_id IS NULL ORDER BY id", (company_id,)):
        vs = [dict(v) | {"action_text": _answer_text(r["decision"], json.loads(v["action"])),
                         "applied": conn.execute("SELECT COUNT(*) FROM rule_application WHERE rule_version_id=?",
                                                 (v["id"],)).fetchone()[0]}
              for v in conn.execute("SELECT * FROM rule_version WHERE rule_id=? ORDER BY version_no DESC", (r["id"],))]
        rules.append(dict(r) | {"versions": vs})
    lags = []
    from . import plan as plan_mod
    for l in conn.execute("SELECT * FROM lag WHERE company_id=? AND layer='ops' ORDER BY CASE status WHEN 'awaiting' THEN 0 WHEN 'open'"
                          " THEN 1 WHEN 'verifying' THEN 1 WHEN 'held' THEN 2 ELSE 3 END, id DESC", (company_id,)):
        lags.append(dict(l) | {
            "links": conn.execute("SELECT COUNT(*) FROM lag_link WHERE lag_id=?", (l["id"],)).fetchone()[0],
            "verifications": [dict(v) | {"evidence": verification_evidence(conn, v["id"])} for v in conn.execute(
                "SELECT * FROM verification WHERE lag_id=? ORDER BY id", (l["id"],))],
            "log": [dict(x) for x in conn.execute("SELECT * FROM lag_log WHERE lag_id=? ORDER BY id", (l["id"],))],
            "plans": [{"id": p["id"], "title": p["title"], "status": p["status"]}
                      for p in plan_mod.list_plans(conn, company_id, lag_id=l["id"])],
            "case_ids": [x[0] for x in conn.execute(
                "SELECT decision_case_id FROM lag_link WHERE lag_id=? AND decision_case_id IS NOT NULL", (l["id"],))],
        })
    asked = conn.execute("SELECT COUNT(*) FROM check_item WHERE state='answered'").fetchone()[0]
    auto = conn.execute("SELECT COUNT(*) FROM rule_application WHERE result='performed'").fetchone()[0]
    pending = sum(1 for r in rules for v in r["versions"] if v["status"] == "pending")
    return {"rules": rules, "lags": lags, "plans": plan_mod.list_plans(conn, company_id),
            "reclassification": plan_mod.reclassification_table(conn), "candidates": candidates(conn, company_id), "candidate_after": CANDIDATE_AFTER,
            "stats": {"asked": asked, "auto": auto}, "pending": pending,
            "users": [dict(u) for u in conn.execute("SELECT * FROM app_user WHERE role<>'system'")],
            "code_defaults": [dict(r) for r in conn.execute(
                "SELECT id, condition, action, moved_to FROM rulebook WHERE moved_to LIKE 'code-default%'")]}


def trace_version(conn, version_id) -> dict:
    """규칙 버전 → 판정 → LAG → 검증 → 사례 → 적용 사건 추적."""
    v = conn.execute("SELECT * FROM rule_version WHERE id=?", (version_id,)).fetchone()
    lag_id = v["lag_id"]
    return {
        "version": dict(v),
        "judgments": [dict(r) for r in conn.execute("SELECT * FROM judgment WHERE rule_version_id=? OR lag_id=? ORDER BY id",
                                                    (version_id, lag_id))],
        "lag": dict(conn.execute("SELECT * FROM lag WHERE id=?", (lag_id,)).fetchone()) if lag_id else None,
        "verifications": [dict(r) for r in conn.execute("SELECT * FROM verification WHERE lag_id=?", (lag_id,))],
        "cases": [dict(r) for r in conn.execute(
            "SELECT dc.* FROM decision_case dc JOIN lag_link l ON l.decision_case_id=dc.id WHERE l.lag_id=?", (lag_id,))],
        "applications": [dict(r) for r in conn.execute("SELECT * FROM rule_application WHERE rule_version_id=? ORDER BY id",
                                                       (version_id,))],
    }


# --------------------------------------------------------------------------- 사용자 명시 일반화 지시 (DEV-004 v0.2 §3)
def directive_rule(conn, *, actor, company_id, decision, condition, action, title, instruction, answer_id, event_id):
    """'다음에도 이 양식은 팔레트 입고로 처리해' 같은 명시적 지시를 근거로 한 번의 사용자 결정에서
    LAG(출처) → 판정(규칙 반영) → 규칙 버전을 함께 만든다. 승인권자면 바로 활성화, 아니면 승인 대기.
    수행하지 않은 검증 기록은 만들지 않는다(검증 0건으로 남김)."""
    u = user(conn, actor)
    if u["role"] == "system":
        raise Forbidden("시스템은 규칙을 만들 수 없습니다")
    action = validate_action(conn, decision, action)
    require_specific(decision, condition)
    t = _now()
    lid = conn.execute("INSERT INTO lag(company_id, question, context, status, outcome, origin, created_by, created_at,"
                       " updated_at) VALUES (?,?,?, 'closed', 'rule', ?, ?, ?, ?)",
                       (company_id, f"{title} — 사용자 지시로 일반화",
                        _j({"decision": decision, "key": case_key(condition), "condition": condition, "answer_id": answer_id}),
                        f"dialog answer#{answer_id}", actor, t, t)).lastrowid
    conn.execute("INSERT INTO lag_log(lag_id, from_status, to_status, note, actor, at) VALUES (?,NULL,'closed',?,?,?)",
                 (lid, f"사용자 명시 지시: {instruction}", actor, t))
    if event_id:
        conn.execute("INSERT INTO lag_link(lag_id, event_id) VALUES (?,?)", (lid, event_id))
    conn.execute("INSERT INTO judgment(lag_id, kind, reason, evidence, actor, role, at) VALUES (?,?,?,?,?,?,?)",
                 (lid, "rule", "사용자 명시 일반화 지시", f"answer#{answer_id}: {instruction}", actor, u["role"], t))
    rule = conn.execute("SELECT r.id FROM rule r JOIN rule_version v ON v.rule_id=r.id WHERE r.decision=? AND"
                        " r.company_id=? AND v.condition=? LIMIT 1", (decision, company_id, _j(condition))).fetchone()
    if rule:
        rid = rule[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM rule WHERE id LIKE 'R-D%'").fetchone()[0] + 1
        rid = f"R-D{n:03d}"
        conn.execute("INSERT INTO rule(id, company_id, event_kind, decision, title, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                     (rid, company_id, DECISIONS[decision]["event_kind"], decision, title, actor, t))
    vid = create_version(conn, rule_id=rid, actor=actor, condition=condition, action=action, scope={},
                         basis=f"사용자 명시 지시(answer#{answer_id}) '{instruction}' · LAG#{lid} · 검증 기록 없음",
                         description=title, lag_id=lid)
    status = "pending"
    if u["role"] in ROLE_CAN_APPROVE:
        approve_version(conn, version_id=vid, actor=actor, reason="지시한 사용자가 승인권자")
        status = "active"
    return {"lag_id": lid, "rule_id": rid, "rule_version_id": vid, "status": status}
