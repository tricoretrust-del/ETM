"""조회: 보관하지 않고 사건·가치·돈·완결에서 계산한다."""
from __future__ import annotations


def stock(c, item=None, until=None):
    """공장 재고 = 공장으로 들어온 기본 단위 수량 − 공장에서 나간 수량."""
    q = ("SELECT m.item_code, SUM(CASE WHEN m.to_loc='factory' THEN m.base_qty ELSE 0 END)"
         " - SUM(CASE WHEN m.from_loc='factory' THEN m.base_qty ELSE 0 END)"
         " FROM move m JOIN event e ON e.id=m.event_id WHERE m.base_qty IS NOT NULL")
    args = []
    if item:
        q += " AND m.item_code=?"; args.append(item)
    if until:
        q += " AND e.date<=?"; args.append(until)
    return dict(c.execute(q + " GROUP BY m.item_code", args).fetchall())


def flows(c, item):
    """품목의 들어온 양 / 나간 양을 사건 종류별로."""
    return c.execute("SELECT e.kind, SUM(CASE WHEN m.to_loc='factory' THEN m.base_qty ELSE 0 END),"
                     " SUM(CASE WHEN m.from_loc='factory' THEN m.base_qty ELSE 0 END), COUNT(*)"
                     " FROM move m JOIN event e ON e.id=m.event_id WHERE m.item_code=? GROUP BY e.kind", (item,)).fetchall()


def pallets_at_sites(c):
    """KPP 팔레트 납품현장별 체류(회수 기록이 아직 없으므로 출하 누계)."""
    return c.execute("SELECT s.address, SUM(m.base_qty) FROM move m JOIN site s ON m.to_loc='site:'||s.id"
                     " WHERE m.item_code='PLT.KPP' GROUP BY s.id ORDER BY 2 DESC").fetchall()


def open_money(c, d):
    """미결 = 금액(+부가세) − 완결 합계 > 0. (확인 미결은 confirmations)"""
    return c.execute(
        "SELECT m.*, (m.amount+m.vat) - COALESCE((SELECT SUM(amount) FROM settle s WHERE s.money_id=m.id AND NOT cancelled),0)"
        " AS remain FROM money m WHERE m.dir=? AND (m.amount+m.vat) - COALESCE((SELECT SUM(amount) FROM settle s"
        " WHERE s.money_id=m.id AND NOT cancelled),0) > 0.5", (d,)).fetchall()


def receivable_tree(c):
    """미결 채권: 청구처 합계 → 수요처 → 납품현장."""
    return c.execute(
        "SELECT pb.name, pd.name, s.address, SUM(m.amount+m.vat), SUM(m.price_state='미결')"
        " FROM money m JOIN event e ON e.id=m.event_id LEFT JOIN site s ON s.id=e.site_id"
        " LEFT JOIN party pb ON pb.id=m.party_id LEFT JOIN party pd ON pd.id=s.demand_party_id"
        " WHERE m.dir='AR' AND NOT EXISTS (SELECT 1 FROM settle x WHERE x.money_id=m.id)"
        " GROUP BY pb.name, pd.name, s.address ORDER BY pb.name, 4 DESC").fetchall()


def monthly(c):
    """월별 매출(채권 공급가) / 비용(채무 공급가, 품목 비용 성격별)."""
    sales = dict(c.execute("SELECT substr(e.date,1,7), SUM(m.amount) FROM money m JOIN event e ON e.id=m.event_id"
                           " WHERE m.dir='AR' GROUP BY 1").fetchall())
    cost = c.execute("SELECT substr(e.date,1,7), COALESCE(i.cost_kind,'(미정)'), SUM(m.amount) FROM money m"
                     " JOIN event e ON e.id=m.event_id LEFT JOIN item i ON i.code=m.item_code"
                     " WHERE m.dir='AP' GROUP BY 1,2").fetchall()
    return sales, cost


def price_trend(c, item):
    """단가 추이: 품목 × 상대별로 단가가 바뀐 시점만."""
    out, last = [], {}
    for d, party, price, state in c.execute(
            "SELECT e.date, p.name, m.unit_price, m.price_state FROM money m JOIN event e ON e.id=m.event_id"
            " LEFT JOIN party p ON p.id=m.party_id WHERE m.item_code=? AND m.unit_price IS NOT NULL ORDER BY e.date",
            (item,)):
        if last.get(party) != price:
            out.append((d, party, price, state))
            last[party] = price
    return out


def material_cost(c):
    """월 원자재 원가 = 사용량(KG) × 품목별 (매입 금액 + 그 자재를 나른 운반비) / 매입 KG. 기간 전체 평균(v0 규칙)."""
    buy = dict(c.execute(
        "SELECT mo.item_code, SUM(m.amount) FROM money m JOIN move mo ON mo.event_id=m.event_id"
        " AND mo.item_code=m.item_code WHERE m.dir='AP' AND mo.base_qty>0 GROUP BY mo.item_code").fetchall())
    kg = dict(c.execute(
        "SELECT mo.item_code, SUM(mo.base_qty) FROM money m JOIN move mo ON mo.event_id=m.event_id"
        " AND mo.item_code=m.item_code WHERE m.dir='AP' AND mo.base_qty>0 GROUP BY mo.item_code").fetchall())
    freight = dict(c.execute("SELECT for_item, SUM(amount) FROM money WHERE for_item IS NOT NULL GROUP BY 1").fetchall())
    avg = {k: (buy[k] + freight.get(k, 0)) / kg[k] for k in buy if kg.get(k)}
    out = {}
    for mon, item, kg in c.execute(
            "SELECT substr(e.date,1,7), m.item_code, SUM(m.base_qty) FROM move m JOIN event e ON e.id=m.event_id"
            " WHERE e.kind='사용' AND m.base_qty IS NOT NULL GROUP BY 1,2"):
        out[mon] = out.get(mon, 0) + kg * (avg.get(item) or 0)
    return out, avg


def kpp_notice(c, start, end):
    """KPP 출하통보: 기간 안의 KPP 팔레트 출하를 납품현장별로. 출하 기록 하나로 만든다(다시 입력하지 않음)."""
    out = []
    for d, bill, demand, addr, contact, qty, memo in c.execute(
            "SELECT e.date, pb.name, pd.name, s.address, s.contact, SUM(m.qty), e.memo FROM move m"
            " JOIN event e ON e.id=m.event_id JOIN site s ON m.to_loc='site:'||s.id"
            " LEFT JOIN party pb ON pb.id=s.bill_party_id LEFT JOIN party pd ON pd.id=s.demand_party_id"
            " WHERE m.item_code='PLT.KPP' AND e.kind='출하' AND e.date BETWEEN ? AND ?"
            " GROUP BY e.id ORDER BY e.date, e.id", (start, end)):
        car = memo.split()[1] if memo.startswith("차량 ") else ""
        out.append({"date": d, "bill": bill, "demand": demand, "address": addr, "contact": contact,
                    "type": "P10", "qty": qty, "car": car})
    return out


def kpp_at_sites(c, until=None):
    """KPP 팔레트 납품현장별 체류 = 출하 누계 − 회수(회수 기록이 생기면 차감)."""
    q = ("SELECT pd.name, s.address, s.contact, SUM(CASE WHEN m.to_loc='site:'||s.id THEN m.qty ELSE -m.qty END)"
         " FROM move m JOIN event e ON e.id=m.event_id JOIN site s ON m.to_loc='site:'||s.id OR m.from_loc='site:'||s.id"
         " LEFT JOIN party pd ON pd.id=s.demand_party_id WHERE m.item_code='PLT.KPP'")
    args = []
    if until:
        q += " AND e.date<=?"; args.append(until)
    return c.execute(q + " GROUP BY s.id HAVING SUM(CASE WHEN m.to_loc='site:'||s.id THEN m.qty ELSE -m.qty END) <> 0"
                         " ORDER BY 4 DESC", args).fetchall()


def confirmations(c, limit=None):
    """사용자 확인 요청(notice): 확정이 없어 미결로 둔 돈 줄. 판단 근거로 같은 청구처의 단가 기록을 붙인다."""
    out = []
    q = ("SELECT m.id, m.pending, e.date, e.no, p.name, i.name, m.item_code, m.qty, m.unit_price, m.amount, m.party_id,"
         " s.address, pd.name FROM money m JOIN event e ON e.id=m.event_id LEFT JOIN party p ON p.id=m.party_id"
         " LEFT JOIN item i ON i.code=m.item_code LEFT JOIN site s ON s.id=e.site_id LEFT JOIN party pd ON pd.id=s.demand_party_id"
         " WHERE m.price_state='미결' ORDER BY e.date, m.id")
    for mid, why, d, no, party, iname, code, qty, price, amt, pid, addr, demand in c.execute(q + (f" LIMIT {int(limit)}" if limit else "")):
        hist = c.execute("SELECT e.date, m.unit_price FROM money m JOIN event e ON e.id=m.event_id WHERE m.party_id=?"
                         " AND m.item_code=? AND m.price_state='확정' AND e.date<=? ORDER BY e.date DESC LIMIT 3",
                         (pid, code, d)).fetchall()
        basis = ", ".join(f"{hd} {hp:,.0f}원" for hd, hp in hist) or "이 청구처의 확정 단가 기록 없음"
        where = " / ".join(x for x in (demand, addr) if x)
        if why == "무상 제공 여부":
            msg = f"{d} {party} {where}에 {iname} {(qty or 0):g}개를 출하했습니다. 무상 제공인가요, 판매인가요?"
        elif why == "운반비 부담":
            sh = c.execute("SELECT pb.name FROM event e LEFT JOIN party pb ON pb.id=e.party_id WHERE e.id="
                           "(SELECT event_id FROM money WHERE id=?)", (mid,)).fetchone()[0]
            msg = f"{d} 송장 {no or '-'} ({sh}) 운반비 {amt:,.0f}원은 우리 부담인가요?"
            prices = "; ".join(f"{iname_} {u:,.0f}원" for iname_, u in c.execute(
                "SELECT DISTINCT i.name, m.unit_price FROM money m JOIN event e ON e.id=m.event_id JOIN item i ON i.code=m.item_code"
                " WHERE e.id=(SELECT event_id FROM money WHERE id=?) AND m.dir='AR' AND m.unit_price IS NOT NULL", (mid,))) \
                or "이 송장의 판매 단가 기록 없음"
            ours, theirs = c.execute(   # 같은 청구처 송장의 운반비를 사용자가 확정한 기록(우리 부담 = 채무 유지, 거래처 부담 = 0원 확정)
                "SELECT SUM(m.amount>0), SUM(m.amount=0) FROM money m JOIN event e ON e.id=m.event_id"
                " WHERE m.item_code LIKE 'CHG.FRT%' AND m.dir='AP' AND m.price_state='확정' AND e.kind='출하'"
                " AND e.party_id=(SELECT party_id FROM event WHERE id=(SELECT event_id FROM money WHERE id=?))", (mid,)).fetchone()
            hist = f"이 청구처의 지난 운반비 확정: 우리 부담 {ours or 0}건 / 거래처 부담 {theirs or 0}건"
            basis = f"판매 단가 {prices}. {hist}"
        else:
            msg = f"{d} {party} {where}에 출하한 {iname} {(qty or 0):g}의 단가가 없습니다. 얼마인가요?"
        out.append({"money_id": mid, "reason": why, "message": msg, "basis": basis,
                    "reference_amount": amt if why != "운반비 부담" else None})
    return out
