"""판단 위임과 학습 구조.

원칙
- 프로그램은 스스로 판단하지 않는다. **승인된 룰북 규칙**이 있으면 그 규칙대로 처리하고,
  없으면 사용자에게 묻는다(제안은 보여줄 수 있지만 결정은 사용자가 한다).
- 사용자의 답은 확정 사례(decision_case)로 쌓인다.
- 같은 상황에서 같은 답이 PROMOTE_AFTER 번 쌓이면 커리큘럼에 '규칙 제안'이 생긴다.
  사용자가 승인해야만 룰북 규칙이 되고(자동 승격 없음), 그 뒤로는 묻지 않는다.
- 답이 엇갈리면 제안하지 않고 커리큘럼에 '판정 엇갈림'으로 남긴다.
- 규칙은 언제든 중지할 수 있고, 중지하면 다시 묻는다.

결과적으로 프로그램이 자동으로 하는 일은 모두 룰북에 적혀 있게 된다.
"""
from __future__ import annotations

import json
from collections import Counter

PROMOTE_AFTER = 3  # 잠정값. 승격 제안 기준은 TRNK 결정 사항(CUR §4 '학습 규칙')

DECISION_KINDS = {
    "doc_kind": "자료 종류(가치 이동 방향)",
    "item_map": "입고 품목 연결",
}


def _now():
    from .process import now
    return now()


def _j(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def find_rule(conn, kind: str, key: str):
    return conn.execute(
        "SELECT * FROM rulebook WHERE decision_kind=? AND decision_key=? AND status='active'", (kind, key)).fetchone()


def rule_answer(rule):
    return json.loads(rule["answer"])


def record_case(conn, *, event_id, check_id, kind, key, label, answer, rule_id=None):
    conn.execute(
        "INSERT INTO decision_case(event_id, check_id, decision_kind, decision_key, label, answer, rule_id, at)"
        " VALUES (?,?,?,?,?,?,?,?)", (event_id, check_id, kind, key, label, _j(answer), rule_id, _now()))
    if rule_id:
        conn.execute("UPDATE rulebook SET case_count = case_count + 1 WHERE id=?", (rule_id,))
    else:
        update_curriculum(conn, kind, key, label)


def past_answers(conn, kind: str, key: str) -> Counter:
    """사용자가 직접 답한 과거 사례만 센다(규칙이 처리한 것은 제외)."""
    return Counter(r["answer"] for r in conn.execute(
        "SELECT answer FROM decision_case WHERE decision_kind=? AND decision_key=? AND rule_id IS NULL", (kind, key)))


def suggestion(conn, kind: str, key: str):
    """과거 사례가 있으면 '지난번 답'을 제안으로 돌려준다. 결정은 아니다."""
    c = past_answers(conn, kind, key)
    if not c:
        return None
    ans, n = c.most_common(1)[0]
    return {"answer": json.loads(ans), "count": n, "total": sum(c.values()), "conflict": len(c) > 1}


def update_curriculum(conn, kind: str, key: str, label: str):
    if find_rule(conn, kind, key):
        return
    c = past_answers(conn, kind, key)
    n = sum(c.values())
    row = conn.execute("SELECT * FROM curriculum WHERE decision_kind=? AND decision_key=? AND state<>'done'",
                       (kind, key)).fetchone()
    if len(c) > 1:
        state, proposed = "conflict", None
        title = f"판정 엇갈림: {label}"
        next_check = "같은 상황인데 답이 다릅니다. 상황을 더 나눠야 하는지 검토"
    elif n >= PROMOTE_AFTER:
        state, proposed = "proposed", next(iter(c))
        title = f"규칙 제안: {label}"
        next_check = "승인하면 이후 같은 상황은 묻지 않고 처리"
    else:
        state, proposed = "collecting", next(iter(c))
        title = f"학습 중: {label}"
        next_check = f"같은 답 {PROMOTE_AFTER}건이면 규칙 제안"
    evidence = ", ".join(f"{_answer_text(kind, json.loads(a))} {k}건" for a, k in c.items())
    if row:
        conn.execute("UPDATE curriculum SET title=?, evidence=?, next_check=?, state=?, proposed_answer=?, case_count=?,"
                     " updated_at=? WHERE id=?", (title, evidence, next_check, state, proposed, n, _now(), row["id"]))
    else:
        conn.execute("INSERT INTO curriculum(title, evidence, next_check, done_criteria, state, decision_kind, decision_key,"
                     " proposed_answer, case_count, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (title, evidence, next_check, "사용자 승인 후 룰북 규칙으로 전환", state, kind, key, proposed, n, _now()))


def approve(conn, curriculum_id: int) -> str:
    row = conn.execute("SELECT * FROM curriculum WHERE id=?", (curriculum_id,)).fetchone()
    if not row or row["state"] != "proposed":
        raise ValueError("승인할 수 있는 규칙 제안이 아닙니다")
    n = conn.execute("SELECT COUNT(*) FROM rulebook WHERE id LIKE 'L-%'").fetchone()[0] + 1
    rule_id = f"L-{n:03d}"
    label = row["title"].split(": ", 1)[-1]
    answer = json.loads(row["proposed_answer"])
    cases = [r["id"] for r in conn.execute(
        "SELECT id FROM decision_case WHERE decision_kind=? AND decision_key=? AND rule_id IS NULL",
        (row["decision_kind"], row["decision_key"]))]
    conn.execute(
        "INSERT INTO rulebook(id, scope, condition, action, basis, version, status, decision_kind, decision_key, answer,"
        " case_count, created_at) VALUES (?,?,?,?,?,?, 'active', ?,?,?,0,?)",
        (rule_id, "company:default", label, _answer_text(row["decision_kind"], answer),
         f"확정 사례 {len(cases)}건 (#{', #'.join(map(str, cases))}), 사용자 승인", "v1",
         row["decision_kind"], row["decision_key"], row["proposed_answer"], _now()))
    conn.execute("UPDATE curriculum SET state='done', rule_id=?, updated_at=? WHERE id=?", (rule_id, _now(), curriculum_id))
    return rule_id


def suspend(conn, rule_id: str):
    rule = conn.execute("SELECT * FROM rulebook WHERE id=?", (rule_id,)).fetchone()
    if not rule or rule["status"] != "active":
        raise ValueError("중지할 수 있는 규칙이 아닙니다")
    if not rule["decision_kind"]:
        raise ValueError("기본 규칙은 화면에서 중지할 수 없습니다(TRNK 결정 필요)")
    conn.execute("UPDATE rulebook SET status='suspended' WHERE id=?", (rule_id,))
    conn.execute("INSERT INTO curriculum(title, evidence, next_check, done_criteria, state, decision_kind, decision_key,"
                 " case_count, updated_at) VALUES (?,?,?,?, 'open', ?,?,0,?)",
                 (f"규칙 {rule_id} 중지됨: {rule['condition']}", f"규칙 적용 {rule['case_count']}건 후 중지",
                  "다시 묻는 동안 답을 모아 원인 확인", "원인 정리 후 재승인 또는 폐기",
                  rule["decision_kind"], rule["decision_key"], _now()))


def _answer_text(kind, answer) -> str:
    from . import contract
    if kind == "doc_kind":
        return contract.DOC_KIND_CHOICES.get(answer, str(answer))
    if kind == "item_map":
        return answer.get("label") if isinstance(answer, dict) else str(answer)
    return str(answer)


def overview(conn) -> dict:
    rules = [dict(r) for r in conn.execute("SELECT * FROM rulebook ORDER BY decision_kind IS NOT NULL, id")]
    cur = [dict(r) for r in conn.execute(
        "SELECT * FROM curriculum WHERE state<>'done' ORDER BY CASE state WHEN 'proposed' THEN 0 WHEN 'conflict' THEN 1"
        " WHEN 'open' THEN 2 ELSE 3 END, id")]
    asked = conn.execute("SELECT COUNT(*) FROM decision_case WHERE rule_id IS NULL").fetchone()[0]
    auto = conn.execute("SELECT COUNT(*) FROM decision_case WHERE rule_id IS NOT NULL").fetchone()[0]
    return {"rules": rules, "curriculum": cur, "promote_after": PROMOTE_AFTER,
            "stats": {"asked": asked, "auto": auto},
            "proposals": sum(1 for c in cur if c["state"] == "proposed")}
