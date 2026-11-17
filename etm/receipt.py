"""입고(→ 공장) 처리.

입고 자료의 품목을 어떤 기준정보에 연결할지(예: 'P10' → KPP 팔레트)는 프로그램이 정하지 않는다.
- 기준정보와 정확히 같으면 MASTER-EXACT 규칙으로 연결
- 승인된 학습 규칙이 있으면 그 규칙으로 연결
- 그 밖에는 사용자에게 묻는다(지난 답은 제안으로만 보여준다)

반영: 자사 품목 → 공장 재고 증가 / 임대품 → 소유자 그대로, 공장 위치로 이동.
대금(줄 돈)은 아직 규칙이 없어 만들지 않는다.
"""
from __future__ import annotations

import json

from . import learn
from .process import (LABEL, _blocked_paths, _lbl, describe_changes, is_resolved, jdump, line_indexes, load_event,
                      norm, now, open_checks, save_fields, _aliases)


def item_facts(fields, i) -> dict:
    sup = (fields.get("supplier") or {}).get("value") or (fields.get("customer") or {}).get("value") or ""
    return learn.facts_for("item_map", supplier=sup, item_text=fields[f"lines.{i}.item"]["value"])


def item_key(fields, i) -> str:
    return learn.case_key(item_facts(fields, i))


def _exact_item(conn, text):
    n = norm(text)
    for r in conn.execute("SELECT * FROM item"):
        if n and n in [norm(k) for k in (r["name"], r["code"] or "", *_aliases(r)) if k]:
            return r
    return None


def item_choices(conn):
    out = [{"choice": f"item:{r['id']}", "label": f"{r['name']} ({r['unit']})" + (" · 임대품" if r["kind"] == "rental" else "")}
           for r in conn.execute("SELECT * FROM item ORDER BY kind, id")]
    out.append({"choice": "none", "label": "재고와 무관 (기록만)"})
    return out


def required_checks(conn, ev, fields) -> list[dict]:
    checks: list[dict] = []

    def add(path, kind, msg, sig, rule=None, apps=None, **detail):
        checks.append({"path": path, "kind": kind, "message": msg, "signature": jdump(sig),
                       "rule_id": rule, "detail": detail, "apps": apps or []})

    for path, fld in fields.items():
        if path.startswith("_") or fld["source"] == "user" or fld.get("ref_source"):
            continue
        if "handwritten" in fld["flags"] or "uncertain" in fld["flags"]:
            why = "손글씨" if "handwritten" in fld["flags"] else "판독 불확실"
            add(path, "verify", f"{LABEL.get(_lbl(path), path)} 확인 ({why})", {"orig": fld["orig"], "raw": fld["raw"]},
                "HW-CONFIRM" if "handwritten" in fld["flags"] else "UNCERTAIN-FIELD",
                raw=fld["raw"], extracted=fld["orig"])

    if not line_indexes(fields):
        add("lines", "missing_lines", "입고 품목이 없습니다. 품목과 수량을 입력하세요", None)
    for i in line_indexes(fields):
        it, q = fields[f"lines.{i}.item"], fields[f"lines.{i}.qty"]
        if it["value"] is None:
            add(f"lines.{i}.item", "missing", f"{i + 1}번 품목명을 입력하세요", None)
        elif it.get("ref") is None and not it.get("record_only"):
            key = item_key(fields, i)
            label = f"{(fields.get('supplier') or {}).get('value') or '업체 미상'} 입고 자료의 품목 '{it['value']}'"
            row = _exact_item(conn, it["value"])
            if row:   # 기본 동작(code-default): 기준정보 이름·코드·별칭 정확 일치
                it.update(ref=row["id"], value=row["name"], ref_source="code:MASTER-EXACT")
            else:
                f = item_facts(fields, i)
                sup = f["supplier"]
                status, action, ver, basis = learn.decide(conn, event=ev, decision="item_map", facts=f)
                if status == "rule":
                    _set_item(conn, it, action, f"rule:{learn.version_label(ver)}")
                    learn.record_application(conn, event_id=ev["id"], version_id=ver["id"], target=f"lines.{i}.item",
                                             basis=basis, snapshot={"item": it["orig"], "supplier": sup},
                                             result="performed", reason=action.get("label") or action["choice"])
                    learn.record_case(conn, event_id=ev["id"], check_id=None, kind="item_map", key=key, label=label,
                                      answer={"choice": action["choice"], "label": action.get("label")}, actor="system",
                                      version_id=ver["id"], company_id=ev["company_id"])
                else:
                    apps = []
                    why = "활성 규칙 없음 — 사용자 확인"
                    if status == "conflict":
                        why = basis
                        apps = [{"version_id": v["id"], "basis": {"conflict": True}, "result": "held_conflict",
                                 "reason": basis} for v in action]
                    add(f"lines.{i}.item", "item_map", f"{label}은(는) 무엇입니까?", {"k": key}, None, apps=apps,
                        candidates=item_choices(conn), decision_key=key, decision_label=label,
                        suggestion=learn.suggestion(conn, "item_map", key), why=why)
        if q["value"] is None:
            add(f"lines.{i}.qty", "missing", f"{i + 1}번 품목 수량을 입력하세요", None)
    return checks


def _set_item(conn, fld, ans, source):
    if ans.get("choice") == "none":
        fld.pop("ref", None)
        fld["record_only"] = True
    else:
        rid = int(ans["choice"].split(":")[1])
        row = conn.execute("SELECT * FROM item WHERE id=?", (rid,)).fetchone()
        fld.update(ref=row["id"], value=row["name"])
        fld.pop("record_only", None)
    fld["ref_source"] = source


def apply_item_answer(conn, fields, chk, ans) -> dict:
    """item_map 응답 반영. 기록할 결정값을 돌려준다."""
    choice = ans.get("choice")
    label = next((c["label"] for c in json.loads(chk["detail"])["candidates"] if c["choice"] == choice), None)
    if label is None:
        raise ValueError("후보 중에서 선택하세요")
    decision = {"choice": choice, "label": label}
    _set_item(conn, fields[chk["path"]], decision, "user")
    return decision


def reconcile(conn, event_id) -> dict:
    ev, fields = load_event(conn, event_id)
    blocked = _blocked_paths(conn, event_id)
    t = now()
    changes = {"moves": [], "money": [], "obligations": []}
    save_fields(conn, event_id, fields, trade_date=fields["trade_date"]["value"], doc_no=fields["doc_no"]["value"])
    sup = (fields.get("supplier") or {}).get("value") or "업체 미상"
    src = f"supplier:{sup}"
    desired, applied, pending, skipped = {}, [], [], []
    for i in line_indexes(fields):
        it, q = fields[f"lines.{i}.item"], fields[f"lines.{i}.qty"]
        if it.get("record_only"):
            skipped.append(i)
            continue
        ok = (ev["status"] == "active" and it.get("ref") is not None and is_resolved(fields, f"lines.{i}.item", blocked)
              and is_resolved(fields, f"lines.{i}.qty", blocked))
        if not ok:
            pending.append(i)
            continue
        item = conn.execute("SELECT * FROM item WHERE id=?", (it["ref"],)).fetchone()
        ow = (fields.get(f"lines.{i}.owner") or {}).get("value")
        owner = ow or ("own" if item["kind"] == "product" else f"party:{item['owner_party_id']}")
        key = (item["id"], src, "factory", owner)
        desired[key] = desired.get(key, 0) + q["value"]
        applied.append(i)
    existing = {(r["item_id"], r["from_loc"], r["to_loc"], r["owner"]): r["q"] for r in conn.execute(
        "SELECT item_id, from_loc, to_loc, owner, SUM(qty) q FROM value_move WHERE event_id=?"
        " GROUP BY item_id, from_loc, to_loc, owner", (event_id,))}
    had = bool(existing)
    for key in set(desired) | set(existing):
        delta = desired.get(key, 0) - (existing.get(key) or 0)
        if abs(delta) > 1e-9:
            conn.execute("INSERT INTO value_move(event_id, item_id, from_loc, to_loc, owner, qty, reason, created_at)"
                         " VALUES (?,?,?,?,?,?,?,?)", (event_id, *key, delta, "revise" if had else "apply", t))
            changes["moves"].append({"item_id": key[0], "from": key[1], "to": key[2], "owner": key[3], "delta": delta})

    n_open = len(open_checks(conn, event_id))
    lines = line_indexes(fields)
    ob = {"review": ("open", f"확인 {n_open}건") if n_open else ("done", "")}
    if not lines:
        ob["stock_in"] = ("open", "품목 없음")
    elif pending:
        ob["stock_in"] = ("open", f"{len(applied)}/{len(lines) - len(skipped)}줄 반영, 보류 {', '.join(str(i + 1) for i in pending)}번")
    else:
        ob["stock_in"] = ("done", f"{len(applied)}줄 반영" + (f", 기록만 {len(skipped)}줄" if skipped else ""))
    current = {r["kind"]: r for r in conn.execute("SELECT * FROM obligation WHERE event_id=?", (event_id,))}
    for kind, (state, note) in ob.items():
        c = current.get(kind)
        if c is None:
            conn.execute("INSERT INTO obligation(event_id, kind, state, note, updated_at) VALUES (?,?,?,?,?)",
                         (event_id, kind, state, note, t))
        elif (c["state"], c["note"]) != (state, note):
            conn.execute("UPDATE obligation SET state=?, note=?, updated_at=? WHERE id=?", (state, note, t, c["id"]))
        else:
            continue
        changes["obligations"].append({"kind": kind, "state": state, "note": note})
    return changes


__all__ = ["required_checks", "reconcile", "apply_item_answer", "describe_changes"]
