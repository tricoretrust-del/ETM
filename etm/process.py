"""처리 엔진: 입력 → 대조·확인 → 반영(가치·돈·미결) → 이력.

핵심 아이디어
- 사건(event)은 '확정된 필드 값'을 가진다. 장부 효과(재고 이동·채권·채무·남은 처리)는
  그 값에서 계산되는 '목표 상태'다.
- reconcile() 은 목표 상태와 이미 기록된 효과를 비교해 **차이만** 기록한다.
  그래서 재수신·재확인·수정을 몇 번 해도 이중 반영이 생기지 않고(멱등),
  수정은 반대 부호의 조정 행으로 남아 이력이 보존된다.
- 불확실한 값(열린 확인 요청이 걸린 필드)에 의존하는 효과만 보류하고,
  독립적으로 확정된 부분은 바로 반영한다.
- 한 번의 처리(입력·확인·수정)는 하나의 DB 트랜잭션이다. 중간 실패 시 전부 롤백된다.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any, Callable

from . import contract

# 시험용 장애 주입 지점: reconcile 중간(재고 기록 후, 돈 기록 전)에 호출된다.
FAULT_HOOK: Callable[[], None] | None = None

OBLIGATION_LABEL = {
    "review": "확인 대기",
    "stock_out": "출하 재고 반영",
    "pallet_check": "팔레트 확인",
    "amount_confirm": "금액 확정",
    "collect": "수금",
    "freight_info": "운반 내역 확인",
    "freight_pay": "운반비 지급",
    "pallet_return": "팔레트 회수",
}


# --------------------------------------------------------------------------- utils
def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"\(주\)|㈜|주식회사|\(유\)|유한회사", "", s)
    return re.sub(r"[\s\-_.,()·]", "", s).lower()


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


class Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def log(conn, event_id, action, summary, detail=None):
    conn.execute("INSERT INTO history(event_id, at, action, summary, detail) VALUES (?,?,?,?,?)",
                 (event_id, now(), action, summary, jdump(detail or {})))


# --------------------------------------------------------------------------- master data
def _aliases(row) -> list[str]:
    try:
        return json.loads(row["aliases"] or "[]")
    except (TypeError, ValueError):
        return []


def match_party(conn, name: str):
    rows = conn.execute("SELECT * FROM party WHERE kind='customer'").fetchall()
    n = norm(name)
    for r in rows:
        if n and (n == norm(r["name"]) or n in [norm(a) for a in _aliases(r)]):
            return r, []
    return None, _candidates(n, rows, lambda r: [r["name"], *_aliases(r)], lambda r: {
        "choice": f"party:{r['id']}", "label": r["name"]})


def match_site(conn, party_id: int, address: str):
    rows = conn.execute("SELECT * FROM site WHERE party_id=?", (party_id,)).fetchall()
    n = norm(address)
    for r in rows:
        if n and n == norm(r["address"]):
            return r, []
    return None, _candidates(n, rows, lambda r: [r["address"]], lambda r: {
        "choice": f"site:{r['id']}", "label": r["address"]}, all_if_none=True)


def match_item(conn, text: str):
    rows = conn.execute("SELECT * FROM item WHERE kind='product'").fetchall()
    n = norm(text)
    for r in rows:
        keys = [r["name"], r["code"] or "", *_aliases(r)]
        if n and n in [norm(k) for k in keys if k]:
            return r, []
    return None, _candidates(n, rows, lambda r: [r["name"], r["code"] or "", *_aliases(r)], lambda r: {
        "choice": f"item:{r['id']}", "label": f"{r['name']} ({r['unit']})"}, all_if_none=True)


def _candidates(n, rows, keys_of, fmt, all_if_none=False, limit=5):
    scored = []
    for r in rows:
        best = 0.0
        for k in keys_of(r):
            k = norm(k)
            if not k:
                continue
            ratio = difflib.SequenceMatcher(None, n, k).ratio() if n else 0
            if n and (n in k or k in n):
                ratio = max(ratio, 0.8)
            best = max(best, ratio)
        scored.append((best, r))
    scored.sort(key=lambda x: -x[0])
    picked = [r for s, r in scored if s >= 0.4]
    if not picked and all_if_none:
        picked = [r for _, r in scored]
    return [fmt(r) for r in picked[:limit]]


def past_contacts(conn, party_id, site_id, exclude_event):
    """같은 거래처·현장의 과거 사건에서 확정된 담당자. 최근 순."""
    rows = conn.execute(
        "SELECT id, fields, trade_date FROM event WHERE party_id=? AND status='active' AND id<>?"
        " AND (? IS NULL OR site_id=?) ORDER BY COALESCE(trade_date, created_at) DESC, id DESC",
        (party_id, exclude_event, site_id, site_id)).fetchall()
    seen, out = set(), []
    for r in rows:
        f = json.loads(r["fields"])
        name = (f.get("contact.name") or {}).get("value")
        phone = (f.get("contact.phone") or {}).get("value")
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "phone": phone, "event_id": r["id"], "trade_date": r["trade_date"]})
    return out


def price_for(conn, item_id, party_id):
    r = conn.execute(
        "SELECT unit_price, tax FROM price WHERE item_id=? AND (party_id=? OR party_id IS NULL)"
        " ORDER BY party_id IS NULL, id DESC LIMIT 1", (item_id, party_id)).fetchone()
    return (r["unit_price"], r["tax"]) if r else (None, None)


def pallet_item(conn):
    return conn.execute("SELECT * FROM item WHERE kind='rental' ORDER BY id LIMIT 1").fetchone()


def factory_stock(conn, item_id) -> float:
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN to_loc='factory' THEN qty ELSE 0 END),0)"
        " - COALESCE(SUM(CASE WHEN from_loc='factory' THEN qty ELSE 0 END),0)"
        " FROM value_move WHERE item_id=? AND owner='own'", (item_id,)).fetchone()
    return r[0] or 0


# --------------------------------------------------------------------------- event helpers
def load_event(conn, event_id):
    ev = conn.execute("SELECT * FROM event WHERE id=?", (event_id,)).fetchone()
    if not ev:
        raise KeyError(f"event {event_id} 없음")
    return ev, json.loads(ev["fields"])


def save_fields(conn, event_id, fields, **cols):
    sets = ["fields=?", "updated_at=?"] + [f"{k}=?" for k in cols]
    conn.execute(f"UPDATE event SET {', '.join(sets)} WHERE id=?",
                 (jdump(fields), now(), *cols.values(), event_id))


def line_indexes(fields) -> list[int]:
    return sorted({int(p.split(".")[1]) for p in fields if p.startswith("lines.")})


def open_checks(conn, event_id):
    return conn.execute("SELECT * FROM check_item WHERE event_id=? AND state='open' ORDER BY id",
                        (event_id,)).fetchall()


def _blocked_paths(conn, event_id) -> set[str]:
    return {r["path"] for r in open_checks(conn, event_id)}


def is_resolved(fields, path, blocked) -> bool:
    fld = fields.get(path)
    if not fld or fld["value"] is None:
        return False
    return not any(b == path or b.startswith(path + ".") or path.startswith(b + ".") for b in blocked)


# --------------------------------------------------------------------------- analysis
def required_checks(conn, ev, fields) -> list[dict]:
    """현재 필드 상태에서 사용자에게 물어야 할 것만 계산한다. 자동 확정 가능한 것은 fields 에 반영."""
    checks: list[dict] = []

    def add(path, kind, msg, sig, rule=None, **detail):
        checks.append({"path": path, "kind": kind, "message": msg, "signature": jdump(sig),
                       "rule_id": rule, "detail": detail})

    # 1) 손글씨·불확실 표시 필드
    for path, fld in fields.items():
        if fld["source"] == "user" or fld.get("ref_source") == "user":
            continue
        if "handwritten" in fld["flags"] or "uncertain" in fld["flags"]:
            rule = "HW-CONFIRM" if "handwritten" in fld["flags"] else "UNCERTAIN-FIELD"
            why = "손글씨" if "handwritten" in fld["flags"] else "판독 불확실"
            add(path, "verify", f"{LABEL.get(_lbl(path), path)} 확인 ({why})",
                {"orig": fld["orig"], "raw": fld["raw"]}, rule,
                raw=fld["raw"], extracted=fld["orig"], source_ref=ev["id"])

    # 2) 거래처
    cust = fields["customer"]
    if cust["value"] is None:
        add("customer", "missing", "거래처를 입력하세요", None)
    elif cust.get("ref") is None:
        row, cands = match_party(conn, cust["value"])
        if row:
            cust.update(ref=row["id"], value=row["name"])
        else:
            add("customer", "match", f"거래처 '{cust['value']}' 가 기존 기록에 없습니다",
                {"v": cust["orig"]}, "NO-OVERWRITE", candidates=cands, input=cust["value"], allow_new=True)

    # 3) 현장 (거래처 확정 후)
    site = fields["site"]
    if cust.get("ref") is not None and site["value"] is not None and site.get("ref") is None:
        row, cands = match_site(conn, cust["ref"], site["value"])
        if row:
            site.update(ref=row["id"], value=row["address"])
        else:
            add("site", "match", f"현장 '{site['value']}' 이(가) 이 거래처의 기존 현장에 없습니다",
                {"v": site["orig"], "p": cust["ref"]}, "NO-OVERWRITE",
                candidates=cands, input=site["value"], allow_new=True)

    # 4) 담당자: 과거 기록과 대조. 과거값으로 자동 덮어쓰지 않는다.
    cn = fields["contact.name"]
    if cust.get("ref") is not None and cn["source"] != "user":
        past = past_contacts(conn, cust["ref"], site.get("ref"), ev["id"])
        names = [p["name"] for p in past]
        if cn["value"] is None and past:
            add("contact.name", "contact", "담당자가 비어 있습니다. 기존 담당자 중 선택하거나 입력하세요",
                {"v": None, "past": names}, "NO-OVERWRITE", candidates=past, input=None)
        elif cn["value"] is not None and past and cn["value"] not in names:
            add("contact.name", "contact",
                f"담당자 '{cn['value']}' 은(는) 이 현장의 기존 담당자({', '.join(names)})와 다릅니다",
                {"v": cn["orig"], "past": names}, "NO-OVERWRITE",
                candidates=past, input={"name": cn["value"], "phone": fields["contact.phone"]["value"]})

    # 5) 품목 줄
    if not line_indexes(fields):
        add("lines", "missing_lines", "출하 품목이 없습니다. 품목과 수량을 입력하세요", None)
    for i in line_indexes(fields):
        it, q, u = fields[f"lines.{i}.item"], fields[f"lines.{i}.qty"], fields[f"lines.{i}.unit"]
        if it["value"] is None:
            add(f"lines.{i}.item", "missing", f"{i + 1}번 품목명을 입력하세요", None)
        elif it.get("ref") is None:
            row, cands = match_item(conn, it["value"])
            if row:
                it.update(ref=row["id"], value=row["name"])
            else:
                add(f"lines.{i}.item", "match", f"{i + 1}번 품목 '{it['value']}' 이(가) 취급 품목과 맞지 않습니다",
                    {"v": it["orig"]}, "NO-OVERWRITE", candidates=cands, input=it["value"], allow_new=True)
        if q["value"] is None:
            add(f"lines.{i}.qty", "missing", f"{i + 1}번 품목 수량을 입력하세요", None)
        if it.get("ref") is not None:
            item = conn.execute("SELECT * FROM item WHERE id=?", (it["ref"],)).fetchone()
            if u["value"] and norm(u["value"]) != norm(item["unit"]) and u["source"] != "user":
                add(f"lines.{i}.unit", "unit", f"{i + 1}번 단위 '{u['value']}' 가 품목 기본 단위 '{item['unit']}' 와 다릅니다",
                    {"v": u["orig"], "item": item["id"]}, "STOCK-WARN",
                    item_unit=item["unit"], input=u["value"])
            if q["value"] is not None:
                applied = conn.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM value_move WHERE event_id=? AND item_id=? AND owner='own'",
                    (ev["id"], item["id"])).fetchone()[0]
                avail = factory_stock(conn, item["id"]) + applied
                needed = sum(fields[f"lines.{j}.qty"]["value"] or 0 for j in line_indexes(fields)
                             if fields[f"lines.{j}.item"].get("ref") == item["id"])
                if needed > avail:
                    add(f"lines.{i}.qty", "stock", f"{item['name']} 재고 부족: 현재 {avail:g}{item['unit']}, 출하 {needed:g}{item['unit']}",
                        {"item": item["id"], "need": needed}, "STOCK-WARN", available=avail, needed=needed)

    # 6) 팔레트 더블체크 (송장 vs 상차자 메시지)
    pi, pm = fields["pallets.invoice"], fields["pallets.message"]
    pc = fields.get("pallets.confirmed")
    if pc is None or pc["source"] != "user":
        if pi["value"] is not None and pm["value"] is not None and pi["value"] == pm["value"] \
                and not ({"handwritten", "uncertain"} & (set(pi["flags"]) | set(pm["flags"]))):
            fields["pallets.confirmed"] = {"value": pi["value"], "orig": pi["value"], "raw": None,
                                           "flags": [], "source": "double_check"}
        elif pi["value"] is not None or pm["value"] is not None:
            fields.pop("pallets.confirmed", None)
            kind = "pallet_mismatch" if (pi["value"] is not None and pm["value"] is not None) else "pallet_missing"
            msg = (f"팔레트 수량 불일치: 송장 {pi['value']} / 상차 메시지 {pm['value']}" if kind == "pallet_mismatch"
                   else f"팔레트 더블체크 자료 부족: 송장 {_fmt(pi['value'])} / 상차 메시지 {_fmt(pm['value'])}")
            add("pallets.confirmed", kind, msg, {"i": pi["orig"], "m": pm["orig"]}, "PALLET-DOUBLE",
                invoice=pi["value"], message=pm["value"], deferrable=True)
        else:
            fields.pop("pallets.confirmed", None)
    # 같은 필드에 '판독 확인'과 '후보 선택'이 겹치면 후보 선택 한 번으로 묻는다(질문 수 최소화)
    matched = {c["path"] for c in checks if c["kind"] == "match"}
    return [c for c in checks if not (c["kind"] == "verify" and c["path"] in matched)]


def _fmt(v):
    return "없음" if v is None else v


def _lbl(path):
    return re.sub(r"\.\d+\.", ".*.", path)


LABEL = {
    "doc_no": "전표번호", "trade_date": "거래일", "customer": "거래처", "site": "현장",
    "contact.name": "담당자", "contact.phone": "담당자 연락처", "vehicle_no": "차량번호",
    "driver_phone": "기사전화", "lines.*.item": "품목", "lines.*.qty": "수량", "lines.*.unit": "단위",
    "pallets.invoice": "송장 팔레트 수량", "pallets.message": "상차 메시지 팔레트 수량",
    "transport.carrier": "운송업체", "transport.fee": "운반비", "pallets.confirmed": "팔레트",
}


def sync_checks(conn, ev, fields) -> list[dict]:
    """필요한 확인 목록과 저장된 확인 요청을 맞춘다. 이미 답한 같은 질문은 다시 묻지 않는다."""
    identity_open = conn.execute(
        "SELECT 1 FROM check_item WHERE event_id=? AND kind='identity' AND state='open'", (ev["id"],)).fetchone()
    needed = [] if identity_open else required_checks(conn, ev, fields)
    existing = conn.execute("SELECT * FROM check_item WHERE event_id=? AND kind<>'identity'",
                            (ev["id"],)).fetchall()
    by_key = {(r["path"], r["kind"], r["signature"]): r for r in existing}
    keep = set()
    created = []
    for c in needed:
        key = (c["path"], c["kind"], c["signature"])
        r = by_key.get(key)
        if r and r["state"] == "answered":
            keep.add(r["id"])
            continue
        if r and r["state"] == "open":
            keep.add(r["id"])
            conn.execute("UPDATE check_item SET message=?, detail=? WHERE id=?",
                         (c["message"], jdump(c["detail"]), r["id"]))
            continue
        cur = conn.execute(
            "INSERT INTO check_item(event_id, path, kind, signature, rule_id, message, detail, state, created_at)"
            " VALUES (?,?,?,?,?,?,?,'open',?)",
            (ev["id"], c["path"], c["kind"], c["signature"], c["rule_id"], c["message"], jdump(c["detail"]), now()))
        keep.add(cur.lastrowid)
        created.append(c)
    for r in existing:
        if r["state"] == "open" and r["id"] not in keep:
            conn.execute("UPDATE check_item SET state='void', answered_at=? WHERE id=?", (now(), r["id"]))
    return created


# --------------------------------------------------------------------------- reconcile
def reconcile(conn, event_id) -> dict:
    """목표 상태와 기록된 효과의 차이만 기록한다(멱등)."""
    ev, fields = load_event(conn, event_id)
    blocked = _blocked_paths(conn, event_id)
    identity_open = conn.execute(
        "SELECT 1 FROM check_item WHERE event_id=? AND kind='identity' AND state='open'", (event_id,)).fetchone()
    active = ev["status"] == "active" and not identity_open
    t = now()
    changes: dict[str, list] = {"moves": [], "money": [], "obligations": []}

    cust_ref = fields["customer"].get("ref") if is_resolved(fields, "customer", blocked) else None
    site_ref = fields["site"].get("ref") if is_resolved(fields, "site", blocked) else None
    save_fields(conn, event_id, fields, party_id=cust_ref, site_id=site_ref,
                trade_date=fields["trade_date"]["value"], doc_no=fields["doc_no"]["value"])

    # ---- 가치: 목표 이동
    desired: dict[tuple, float] = {}
    lines = line_indexes(fields)
    applied_lines, pending_lines = [], []
    for i in lines:
        it, q = fields[f"lines.{i}.item"], fields[f"lines.{i}.qty"]
        ok = (active and it.get("ref") is not None and is_resolved(fields, f"lines.{i}.item", blocked)
              and is_resolved(fields, f"lines.{i}.qty", blocked)
              and not any(b.startswith(f"lines.{i}.") for b in blocked))
        if ok:
            key = (it["ref"], "factory", "shipped", "own")
            desired[key] = desired.get(key, 0) + q["value"]
            applied_lines.append(i)
        else:
            pending_lines.append(i)

    pal = pallet_item(conn)
    pallet_qty = None
    if active and pal and is_resolved(fields, "pallets.confirmed", blocked) and (site_ref or cust_ref):
        pallet_qty = fields["pallets.confirmed"]["value"]
        dest = f"site:{site_ref}" if site_ref else f"party:{cust_ref}"
        if pallet_qty:
            desired[(pal["id"], "factory", dest, f"party:{pal['owner_party_id']}")] = pallet_qty

    existing = {(r["item_id"], r["from_loc"], r["to_loc"], r["owner"]): r["q"] for r in conn.execute(
        "SELECT item_id, from_loc, to_loc, owner, SUM(qty) q FROM value_move WHERE event_id=?"
        " GROUP BY item_id, from_loc, to_loc, owner", (event_id,))}
    had_any = bool(existing)
    for key in set(desired) | set(existing):
        delta = desired.get(key, 0) - (existing.get(key) or 0)
        if abs(delta) > 1e-9:
            conn.execute(
                "INSERT INTO value_move(event_id, item_id, from_loc, to_loc, owner, qty, reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)", (event_id, *key, delta, "revise" if had_any else "apply", t))
            changes["moves"].append({"item_id": key[0], "from": key[1], "to": key[2], "owner": key[3], "delta": delta})

    if FAULT_HOOK:
        FAULT_HOOK()

    # ---- 돈: 받을 돈(물품)
    want: dict[tuple, dict] = {}
    if active and cust_ref and applied_lines:
        amount, taxes, basis = 0, set(), []
        for i in applied_lines:
            it, q = fields[f"lines.{i}.item"], fields[f"lines.{i}.qty"]
            up, tax = price_for(conn, it["ref"], cust_ref)
            basis.append({"line": i, "item_id": it["ref"], "qty": q["value"], "unit_price": up})
            if up is None:
                amount = None
            elif amount is not None:
                amount += round(up * q["value"])
            taxes.add(tax or "unknown")
        if pending_lines:
            amount = None
        want[("receivable", "goods")] = {
            "party_id": cust_ref, "party_name": fields["customer"]["value"], "amount": amount,
            "tax": taxes.pop() if len(taxes) == 1 else "unknown",
            "basis": {"lines": basis, "pending_lines": pending_lines, "price_ref": fields.get("_price_ref")}}
    # ---- 돈: 줄 돈(운반비). 거래 상대가 확인된 경우만 만든다.
    if active and "transport.carrier" in fields and is_resolved(fields, "transport.carrier", blocked):
        carrier = fields["transport.carrier"]["value"]
        fee = fields["transport.fee"]["value"] if is_resolved(fields, "transport.fee", blocked) else None
        sup = conn.execute("SELECT id FROM party WHERE kind='supplier'").fetchall()
        sid = next((r["id"] for r in sup if norm(conn.execute("SELECT name FROM party WHERE id=?",
                                                               (r["id"],)).fetchone()[0]) == norm(carrier)), None)
        want[("payable", "freight")] = {"party_id": sid, "party_name": carrier,
                                        "amount": int(fee) if fee is not None else None,
                                        "tax": "unknown", "basis": {"source": "transport"}}

    claims = {(r["kind"], r["role"]): r for r in conn.execute(
        "SELECT * FROM money_claim WHERE event_id=?", (event_id,))}
    for key in set(want) | set(claims):
        w, c = want.get(key), claims.get(key)
        if w and not c:
            cur = conn.execute(
                "INSERT INTO money_claim(event_id, kind, role, party_id, party_name, amount, tax, state, basis, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'active',?,?)",
                (event_id, key[0], key[1], w["party_id"], w["party_name"], w["amount"], w["tax"], jdump(w["basis"]), t))
            conn.execute("INSERT INTO money_log(claim_id, old_amount, new_amount, reason, at) VALUES (?,?,?,?,?)",
                         (cur.lastrowid, None, w["amount"], "발생", t))
            changes["money"].append({"kind": key[0], "role": key[1], "old": None, "new": w["amount"], "state": "new"})
        elif w and c:
            if (c["amount"] != w["amount"] or c["state"] != "active" or c["party_id"] != w["party_id"]
                    or c["party_name"] != w["party_name"]):
                conn.execute(
                    "UPDATE money_claim SET party_id=?, party_name=?, amount=?, tax=?, state='active', basis=?, updated_at=? WHERE id=?",
                    (w["party_id"], w["party_name"], w["amount"], w["tax"], jdump(w["basis"]), t, c["id"]))
                conn.execute("INSERT INTO money_log(claim_id, old_amount, new_amount, reason, at) VALUES (?,?,?,?,?)",
                             (c["id"], c["amount"], w["amount"], "수정", t))
                changes["money"].append({"kind": key[0], "role": key[1], "old": c["amount"], "new": w["amount"],
                                         "state": "changed"})
            else:
                conn.execute("UPDATE money_claim SET basis=? WHERE id=?", (jdump(w["basis"]), c["id"]))
        elif c and c["state"] == "active":
            conn.execute("UPDATE money_claim SET state='void', updated_at=? WHERE id=?", (t, c["id"]))
            conn.execute("INSERT INTO money_log(claim_id, old_amount, new_amount, reason, at) VALUES (?,?,?,?,?)",
                         (c["id"], c["amount"], None, "근거 소멸(취소)", t))
            changes["money"].append({"kind": key[0], "role": key[1], "old": c["amount"], "new": None, "state": "void"})

    # ---- 미결·완료
    n_open = len(open_checks(conn, event_id))
    recv = want.get(("receivable", "goods"))
    pay = want.get(("payable", "freight"))
    pallet_out = conn.execute(
        "SELECT COALESCE(SUM(qty),0) FROM value_move WHERE event_id=? AND owner LIKE 'party:%'", (event_id,)).fetchone()[0]
    has_pallet_data = fields["pallets.invoice"]["value"] is not None or fields["pallets.message"]["value"] is not None
    ob: dict[str, tuple[str, str] | None] = {}
    if ev["status"] != "active":
        ob = {k: None for k in OBLIGATION_LABEL}
    else:
        ob["review"] = ("open", f"확인 {n_open}건") if n_open else ("done", "")
        if not lines:
            ob["stock_out"] = ("open", "품목 없음")
        elif pending_lines:
            ob["stock_out"] = ("open", f"{len(applied_lines)}/{len(lines)}줄 반영, 보류 {', '.join(str(i + 1) for i in pending_lines)}번")
        else:
            ob["stock_out"] = ("done", f"{len(lines)}줄 반영")
        if pallet_qty is not None:
            src = fields["pallets.confirmed"]["source"]
            ob["pallet_check"] = ("done", f"{pallet_qty:g}개 ({'송장·메시지 일치' if src == 'double_check' else '사용자 확인'})")
        elif has_pallet_data or pal:
            ob["pallet_check"] = ("open", "송장·상차 메시지 대조 필요")
        else:
            ob["pallet_check"] = None
        if recv:
            ob["amount_confirm"] = ("done", f"{recv['amount']:,}원") if recv["amount"] is not None else (
                "open", "단가 미확정" if not pending_lines else "보류 품목 있음")
            ob["collect"] = ("open", f"{recv['amount']:,}원" if recv["amount"] is not None else "금액 미확정")
        else:
            ob["amount_confirm"] = ("open", "거래처·품목 확정 필요") if lines else None
            ob["collect"] = None
        if "transport.carrier" in fields:
            missing = [LABEL[p] for p in ("transport.carrier", "transport.fee") if not is_resolved(fields, p, blocked)]
            ob["freight_info"] = ("open", ", ".join(missing) + " 미확정") if missing else ("done", "")
        else:
            ob["freight_info"] = None
        ob["freight_pay"] = ("open", f"{pay['party_name']} {pay['amount']:,}원" if pay["amount"] is not None
                             else f"{pay['party_name']} 금액 미확정") if pay else None
        ob["pallet_return"] = ("open", f"{pallet_out:g}개 현장 체류") if pallet_out > 0 else None

    current = {r["kind"]: r for r in conn.execute("SELECT * FROM obligation WHERE event_id=?", (event_id,))}
    for kind, val in ob.items():
        state, note = val if val else ("n/a", "")
        c = current.get(kind)
        if c is None and val is None:
            continue
        if c is None:
            conn.execute("INSERT INTO obligation(event_id, kind, state, note, updated_at) VALUES (?,?,?,?,?)",
                         (event_id, kind, state, note, t))
            changes["obligations"].append({"kind": kind, "state": state, "note": note})
        elif c["state"] != state or c["note"] != note:
            conn.execute("UPDATE obligation SET state=?, note=?, updated_at=? WHERE id=?", (state, note, t, c["id"]))
            changes["obligations"].append({"kind": kind, "state": state, "note": note, "was": c["state"]})
    return changes


# --------------------------------------------------------------------------- summaries
def item_name(conn, item_id):
    r = conn.execute("SELECT name, unit FROM item WHERE id=?", (item_id,)).fetchone()
    return (r["name"], r["unit"]) if r else (f"#{item_id}", "")


def event_title(conn, event_id) -> str:
    ev, f = load_event(conn, event_id)
    cust = f["customer"]["value"] or "거래처 미확정"
    parts = []
    for i in line_indexes(f):
        it, q = f[f"lines.{i}.item"]["value"], f[f"lines.{i}.qty"]["value"]
        parts.append(f"{it or '품목?'} {_fmt_qty(q)}")
    return f"출하 {cust} · {', '.join(parts) or '품목 없음'}"


def _fmt_qty(q):
    return "수량?" if q is None else f"{q:g}"


def describe_changes(conn, changes) -> str:
    bits = []
    for m in changes["moves"]:
        name, unit = item_name(conn, m["item_id"])
        if m["owner"] == "own":
            bits.append(f"재고 {name} {-m['delta']:+g}{unit}")
        else:
            bits.append(f"팔레트 {m['delta']:+g} → 현장")
    for m in changes["money"]:
        label = "받을 돈" if m["kind"] == "receivable" else "줄 돈(운반비)"
        if m["state"] == "void":
            bits.append(f"{label} 취소")
        elif m["new"] is None:
            bits.append(f"{label} 금액 미확정")
        elif m["old"] is None:
            bits.append(f"{label} {m['new']:,}원")
        else:
            bits.append(f"{label} {m['old']:,}→{m['new']:,}원")
    return ", ".join(bits)


def remaining(conn, event_id) -> list[dict]:
    return [{"kind": r["kind"], "label": OBLIGATION_LABEL[r["kind"]], "note": r["note"]}
            for r in conn.execute("SELECT * FROM obligation WHERE event_id=? AND state='open' ORDER BY id",
                                  (event_id,))]


# --------------------------------------------------------------------------- public operations
def intake(conn, payload: dict) -> dict:
    """새 자료 접수. 반환: {event_id, outcome, checks, summary}"""
    header, fields = contract.normalize(payload)
    h = contract.payload_hash(payload)
    with Tx(conn):
        # (a) 같은 내용 재수신 → 기존 사건에 증빙만 연결, 추가 반영 없음
        same = conn.execute(
            "SELECT i.event_id FROM intake i JOIN event e ON e.id=i.event_id"
            " WHERE i.payload_hash=? AND e.status='active' ORDER BY i.id LIMIT 1", (h,)).fetchone()
        if same:
            ev_id = same["event_id"]
            conn.execute("INSERT INTO intake(input_id, channel, source_ref, received_at, payload, payload_hash, event_id, outcome)"
                         " VALUES (?,?,?,?,?,?,?,'re_receive')",
                         (header["input_id"], header["channel"], header["source_ref"], now(), jdump(payload), h, ev_id))
            log(conn, ev_id, "re_receive", f"재수신 — 이미 기록된 {event_title(conn, ev_id)} · 추가 반영 없음")
            return {"event_id": ev_id, "outcome": "re_receive", "checks": _checks_out(conn, ev_id)}

        if header.get("price_ref"):
            fields["_price_ref"] = header["price_ref"]
        t = now()
        cur = conn.execute(
            "INSERT INTO event(kind, doc_type, doc_no, trade_date, created_at, updated_at, status, fields)"
            " VALUES ('shipment', ?, ?, ?, ?, ?, 'active', ?)",
            (header["doc_type"], fields["doc_no"]["value"], fields["trade_date"]["value"], t, t, jdump(fields)))
        ev_id = cur.lastrowid

        # (b) 같은 전표번호의 다른 내용 → 기존 사건 수정 후보
        # (c) 다른 번호지만 거래처·날짜·품목·수량이 같은 사건 → 업무상 중복 후보
        outcome, dup = "new", None
        doc_no = fields["doc_no"]["value"]
        cust_n = norm(fields["customer"]["value"])
        for r in conn.execute("SELECT * FROM event WHERE status='active' AND id<>? AND doc_type=?",
                              (ev_id, header["doc_type"])).fetchall():
            rf = json.loads(r["fields"])
            r_cust = norm(rf["customer"]["value"])
            if doc_no and r["doc_no"] == doc_no and (not cust_n or not r_cust or cust_n == r_cust):
                outcome, dup = "revision_candidate", r
                break
            if (cust_n and cust_n == r_cust and fields["trade_date"]["value"]
                    and fields["trade_date"]["value"] == r["trade_date"] and _line_sig(conn, fields) == _line_sig(conn, rf)
                    and line_indexes(fields)):
                outcome, dup = "duplicate_candidate", r
        if dup is not None:
            diffs = _diff_fields(json.loads(dup["fields"]), fields)
            msg = (f"같은 전표번호({doc_no})의 기존 기록이 있습니다. 내용이 다릅니다: "
                   + ", ".join(d["label"] for d in diffs) if outcome == "revision_candidate"
                   else f"거래처·날짜·품목·수량이 같은 기존 기록(전표 {dup['doc_no'] or '없음'})이 있습니다. 같은 출하입니까?")
            conn.execute(
                "INSERT INTO check_item(event_id, path, kind, signature, rule_id, message, detail, state, created_at)"
                " VALUES (?, '_identity', 'identity', ?, NULL, ?, ?, 'open', ?)",
                (ev_id, jdump({"other": dup["id"]}), msg,
                 jdump({"other_event": dup["id"], "other_title": event_title(conn, dup["id"]), "mode": outcome,
                        "diffs": diffs}), now()))

        conn.execute("INSERT INTO intake(input_id, channel, source_ref, received_at, payload, payload_hash, event_id, outcome)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (header["input_id"], header["channel"], header["source_ref"], now(), jdump(payload), h, ev_id, outcome))
        ev, fields = load_event(conn, ev_id)
        sync_checks(conn, ev, fields)
        save_fields(conn, ev_id, fields)
        changes = reconcile(conn, ev_id)
        eff = describe_changes(conn, changes)
        log(conn, ev_id, "intake", f"{'스캔' if header['channel'] == 'scan' else '직접입력'} 접수 · {event_title(conn, ev_id)}"
            + (f" · {eff}" if eff else ""), {"changes": changes, "input_id": header["input_id"]})
        return {"event_id": ev_id, "outcome": outcome, "checks": _checks_out(conn, ev_id)}


def _line_sig(conn, fields):
    """품목(가능하면 기준정보 ID로 맞춘 값)·수량 묶음. 업무상 중복 후보 판별용."""
    sig = []
    for i in line_indexes(fields):
        it = fields[f"lines.{i}.item"]
        key = it.get("ref")
        if key is None and it["value"]:
            row, _ = match_item(conn, it["value"])
            key = row["id"] if row else norm(it["value"])
        sig.append((str(key), fields[f"lines.{i}.qty"]["value"] or 0))
    return sorted(sig)


def _diff_fields(old, new):
    out = []
    for p in sorted(set(old) | set(new)):
        if p.startswith("_") or p == "pallets.confirmed":
            continue
        a, b = (old.get(p) or {}).get("value"), (new.get(p) or {}).get("value")
        if a != b and norm(str(a)) != norm(str(b)):
            out.append({"path": p, "label": LABEL.get(_lbl(p), p), "old": a, "new": b})
    return out


def answer(conn, event_id: int, answers: dict[str, dict]) -> dict:
    """확인 요청에 대한 응답 반영. answers = {check_id: {"choice":..., "value":...}}
    응답한 것만 반영하고, 응답하지 않은 확인은 그대로 남긴다."""
    with Tx(conn):
        ev, fields = load_event(conn, event_id)
        confirmed = []
        merged_into = None
        for cid, ans in answers.items():
            chk = conn.execute("SELECT * FROM check_item WHERE id=? AND event_id=? AND state='open'",
                               (int(cid), event_id)).fetchone()
            if not chk:
                continue
            before = (fields.get(chk["path"]) or {}).get("value")
            result = _apply_answer(conn, ev, fields, chk, ans or {})
            if result == "merged":
                merged_into = json.loads(chk["detail"])["other_event"]
            conn.execute("UPDATE check_item SET state='answered', answer=?, answered_at=? WHERE id=?",
                         (jdump(ans), now(), chk["id"]))
            after = (fields.get(chk["path"]) or {}).get("value")
            conn.execute("INSERT INTO confirmed_case(event_id, check_id, path, before_value, after_value, at)"
                         " VALUES (?,?,?,?,?,?)", (event_id, chk["id"], chk["path"], jdump(before), jdump(after), now()))
            confirmed.append(f"{LABEL.get(_lbl(chk['path']), chk['path'] if chk['path'] != '_identity' else '동일 여부')}"
                             + (f" {after}" if after is not None and chk["kind"] != "identity" else ""))
        save_fields(conn, event_id, fields)
        if merged_into:
            conn.execute("UPDATE event SET status='merged', merged_into=? WHERE id=?", (merged_into, event_id))
            changes = reconcile(conn, event_id)
            log(conn, event_id, "merge", f"기존 기록 #{merged_into}에 연결 — 별도 반영 없음")
            return {"event_id": merged_into, "merged_from": event_id, "checks": _checks_out(conn, merged_into)}
        ev, fields = load_event(conn, event_id)
        sync_checks(conn, ev, fields)
        save_fields(conn, event_id, fields)
        changes = reconcile(conn, event_id)
        eff = describe_changes(conn, changes)
        if confirmed or eff:
            log(conn, event_id, "confirm", f"확인 {', '.join(confirmed)}" + (f" · {eff}" if eff else ""),
                {"changes": changes})
        return {"event_id": event_id, "checks": _checks_out(conn, event_id)}


def _apply_answer(conn, ev, fields, chk, ans):
    kind, path = chk["kind"], chk["path"]
    detail = json.loads(chk["detail"])
    choice, value = ans.get("choice"), ans.get("value")

    def set_user(p, v):
        fld = fields.setdefault(p, {"value": None, "orig": None, "raw": None, "flags": [], "source": "user"})
        fld["value"] = v
        fld["source"] = "user"

    if kind == "identity":
        if choice == "same":
            other = detail["other_event"]
            if detail["mode"] == "revision_candidate":
                _merge_revision(conn, other, fields, ev["id"])
            return "merged"
        return None  # separate: 별개 사건으로 진행

    if kind in ("verify", "missing"):
        if value is None and choice == "ok":
            value = fields[path]["value"]
            if value is None:  # 승인 대상 값이 없으면 임의로 복원하지 않는다
                raise ValueError("확인할 값이 없습니다. 값을 입력하세요")
        if path.endswith(".qty") or path.startswith("pallets.") or path == "transport.fee":
            value = contract._num_or_none(value, path, errs := [])
            if errs:
                raise ValueError(errs[0])
        set_user(path, value)
        if path in ("customer", "site") or path.endswith(".item"):
            fields[path].pop("ref", None)
            fields[path]["ref_source"] = "user"
        return None

    if kind == "match":
        fld = fields[path]
        if choice and choice.split(":")[0] in ("party", "site", "item"):
            table, rid = choice.split(":")
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (int(rid),)).fetchone()
            fld["ref"] = row["id"]
            fld["value"] = row["address"] if table == "site" else row["name"]
        elif choice == "new":
            name = (value or fld["value"] or "").strip()
            if not name:
                raise ValueError("새 값의 이름이 필요합니다")
            if path == "customer":
                rid = conn.execute("INSERT INTO party(name, kind) VALUES (?, 'customer')", (name,)).lastrowid
            elif path == "site":
                rid = conn.execute("INSERT INTO site(party_id, address) VALUES (?, ?)",
                                   (fields["customer"]["ref"], name)).lastrowid
            else:
                i = path.split(".")[1]
                unit = ans.get("unit") or fields[f"lines.{i}.unit"]["value"]
                if not unit:
                    raise ValueError("새 품목의 단위가 필요합니다")
                rid = conn.execute("INSERT INTO item(name, unit, kind) VALUES (?, ?, 'product')", (name, unit)).lastrowid
            fld["ref"], fld["value"] = rid, name
        else:
            raise ValueError("후보를 선택하거나 새 값을 확정하세요")
        fld["ref_source"] = "user"
        return None

    if kind == "contact":
        if choice is not None and str(choice).isdigit():
            c = detail["candidates"][int(choice)]
            set_user("contact.name", c["name"])
            set_user("contact.phone", c.get("phone"))
        elif choice == "keep":
            if fields["contact.name"]["value"] is None:
                raise ValueError("유지할 담당자 값이 없습니다")
            set_user("contact.name", fields["contact.name"]["value"])
        else:
            v = value or {}
            if not v.get("name"):
                raise ValueError("담당자 이름이 필요합니다")
            set_user("contact.name", v["name"])
            if v.get("phone"):
                set_user("contact.phone", v["phone"])
        return None

    if kind == "unit":
        set_user(path, detail["item_unit"] if choice == "item_unit" else (value or fields[path]["value"]))
        return None

    if kind == "stock":
        if value is not None:
            set_user(path, contract._num_or_none(value, path, []))
        return None  # 'ok' = 재고 부족을 알고 진행

    if kind in ("pallet_mismatch", "pallet_missing"):
        if choice == "invoice":
            value = detail["invoice"]
        elif choice == "message":
            value = detail["message"]
        v = contract._num_or_none(value, path, errs := [])
        if v is None or errs:
            raise ValueError("확정할 팔레트 수량을 입력하세요")
        fields["pallets.confirmed"] = {"value": v, "orig": None, "raw": None, "flags": [], "source": "user"}
        return None
    raise ValueError(f"알 수 없는 확인 종류: {kind}")


def _merge_revision(conn, other_id, new_fields, from_event):
    """같은 사건의 새 자료: 달라진 필드만 기존 사건에 반영하고 다시 계산한다."""
    _, old = load_event(conn, other_id)
    diffs = _diff_fields(old, new_fields)
    for p in list(old):
        if p.startswith("lines.") and p not in new_fields:
            old.pop(p)
    for d in diffs:
        nf = dict(new_fields.get(d["path"]) or {"value": None, "orig": None, "raw": None, "flags": [], "source": "input"})
        nf.pop("ref", None)
        old[d["path"]] = nf
    if any(d["path"].startswith("pallets.") for d in diffs):
        old.pop("pallets.confirmed", None)
    ev, _ = load_event(conn, other_id)
    save_fields(conn, other_id, old)
    ev, old = load_event(conn, other_id)
    sync_checks(conn, ev, old)
    save_fields(conn, other_id, old)
    changes = reconcile(conn, other_id)
    eff = describe_changes(conn, changes)
    log(conn, other_id, "revise",
        f"새 자료(#{from_event})로 수정: {', '.join(d['label'] + ' ' + str(d['old']) + '→' + str(d['new']) for d in diffs)}"
        + (f" · {eff}" if eff else ""), {"changes": changes, "diffs": diffs})


def revise(conn, event_id: int, changes_in: dict[str, Any]) -> dict:
    """사용자가 상세 화면에서 값을 직접 고칠 때. 이전 반영분과의 차이만 기록한다."""
    with Tx(conn):
        ev, fields = load_event(conn, event_id)
        if ev["status"] != "active":
            raise ValueError("병합·취소된 기록은 수정할 수 없습니다")
        diffs = []
        for path, v in changes_in.items():
            if path not in fields and path != "pallets.confirmed":
                raise ValueError(f"수정할 수 없는 항목: {path}")
            if path.endswith(".qty") or path.startswith("pallets.") or path == "transport.fee":
                errs: list[str] = []
                v = contract._num_or_none(v, path, errs)
                if errs:
                    raise ValueError(errs[0])
            old = (fields.get(path) or {}).get("value")
            if old == v:
                continue
            fld = fields.setdefault(path, {"value": None, "orig": None, "raw": None, "flags": [], "source": "user"})
            fld.update(value=v, source="user")
            if path in ("customer", "site") or path.endswith(".item"):
                fld.pop("ref", None)
                fld.pop("ref_source", None)
            if path == "customer":
                fields["site"].pop("ref", None)
            if path in ("pallets.invoice", "pallets.message"):
                pc = fields.get("pallets.confirmed")
                if pc and pc["source"] != "user":
                    fields.pop("pallets.confirmed")
            diffs.append({"path": path, "label": LABEL.get(_lbl(path), path), "old": old, "new": v})
            conn.execute("INSERT INTO confirmed_case(event_id, check_id, path, before_value, after_value, at)"
                         " VALUES (?,?,?,?,?,?)", (event_id, None, path, jdump(old), jdump(v), now()))
        if not diffs:
            return {"event_id": event_id, "checks": _checks_out(conn, event_id)}
        save_fields(conn, event_id, fields)
        ev, fields = load_event(conn, event_id)
        sync_checks(conn, ev, fields)
        save_fields(conn, event_id, fields)
        changes = reconcile(conn, event_id)
        eff = describe_changes(conn, changes)
        log(conn, event_id, "revise",
            "수정 " + ", ".join(f"{d['label']} {d['old']}→{d['new']}" for d in diffs) + (f" · {eff}" if eff else ""),
            {"changes": changes, "diffs": diffs})
        return {"event_id": event_id, "checks": _checks_out(conn, event_id)}


# --------------------------------------------------------------------------- queries
def _checks_out(conn, event_id):
    out = []
    for r in open_checks(conn, event_id):
        out.append({"id": r["id"], "path": r["path"], "kind": r["kind"], "rule_id": r["rule_id"],
                    "message": r["message"], "detail": json.loads(r["detail"])})
    return out


def event_detail(conn, event_id) -> dict:
    ev, fields = load_event(conn, event_id)
    moves = [dict(r) for r in conn.execute(
        "SELECT m.*, i.name item_name, i.unit FROM value_move m JOIN item i ON i.id=m.item_id"
        " WHERE event_id=? ORDER BY m.id", (event_id,))]
    claims = [dict(r) for r in conn.execute("SELECT * FROM money_claim WHERE event_id=?", (event_id,))]
    for c in claims:
        c["log"] = [dict(r) for r in conn.execute("SELECT * FROM money_log WHERE claim_id=? ORDER BY id", (c["id"],))]
    return {
        "id": ev["id"], "status": ev["status"], "merged_into": ev["merged_into"], "title": event_title(conn, event_id),
        "doc_no": ev["doc_no"], "trade_date": ev["trade_date"], "created_at": ev["created_at"],
        "updated_at": ev["updated_at"],
        "fields": {k: v for k, v in fields.items() if not k.startswith("_")},
        "checks": _checks_out(conn, event_id),
        "answered": [dict(r) for r in conn.execute(
            "SELECT id, path, kind, message, answer, answered_at FROM check_item WHERE event_id=? AND state='answered' ORDER BY id",
            (event_id,))],
        "moves": moves, "claims": claims,
        "obligations": [dict(r) | {"label": OBLIGATION_LABEL[r["kind"]]} for r in conn.execute(
            "SELECT * FROM obligation WHERE event_id=? AND state<>'n/a' ORDER BY id", (event_id,))],
        "intakes": [dict(r) for r in conn.execute(
            "SELECT id, input_id, channel, source_ref, received_at, outcome, payload FROM intake WHERE event_id=? ORDER BY id",
            (event_id,))],
        "history": [dict(r) for r in conn.execute(
            "SELECT id, at, action, summary FROM history WHERE event_id=? OR event_id IN"
            " (SELECT id FROM event WHERE merged_into=?) ORDER BY id", (event_id, event_id))],
    }


def history(conn, limit_days: int = 14) -> list[dict]:
    """오른편 히스토리: 처리일별 → 행(처리 시각·한 줄 요약·남은 일)."""
    rows = conn.execute(
        "SELECT h.*, e.status, e.trade_date, e.merged_into FROM history h LEFT JOIN event e ON e.id=h.event_id"
        " WHERE date(h.at) >= date('now', 'localtime', ?) ORDER BY h.id DESC", (f"-{limit_days} days",)).fetchall()
    days: dict[str, list] = {}
    seen: set[int] = set()
    for r in rows:
        target = r["merged_into"] or r["event_id"]
        ref = ""
        if target and r["action"] not in ("intake",):
            ev = conn.execute("SELECT doc_no FROM event WHERE id=?", (target,)).fetchone()
            ref = (ev["doc_no"] if ev and ev["doc_no"] else f"#{target}") + " · "
        # 남은 일은 사건별 가장 최근 행에만 붙인다(같은 사건이 여러 건으로 보이지 않도록)
        rem = remaining(conn, target) if target and target not in seen else []
        if target:
            seen.add(target)
        days.setdefault(r["at"][:10], []).append({
            "id": r["id"], "at": r["at"], "action": r["action"], "summary": ref + r["summary"],
            "event_id": target, "trade_date": r["trade_date"], "remaining": rem,
        })
    return [{"date": d, "items": items} for d, items in days.items()]


def summary(conn) -> dict:
    stock = [{"item_id": r["id"], "name": r["name"], "unit": r["unit"], "qty": factory_stock(conn, r["id"])}
             for r in conn.execute("SELECT * FROM item WHERE kind='product' ORDER BY id")]
    pallets = [dict(r) for r in conn.execute(
        "SELECT m.to_loc loc, COALESCE(s.address, p.name, m.to_loc) place, SUM(m.qty) qty FROM value_move m"
        " LEFT JOIN site s ON m.to_loc = 'site:' || s.id LEFT JOIN party p ON m.to_loc = 'party:' || p.id"
        " WHERE m.owner LIKE 'party:%' GROUP BY m.to_loc HAVING SUM(m.qty) <> 0")]
    claims = [dict(r) for r in conn.execute(
        "SELECT c.kind, c.role, c.party_name, c.amount, c.settled, c.event_id FROM money_claim c"
        " JOIN event e ON e.id=c.event_id WHERE c.state='active' AND e.status='active'"
        " AND (c.amount IS NULL OR c.amount > c.settled) ORDER BY c.id")]

    def tot(kind):
        known = sum(c["amount"] - c["settled"] for c in claims if c["kind"] == kind and c["amount"] is not None)
        unknown = sum(1 for c in claims if c["kind"] == kind and c["amount"] is None)
        return {"known": known, "undetermined_count": unknown}

    return {"stock": stock, "pallets_out": pallets, "receivable": tot("receivable"), "payable": tot("payable"),
            "claims": claims}
