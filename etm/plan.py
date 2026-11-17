"""운영 PLN — 실행계획 (DEV-003).

세 축의 책임
- RBK: 어떤 조건에서 어떤 처리를 재사용할지 (승인된 규칙 버전)
- LAG: 무엇을 아직 모르며 어떤 근거로 판정할지 (질문·검증·판정)
- PLN: 무엇을 실행하고 무엇으로 완료를 판단할지 (조치·완료 조건·담당·기한·진행·결과)

상태
  DRAFT ──(명시적 실행 지시: 승인권자 또는 담당자)──▶ READY ──▶ IN_PROGRESS ──(결과+증빙)──▶ DONE
     │                                              │  ▲              │                    │
     └──▶ CANCELLED       BLOCKED / DEFERRED ◀──────┘  └── 재개 ──────┘          재개(이력 보존) ◀┘
- 시스템은 실행 지시를 할 수 없다(초안을 사용자 지시로 꾸며 활성화하지 않음).
- DONE 은 결과와 해제되지 않은 증빙 연결이 있어야 한다(서비스 + DB 트리거).
- 승인된 범위(목적·조치·완료 조건)를 바꾸면 DRAFT 로 돌아가 다시 실행 지시를 받는다.
- PLN 상태 변경은 재고·채권·채무를 바꾸지 않고, LAG 판정·RBK 승인을 자동으로 일으키지 않는다.
- 모든 변경은 버전 검사(expected_version)와 트랜잭션, request_id 멱등성으로 보호한다.
"""
from __future__ import annotations

import json

from . import learn
from .learn import Conflict, Forbidden

STATUSES = ("DRAFT", "READY", "IN_PROGRESS", "DONE", "BLOCKED", "DEFERRED", "CANCELLED")
STATUS_LABEL = {"DRAFT": "초안", "READY": "실행 지시됨", "IN_PROGRESS": "진행 중", "DONE": "완료",
                "BLOCKED": "차단", "DEFERRED": "보류", "CANCELLED": "취소"}
TRANSITIONS = {   # (from, to): 사유 필요 여부
    ("READY", "IN_PROGRESS"): False,
    ("READY", "BLOCKED"): True, ("READY", "DEFERRED"): True, ("READY", "CANCELLED"): True,
    ("IN_PROGRESS", "BLOCKED"): True, ("IN_PROGRESS", "DEFERRED"): True, ("IN_PROGRESS", "CANCELLED"): True,
    ("BLOCKED", "READY"): True, ("DEFERRED", "READY"): True,
    ("BLOCKED", "CANCELLED"): True, ("DEFERRED", "CANCELLED"): True,
    ("DRAFT", "CANCELLED"): True,
}
SCOPE_FIELDS = ("purpose", "action", "done_criteria")          # 승인된 범위
EDIT_FIELDS = ("title", *SCOPE_FIELDS, "assignee", "due_date")
TARGETS = {"lag": "lag_id", "event": "event_id", "intake": "intake_id", "rule_version": "rule_version_id",
           "case": "decision_case_id", "obligation": "obligation_id"}
TARGET_TABLE = {"lag_id": "lag", "event_id": "event", "intake_id": "intake", "rule_version_id": "rule_version",
                "decision_case_id": "decision_case", "obligation_id": "obligation"}


def _now():
    from .process import now
    return now()


def _j(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _human(conn, actor):
    u = learn.user(conn, actor)
    if u["role"] == "system":
        raise Forbidden("시스템은 실행계획을 만들거나 바꿀 수 없습니다")
    return u


def _get(conn, plan_id, expected_version=None):
    p = conn.execute("SELECT * FROM plan WHERE id=?", (plan_id,)).fetchone()
    if not p:
        raise ValueError(f"PLN-{plan_id} 이(가) 없습니다")
    if expected_version is not None and int(expected_version) != p["version"]:
        raise Conflict(f"PLN-{plan_id} 이(가) 다른 곳에서 바뀌었습니다(현재 버전 {p['version']}). 새로 불러와 다시 하세요")
    return p


def _log(conn, p, action, actor, *, to=None, changes=None, reason=None, version=None):
    conn.execute("INSERT INTO plan_log(plan_id, plan_version, action, from_status, to_status, changes, reason, actor, at)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (p["id"], version or p["version"], action, p["status"], to or p["status"], _j(changes or {}), reason,
                  actor, _now()))


def _bump(conn, p, **cols):
    sets = ", ".join([f"{k}=?" for k in cols] + ["version=version+1", "updated_at=?"])
    conn.execute(f"UPDATE plan SET {sets} WHERE id=? AND version=?", (*cols.values(), _now(), p["id"], p["version"]))
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise Conflict("동시 수정 충돌")
    return p["version"] + 1


def _can_order(u, p):
    """실행 지시 권한: 승인권자(approver/admin) 또는 지정된 담당자."""
    return u["role"] in ("approver", "admin") or (p["assignee"] and p["assignee"] == u["id"])


# --------------------------------------------------------------------------- 생성·수정
def create(conn, *, actor, title, company_id=1, purpose=None, action=None, done_criteria=None, assignee=None,
           due_date=None, links=(), execute_now=False, origin=None):
    """links: [{"role": "basis"|"target"|"evidence", "kind": "lag"|"event"|..., "id": n, "note": ...}]
    execute_now: 사용자가 직접 실행을 지시한 경우 생성과 READY 전환을 한 동작으로."""
    u = _human(conn, actor)
    if not (title or "").strip():
        raise ValueError("계획 제목이 필요합니다")
    if assignee:
        learn.user(conn, assignee)
    t = _now()
    pid = conn.execute(
        "INSERT INTO plan(company_id, title, purpose, action, done_criteria, assignee, due_date, origin, created_by,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (company_id, title.strip(), purpose, action, done_criteria, assignee, due_date, origin, actor, t, t)).lastrowid
    p = _get(conn, pid)
    _log(conn, p, "create", actor, changes={k: p[k] for k in ("title", *SCOPE_FIELDS, "assignee", "due_date")})
    for ln in links:
        link(conn, plan_id=pid, actor=actor, role=ln.get("role", "basis"), kind=ln["kind"], target_id=ln["id"],
             note=ln.get("note"))
    if execute_now:
        approve(conn, plan_id=pid, actor=actor, reason="생성과 함께 실행 지시")
    return pid


def approve(conn, *, plan_id, actor, expected_version=None, reason=""):
    """DRAFT → READY. 명시적 실행 지시."""
    u = _human(conn, actor)
    p = _get(conn, plan_id, expected_version)
    if p["status"] != "DRAFT":
        raise Conflict(f"초안(DRAFT)만 실행 지시할 수 있습니다(현재 {p['status']})")
    if not _can_order(u, p):
        raise Forbidden(f"{u['name']}은(는) 이 계획의 실행을 지시할 권한이 없습니다(승인권자 또는 담당자만)")
    if not (p["action"] or "").strip():
        raise ValueError("조치 내용이 정해지지 않은 계획은 실행 지시할 수 없습니다")
    t = _now()
    v = _bump(conn, p, status="READY", approved_by=actor, approved_at=t, status_reason=None)
    _log(conn, p, "approve", actor, to="READY", reason=reason, version=v)
    return {"plan_id": plan_id, "status": "READY", "version": v}


def transition(conn, *, plan_id, actor, to, reason="", expected_version=None):
    u = _human(conn, actor)
    p = _get(conn, plan_id, expected_version)
    key = (p["status"], to)
    if key not in TRANSITIONS:
        hint = " 실행 지시(approve)가 먼저 필요합니다." if p["status"] == "DRAFT" else ""
        hint = " 완료는 결과·증빙과 함께 complete 로 합니다." if to == "DONE" else hint
        raise Conflict(f"{p['status']} → {to} 전환은 허용되지 않습니다.{hint}")
    if TRANSITIONS[key] and not (reason or "").strip():
        raise ValueError("이 전환에는 사유가 필요합니다")
    if key == ("BLOCKED", "READY") or key == ("DEFERRED", "READY"):
        if not _can_order(u, p):
            raise Forbidden("재개는 승인권자 또는 담당자만 할 수 있습니다")
    from . import process
    v = _bump(conn, p, status=to, status_reason=reason or None)
    if process.FAULT_HOOK:
        process.FAULT_HOOK()
    _log(conn, p, "transition", actor, to=to, reason=reason, version=v)
    return {"plan_id": plan_id, "status": to, "version": v}


def complete(conn, *, plan_id, actor, result, evidence=(), expected_version=None):
    """IN_PROGRESS → DONE. evidence: 새로 연결할 증빙 [{kind, id, note}] (기존 evidence 연결도 인정)."""
    _human(conn, actor)
    p = _get(conn, plan_id, expected_version)
    if p["status"] != "IN_PROGRESS":
        raise Conflict(f"진행 중(IN_PROGRESS)인 계획만 완료할 수 있습니다(현재 {p['status']})")
    if not (result or "").strip():
        raise ValueError("실행 결과를 적어 주세요")
    if not (p["done_criteria"] or "").strip():
        raise ValueError("완료 조건이 없는 계획은 완료할 수 없습니다. 먼저 완료 조건을 정하세요")
    for ev in evidence:
        link(conn, plan_id=plan_id, actor=actor, role="evidence", kind=ev["kind"], target_id=ev["id"], note=ev.get("note"),
             _log_it=False)
    links = [dict(r) for r in conn.execute(
        "SELECT * FROM plan_link WHERE plan_id=? AND role='evidence' AND removed_at IS NULL", (plan_id,))]
    if not links:
        raise ValueError("완료 근거(증빙: 사건·증빙·확인 기록·LAG 등)를 하나 이상 연결해 주세요")
    p = _get(conn, plan_id)
    v = _bump(conn, p, status="DONE", result=result.strip(), done_at=_now(), status_reason=None)
    _log(conn, p, "complete", actor, to="DONE", version=v,
         changes={"result": result.strip(), "done_criteria": p["done_criteria"],
                  "evidence": [_target_text(conn, l) for l in links]})
    return {"plan_id": plan_id, "status": "DONE", "version": v}


def reopen(conn, *, plan_id, actor, reason, expected_version=None):
    """DONE → IN_PROGRESS. 이전 결과·완료 근거는 이력에 남기고 지운다."""
    u = _human(conn, actor)
    p = _get(conn, plan_id, expected_version)
    if p["status"] != "DONE":
        raise Conflict("완료된 계획만 재개할 수 있습니다")
    if not (reason or "").strip():
        raise ValueError("재개 사유가 필요합니다")
    if not _can_order(u, p):
        raise Forbidden("재개는 승인권자 또는 담당자만 할 수 있습니다")
    v = _bump(conn, p, status="IN_PROGRESS", result=None, done_at=None, status_reason=reason)
    _log(conn, p, "reopen", actor, to="IN_PROGRESS", reason=reason, version=v,
         changes={"previous_result": p["result"], "previous_done_at": p["done_at"]})
    return {"plan_id": plan_id, "status": "IN_PROGRESS", "version": v}


def edit(conn, *, plan_id, actor, changes: dict, expected_version=None, reason=""):
    _human(conn, actor)
    p = _get(conn, plan_id, expected_version)
    bad = set(changes) - set(EDIT_FIELDS)
    if bad:
        raise ValueError(f"수정할 수 없는 항목: {', '.join(sorted(bad))}")
    if p["status"] in ("DONE", "CANCELLED"):
        raise Conflict("완료·취소된 계획은 수정할 수 없습니다. 완료된 계획은 재개 후 수정하세요")
    diff = {k: {"old": p[k], "new": v} for k, v in changes.items() if p[k] != v}
    if not diff:
        return {"plan_id": plan_id, "version": p["version"], "status": p["status"]}
    if "assignee" in diff and diff["assignee"]["new"]:
        learn.user(conn, diff["assignee"]["new"])
    cols = {k: d["new"] for k, d in diff.items()}
    to = p["status"]
    if p["approved_by"] and set(diff) & set(SCOPE_FIELDS):
        # 이미 승인한 범위가 바뀌면 다시 판정받는다
        cols.update(status="DRAFT", approved_by=None, approved_at=None,
                    status_reason="승인된 범위 변경 — 다시 실행 지시 필요")
        to = "DRAFT"
    v = _bump(conn, p, **cols)
    _log(conn, p, "edit", actor, to=to, changes=diff, reason=reason, version=v)
    return {"plan_id": plan_id, "version": v, "status": to}


# --------------------------------------------------------------------------- 연결
def link(conn, *, plan_id, actor, role, kind, target_id, note=None, _log_it=True):
    _human(conn, actor)
    p = _get(conn, plan_id)
    col = TARGETS.get(kind)
    if not col:
        raise ValueError(f"연결 대상 종류: {', '.join(TARGETS)}")
    if role not in ("basis", "target", "evidence"):
        raise ValueError("연결 역할: basis(근거) | target(대상) | evidence(완료 증빙)")
    if not conn.execute(f"SELECT 1 FROM {TARGET_TABLE[col]} WHERE id=?", (int(target_id),)).fetchone():
        raise ValueError(f"연결할 {kind} #{target_id} 이(가) 없습니다")
    if kind == "lag" and conn.execute("SELECT layer FROM lag WHERE id=?", (int(target_id),)).fetchone()[0] != "ops":
        raise ValueError("설계 계층(TRNK)으로 재분류된 LAG 에는 운영 계획을 연결하지 않습니다")
    dup = conn.execute(f"SELECT id FROM plan_link WHERE plan_id=? AND role=? AND {col}=? AND removed_at IS NULL",
                       (plan_id, role, int(target_id))).fetchone()
    if dup:
        return dup[0]
    import sqlite3
    try:
        lid = conn.execute(f"INSERT INTO plan_link(plan_id, role, {col}, note, created_by, created_at) VALUES (?,?,?,?,?,?)",
                           (plan_id, role, int(target_id), note, actor, _now())).lastrowid
    except sqlite3.IntegrityError as e:
        if "회사 경계" in str(e):
            raise Forbidden("다른 회사의 기록에는 연결할 수 없습니다") from e
        raise
    if _log_it:
        _log(conn, p, "link", actor, changes={"role": role, "target": f"{kind}#{target_id}", "note": note})
    return lid


def unlink(conn, *, link_id, actor, reason):
    _human(conn, actor)
    ln = conn.execute("SELECT * FROM plan_link WHERE id=?", (link_id,)).fetchone()
    if not ln or ln["removed_at"]:
        raise ValueError("해제할 연결이 없습니다")
    if not (reason or "").strip():
        raise ValueError("연결 해제 사유가 필요합니다")
    p = _get(conn, ln["plan_id"])
    if p["status"] == "DONE" and ln["role"] == "evidence":
        raise Conflict("완료된 계획의 완료 근거는 해제할 수 없습니다. 재개 후 해제하세요")
    conn.execute("UPDATE plan_link SET removed_by=?, removed_at=?, removed_reason=? WHERE id=?",
                 (actor, _now(), reason, link_id))
    _log(conn, p, "unlink", actor, changes={"link_id": link_id, "target": _target_text(conn, ln)}, reason=reason)


def _target_text(conn, ln) -> str:
    for col, table in TARGET_TABLE.items():
        if ln[col] is not None:
            kind = next(k for k, c in TARGETS.items() if c == col)
            if kind == "lag":
                return f"LAG-{ln[col]}"
            if kind == "rule_version":
                v = conn.execute("SELECT rule_id, version_no FROM rule_version WHERE id=?", (ln[col],)).fetchone()
                return f"{v[0]} v{v[1]}"
            if kind == "event":
                from .process import event_title
                return f"사건#{ln[col]} {event_title(conn, ln[col])}"
            return f"{kind}#{ln[col]}"
    return "?"


# --------------------------------------------------------------------------- 조회
def detail(conn, plan_id) -> dict:
    p = dict(_get(conn, plan_id))
    links = [dict(r) for r in conn.execute("SELECT * FROM plan_link WHERE plan_id=? ORDER BY id", (plan_id,))]
    for ln in links:
        ln["target"] = _target_text(conn, ln)
        ln["kind"] = next(k for k, c in TARGETS.items() if ln[c] is not None)
        ln["target_id"] = ln[TARGETS[ln["kind"]]]
    p.update(status_label=STATUS_LABEL[p["status"]], links=links,
             log=[dict(r) for r in conn.execute("SELECT * FROM plan_log WHERE plan_id=? ORDER BY id", (plan_id,))])
    return p


def list_plans(conn, company_id=1, *, lag_id=None, event_id=None) -> list[dict]:
    q = "SELECT DISTINCT p.id FROM plan p LEFT JOIN plan_link l ON l.plan_id=p.id AND l.removed_at IS NULL WHERE p.company_id=?"
    args: list = [company_id]
    if lag_id:
        q += " AND l.lag_id=?"
        args.append(lag_id)
    if event_id:
        q += " AND l.event_id=?"
        args.append(event_id)
    q += (" ORDER BY CASE p.status WHEN 'IN_PROGRESS' THEN 0 WHEN 'READY' THEN 1 WHEN 'DRAFT' THEN 2 WHEN 'BLOCKED' THEN 3"
          " WHEN 'DEFERRED' THEN 4 ELSE 5 END, p.id DESC")
    return [detail(conn, r[0]) for r in conn.execute(q, args)]


# --------------------------------------------------------------------------- 이관·재분류 (비파괴, 재실행 안전)
LEGACY_TARGETS = {
    # 커리큘럼 제목 → (대상 계층, Docs 대응 항목, 사유)
    "처리 건수 집계 규칙": ("TRNK-PLN", "Docs PLN v0.1 §4 '처리 건수' 미결정",
                      "ETM 자체의 설계 결정 사항(이력 건수 정의). 운영 실행계획이 아님"),
    "스캔 출력 어댑터 연결": ("TRNK-PLN", "Docs DEV-001 §3 / PLN v0.1 §6 (스캔·처리 공통 데이터 규격)",
                       "코드 변경이 필요한 개발 과제. 운영 실행계획이 아님"),
    "확정 사례 → 운영 규칙 승격 기준": ("TRNK-LAG", "Docs LAG v0.1 LAG-001",
                              "설계 질문. Docs LAG-001 과 같은 질문이며 운영 사례가 붙지 않음"),
    "미결·완결 세부 상태와 완료 조건 검증": ("TRNK-LAG", None,
                                 "설계 질문(PLN v0.3 '세부 상태·완료 조건은 사례별 검증 필요'). Docs 에 대응 LAG ID 없음 — TRNK 등록 대기"),
}


def reclassify_legacy(conn, actor="system") -> list[dict]:
    """이전 curriculum 의 'PLN' 표시만 있는 항목과 운영 LAG 에 잘못 들어간 설계 질문을 실제 레코드 기준으로 재분류.
    실제로 있는 레코드만 처리하고, 이미 처리한 것은 건너뛴다(UNIQUE). 삭제하지 않는다."""
    done = []
    t = _now()
    for r in conn.execute("SELECT * FROM curriculum").fetchall():
        target = LEGACY_TARGETS.get(r["title"])
        if not target or conn.execute("SELECT 1 FROM reclassification WHERE source_table='curriculum' AND source_id=?",
                                      (r["id"],)).fetchone():
            continue
        layer, ref, reason = target
        moved = r["moved_to"] or ""
        if moved.startswith("LAG:"):
            # 운영 LAG 로 옮겨졌던 설계 질문 → LAG 레코드를 TRNK 계층으로 표시(이력 보존)
            lag_id = int(moved[4:])
            lag = conn.execute("SELECT * FROM lag WHERE id=?", (lag_id,)).fetchone()
            if lag is None:
                conn.execute("INSERT INTO reclassification(source_table, source_id, target_layer, target_ref, status,"
                             " reason, actor, at) VALUES ('curriculum', ?, ?, ?, 'pending_trnk', ?, ?, ?)",
                             (r["id"], layer, ref, f"표시된 LAG#{lag_id} 레코드 없음 — 복구 대기", actor, t))
                continue
            conn.execute("UPDATE lag SET layer='trnk', updated_at=? WHERE id=?", (t, lag_id))
            conn.execute("INSERT INTO lag_log(lag_id, from_status, to_status, note, actor, at) VALUES (?,?,?,?,?,?)",
                         (lag_id, lag["status"], lag["status"],
                          f"DEV-003 재분류: 운영 LAG → {layer} ({ref or 'TRNK 등록 대기'})", actor, t))
            conn.execute("INSERT INTO reclassification(source_table, source_id, target_layer, target_ref, status, reason,"
                         " actor, at) VALUES ('lag', ?, ?, ?, ?, ?, ?, ?)",
                         (lag_id, layer, ref, "done" if ref else "pending_trnk", reason, actor, t))
            status = "done" if ref else "pending_trnk"
            conn.execute("INSERT INTO reclassification(source_table, source_id, target_layer, target_ref, status, reason,"
                         " actor, at) VALUES ('curriculum', ?, ?, ?, ?, ?, ?, ?)",
                         (r["id"], layer, f"LAG#{lag_id} → {ref or 'TRNK 등록 대기'}", status, reason, actor, t))
        elif moved == "PLN":
            conn.execute("INSERT INTO reclassification(source_table, source_id, target_layer, target_ref, status, reason,"
                         " actor, at) VALUES ('curriculum', ?, ?, ?, 'done', ?, ?, ?)", (r["id"], layer, ref, reason, actor, t))
            conn.execute("UPDATE curriculum SET moved_to=? WHERE id=?", (f"{layer}: {ref}", r["id"]))
        else:
            continue
        done.append({"curriculum": r["id"], "title": r["title"], "to": layer, "ref": ref})
    return done


def reclassification_table(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM reclassification ORDER BY id")]
