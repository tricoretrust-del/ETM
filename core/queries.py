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
    """미결 = 금액(+부가세) − 완결 합계 > 0."""
    return c.execute(
        "SELECT m.*, (m.amount+m.vat) - COALESCE((SELECT SUM(amount) FROM settle s WHERE s.money_id=m.id AND NOT cancelled),0)"
        " AS remain FROM money m WHERE m.dir=? AND (m.amount+m.vat) - COALESCE((SELECT SUM(amount) FROM settle s"
        " WHERE s.money_id=m.id AND NOT cancelled),0) > 0.5", (d,)).fetchall()


def receivable_tree(c):
    """미결 채권: 청구처 합계 → 수요처 → 납품현장."""
    return c.execute(
        "SELECT pb.name, pd.name, s.address, SUM(m.amount+m.vat), SUM(m.price_state='임시')"
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
