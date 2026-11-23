"""이관 시험: 시트(JSON 내보내기)를 보관 구조 v0(core/schema.sql)에 넣는다.

실행: python3 -m core.migrate <원천 폴더> <DB 경로>
원천 폴더: cost.json(2.비용) ship.json(3.출고) prod.json(4.생산매입) sku.json(SKU) raw1.json(1.원자재재고) stock5.json(5.보유재고량)
시트는 읽기만 한다. 들어가지 못하는 줄은 issue 표에 남긴다(보관 구조의 일부가 아님).

이관 규칙 (docs/ETM_STORAGE_v0.md §5)
- 2.비용: 제외(통계)·원자재 = 매입(가치 + 채무). 변동·원자재 = 과거 사용 기록(가치만, 돈 없음).
  KPP팔레트 = 사용료 채무만(실물 수량은 4.생산매입). 결재방법 원가용·해당없음 = 돈 없음. 월총비용 = 제외.
  그 밖 = 비용(채무). 지출여부 Y·카드·자동이체·선급금 = 완결 기록.
- 3.출고: 송장 1건 = 출하 사건. 가치(공장 → 납품현장) + 채권(청구처). 운반비 = 채무(운송사 미상).
  유형 '운반비'(S.FRT, 음수 단가) = 채권 차감. 단가 없음 = 최근 실제 단가로 임시 단가(단가 미결).
- 4.생산매입: 생산판매 = 생산(가치). 소매판매 매입량 = 상품 입고(가치만, 돈은 2.비용).
- 기초: 1.원자재재고 기초재고, 5.보유재고량 조정(재고조사 2026-10-28).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
KG = {"TON": 1000.0, "KG": 1.0}
M3_KG = {"MAT.AGG.STD": 1450.0, "MAT.AGG.SAND": 1450.0, "MAT.AGG.PLS": 1450.0, "MAT.AGG.RCA": 1450.0}  # MAT 메모·원자재 매입가 기록
TRUCK_KG = {"MAT.AGG.RCA": 17 * 1450.0}                          # 순환골재 1대 = 17 M3 (원자재 매입가 기록)
BAG_KG = {"MAT.AGG.SIL4": 1600.0, "MAT.AGG.SIL5": 1600.0}        # 통합관리 메모
PALLET = "PLT.KPP"                                               # KPP 팔레트 실물(소유 KPP)

# 2.비용 Lv.2/3/4 → 품목 코드 (SKU 탭에 등록된 것). 그 밖은 임시 품목(X.)
RAW = {("시멘트", "OPC"): "MAT.CEM.OPC", ("시멘트", "백시멘트"): "MAT.CEM.WHC",
       ("골재", "8MM"): "MAT.AGG.8MM", ("골재", "규사#4"): "MAT.AGG.SIL4", ("골재", "규사#5"): "MAT.AGG.SIL5",
       ("골재", "모래"): "MAT.AGG.SAND", ("골재", "석분"): "MAT.AGG.STD", ("골재", "미장사"): "MAT.AGG.PLS",
       ("25톤", "미장사"): "MAT.AGG.PLS", ("골재", "성철순환골재"): "MAT.AGG.RCA", ("골재", "영흥순환골재"): "MAT.AGG.RCA",
       ("혼화제", "K300"): "MAT.ADM.K300", ("안료", "레드"): "MAT.PIG.RED", ("안료", "브라운"): "MAT.PIG.BRN",
       ("안료", "블랙"): "MAT.PIG.BLK", ("안료", "아이보리"): "MAT.PIG.IVR", ("안료", "오렌지"): "MAT.PIG.ORG"}
FREIGHT = {"25톤": "CHG.FRT.25T", "18톤": "CHG.FRT.18T", "9.5톤": "CHG.FRT.95T", "5톤축": "CHG.FRT.5TX",
           "1톤": "CHG.FRT.1T", "기타": "CHG.FRT.ETC"}
GOODS = {"논슬립선형": "C.NSLN", "논슬립점형": "C.NSDT", "포설규사": "C.SLC", "경계석(SC)": "C.CB.SC.GRY"}
CARD = ("법인카드", "다성카드", "개인카드")


def num(s):
    s = (s or "").replace("₩", "").replace(",", "").replace("\\", "").strip()
    s = re.sub(r"^-\s+", "-", s)
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def rows(path, width, header=1):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for i, r in enumerate(d["values"][header:], start=d.get("startRow", 0) + header + 1):
        r = [c.strip() for c in r] + [""] * (width - len(r))
        if any(r[1:]):
            out.append((i, r))
    return out


class Loader:
    def __init__(self, conn):
        self.c = conn
        self.party_ids = {}
        self.items = set()

    # -- 기준정보
    def item(self, code, name, typ, unit, conv=None, owner="own", cost_kind=None, alias=None):
        if code not in self.items:
            self.c.execute("INSERT INTO item(code,name,type,base_unit,conv,owner,cost_kind,aliases) VALUES (?,?,?,?,?,?,?,?)",
                           (code, name, typ, unit, json.dumps(conv or {}), owner, cost_kind,
                            json.dumps([alias] if alias else [], ensure_ascii=False)))
            self.items.add(code)
        elif alias:
            a = json.loads(self.c.execute("SELECT aliases FROM item WHERE code=?", (code,)).fetchone()[0])
            if alias not in a:
                self.c.execute("UPDATE item SET aliases=? WHERE code=?", (json.dumps(a + [alias], ensure_ascii=False), code))
        return code

    def party(self, name, role):
        name = (name or "").strip()
        if not name:
            return None
        pid = self.party_ids.get(name)
        if pid is None:
            pid = self.c.execute("INSERT INTO party(name, roles) VALUES (?,?)", (name, role)).lastrowid
            self.party_ids[name] = pid
        else:
            roles = self.c.execute("SELECT roles FROM party WHERE id=?", (pid,)).fetchone()[0]
            if role not in roles.split(","):
                self.c.execute("UPDATE party SET roles=? WHERE id=?", (",".join(x for x in (roles, role) if x), pid))
        return pid

    def issue(self, src, row, kind, detail):
        self.c.execute("INSERT INTO issue(source,row,kind,detail) VALUES (?,?,?,?)", (src, row, kind, detail))

    # -- 사건
    def event(self, no, date, kind, party_id, source, site_id=None, memo=""):
        return self.c.execute("INSERT INTO event(no,date,kind,party_id,site_id,source,memo) VALUES (?,?,?,?,?,?,?)",
                              (no, date, kind, party_id, site_id, source, memo)).lastrowid

    def move(self, ev, item, qty, unit, frm, to, owner="own"):
        conv = json.loads(self.c.execute("SELECT conv, base_unit FROM item WHERE code=?", (item,)).fetchone()[0])
        base_unit = self.c.execute("SELECT base_unit FROM item WHERE code=?", (item,)).fetchone()[0]
        base = qty if unit == base_unit or unit in ("", "EA|M2") else (qty * conv[unit] if unit in conv else None)
        self.c.execute("INSERT INTO move(event_id,item_code,qty,unit,base_qty,from_loc,to_loc,owner) VALUES (?,?,?,?,?,?,?,?)",
                       (ev, item, qty, unit, base, frm, to, owner))
        return base

    def money(self, ev, pid, d, item, amount, vat=0, qty=None, price=None, state="확정", due=None, method=None, for_item=None):
        return self.c.execute("INSERT INTO money(event_id,party_id,dir,item_code,qty,unit_price,price_state,amount,vat,due,method,"
                              "for_item) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (ev, pid, d, item, qty, price, state, amount, vat or 0, due, method, for_item)).lastrowid

    def settle(self, mid, date, amount, method, memo=""):
        self.c.execute("INSERT INTO settle(money_id,date,amount,method,memo) VALUES (?,?,?,?,?)",
                       (mid, date, amount, method, memo))


def base_items(L, sku_path):
    """SKU 탭 → 품목. 기본 단위: 인조화강·소형고압 M2, 그 밖 제품·상품 EA, 원자재 KG."""
    for i, r in rows(sku_path, 10):
        code, typ, kind = r[1], r[2], r[4]
        if not code:
            continue
        name = " ".join(x for x in (kind, r[6], r[8]) if x and x != "없음") or code
        t = code.split(".")[0]
        if t == "MAT":
            conv = dict(KG)
            conv.update({"M3": M3_KG[code]} if code in M3_KG else {})
            conv.update({"BAG": BAG_KG[code]} if code in BAG_KG else {})
            conv.update({"대": TRUCK_KG[code], "트럭": TRUCK_KG[code]} if code in TRUCK_KG else {})
            L.item(code, name, "MAT", "KG", conv, cost_kind="변동")
        elif t == "CHG":
            L.item(code, name, "CHG", "회", cost_kind="변동")
        else:
            unit = "M2" if code.startswith(("P.PV", "P.ILP", "C.PV", "C.ILP")) else ("회" if t == "S" else "EA")
            L.item(code, name, t, unit)
    L.item(PALLET, "KPP 팔레트(실물) P10", "RNT", "EA", owner="KPP", alias="P10")
    L.item("MAT.CEM.SLG", "슬래그시멘트", "MAT", "KG", dict(KG), cost_kind="변동")   # 원자재 매입가 기록(다다씨엠)


def load_cost(L, path):
    src = "2.비용"
    for i, r in rows(path, 17):
        date, lv1, lv2, lv3, lv4, unit = r[1], r[2], r[3], r[4], r[5], r[6]
        qty, price, amt, vat = num(r[7]), num(r[8]), num(r[9]), num(r[10])
        sup, paid, method, pay_date, memo = r[12], r[13], r[14], r[15], r[16]
        if not date or not lv2:
            L.issue(src, i, "빈 줄·분류 없음", " ".join(r[1:7]))
            continue
        if lv2 == "월총비용":
            continue
        if amt is None and qty is not None and price is not None:
            amt = qty * price
        pid = L.party(sup, "공급처")
        if lv2 == "원자재":
            code = RAW.get((lv3, lv4))
            if not code:
                L.issue(src, i, "품목 코드 없음", f"원자재/{lv3}/{lv4}")
                continue
            L.item(code, lv4, "MAT", "KG", alias=f"비용:{lv3}/{lv4}")
            if lv1 == "변동":       # 과거 원가용 사용 기록 → 사용(가치만)
                if method in ("계산서",) + CARD:
                    L.issue(src, i, "사용 줄인데 결제수단 있음", f"{date} {lv4} {qty}{unit} {method}")
                ev = L.event(None, date, "사용", None, f"{src}!{i}", memo=memo)
                if L.move(ev, code, qty or 0, unit, "factory", "consumed") is None:
                    L.issue(src, i, "단위 환산 없음", f"{lv4} {unit}")
                continue
            # 제외(통계)·운영 → 매입(가치 + 채무)
            ev = L.event(None, date, "매입", pid, f"{src}!{i}", memo=memo)
            if L.move(ev, code, qty or 0, unit, f"supplier:{pid}", "factory") is None:
                L.issue(src, i, "단위 환산 없음", f"{lv4} {qty} {unit}")
            _cost_money(L, src, i, ev, pid, code, amt, vat, qty, price, method, paid, pay_date, date)
            continue
        if method in ("원가용", "해당없음"):
            L.issue(src, i, "돈 없는 비용 줄(원가용·해당없음)", f"{date} {lv1}/{lv2}/{lv3}/{lv4} {amt}")
            continue
        if (lv2, lv3, lv4) == ("소모품", "포장", "KPP팔레트"):
            code = "CHG.RNT.PLT"
        elif lv2 == "운송":
            code = FREIGHT.get(lv3, "CHG.FRT.ETC")
            for_item = RAW.get(("골재", lv4)) if lv4 not in ("없음", "") else None
            if lv4 not in ("없음", "") and not for_item:
                L.issue(src, i, "운반비 대상 자재 불명", f"운송/{lv3}/{lv4}")
        elif (lv2, lv3) == ("자산", "상품"):
            code = GOODS.get(lv4)
            if not code:
                code = L.item(f"X.상품.{lv4}", f"상품 {lv4}", "X", unit or "EA")
                L.issue(src, i, "상품 SKU 대응 불가", f"자산/상품/{lv4} (여러 SKU 또는 미등록)")
        else:
            code = L.item("X." + ".".join(x for x in (lv2, lv3, lv4) if x and x != "없음"),
                          "/".join(x for x in (lv2, lv3, lv4) if x and x != "없음"), "X", "회",
                          cost_kind=lv1 if lv1 in ("고정", "변동", "운영") else None)
        ev = L.event(None, date, "비용", pid, f"{src}!{i}", memo=memo)
        _cost_money(L, src, i, ev, pid, code, amt, vat, qty, price, method, paid, pay_date, date,
                    for_item=for_item if lv2 == "운송" else None)


def _cost_money(L, src, i, ev, pid, code, amt, vat, qty, price, method, paid, pay_date, date, for_item=None):
    if amt is None:
        L.issue(src, i, "금액 없음", f"{date} {code}")
        return
    if pid is None:
        L.issue(src, i, "상대 없음", f"{date} {code} {amt}")
    due = pay_date if re.match(r"\d{4}-\d\d-\d\d$", pay_date or "") else None
    mid = L.money(ev, pid, "AP", code, amt, vat, qty, price, due=due, method=method or None, for_item=for_item)
    total = amt + (vat or 0)
    if paid == "Y":
        L.settle(mid, due or date, total, method, "지출여부 Y")
    elif method in CARD:
        L.settle(mid, date, total, method, "카드 결제")
    elif pay_date in ("자동이체", "선급금"):
        L.settle(mid, date, total, pay_date, pay_date)


def site_key(r):
    """납품현장 = 주소 + 수요처 (같은 주소라도 수요처가 다르면 다른 납품현장. 예: 파라곤현장의 라인건설 / 주원조경)."""
    return ((r[14] or r[13]).strip(), r[13].strip())


def phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 10 and d.startswith("10"):
        d = "0" + d                      # 숫자로 저장돼 앞의 0이 빠진 번호
    return f"{d[:3]}-{d[3:-4]}-{d[-4:]}" if len(d) in (10, 11) else (str(s).strip() or None)


def load_sites(L, ship_path, contacts=None):
    """청구처는 가장 많이 나온 것. 같은 납품현장(주소+수요처)에 청구처가 여럿이면 정리 대상(차수 분리 또는 오기)."""
    combos = defaultdict(Counter)
    for i, r in rows(ship_path, 19):
        k = site_key(r)
        if k[0] and r[7]:
            combos[k][r[12]] += 1
    site_of = {}
    for (addr, demand), cnt in combos.items():
        bill, _ = cnt.most_common(1)[0]
        contact = (contacts or {}).get((re.sub(r"\s", "", addr), demand))
        sid = L.c.execute("INSERT INTO site(address, demand_party_id, phase, bill_party_id, contact) VALUES (?,?,?,?,?)",
                          (addr, L.party(demand, "수요처"), 1, L.party(bill, "청구처"), contact)).lastrowid
        site_of[(addr, demand)] = sid
        if len(cnt) > 1:
            L.issue("3.출고", None, "같은 납품현장(주소+수요처)에 청구처 여럿(차수 분리 또는 오기)",
                    f"{addr} / {demand} :: " + " / ".join(f"{b} {n}줄" for b, n in cnt.most_common()))
    return site_of


def load_ship(L, path, site_of):
    src = "3.출고"
    by_no = defaultdict(list)
    for i, r in rows(path, 19):
        if not r[1]:
            if any(r[3:11]) and r[10] not in ("", "-"):
                L.issue(src, i, "날짜 없음", " ".join(r[2:11]))
            continue
        no = r[2] or f"(송장없음){r[1]}|{r[12]}|{r[14] or r[13]}"
        by_no[no].append((i, r))
    last_price = {}      # (청구처, SKU) / SKU → 최근 실제 단가 (임시 단가용)
    for no, lines in sorted(by_no.items(), key=lambda kv: (kv[1][0][1][1], kv[0])):
        i0, r0 = lines[0]
        trip = {(r[12], r[13], r[14] or r[13]) for _, r in lines if r[7] != "S.FRT"}
        if len(trip) > 1:
            L.issue(src, i0, "송장 1건에 청구처·수요처·현장 섞임", f"{no} :: " + " / ".join("-".join(t) for t in sorted(trip)))
        if no.startswith("(송장없음)"):
            L.issue(src, i0, "송장번호 없음", no)
        bill = L.party(r0[12], "청구처")
        site = site_of.get(site_key(r0))
        ev = L.event(None if no.startswith("(") else no, r0[1], "출하", bill, f"{src}!{i0}", site_id=site,
                     memo=" ".join(x for x in (r0[15] and f"차량 {r0[15]}", r0[16], r0[11]) if x))
        for i, r in lines:
            sku, qty, price, amt = r[7], num(r[8]), num(r[9]), num(r[10])
            pid = L.party(r[12], "청구처") or bill
            if not sku or sku == "SKU 미등록":
                L.issue(src, i, "SKU 없음", " ".join(x for x in r[3:12] if x))
                continue
            if sku not in L.items:
                L.issue(src, i, "SKU 기준정보 없음", sku)
                continue
            if sku == "S.FRT":      # 운반비를 채권에서 차감(음수)
                L.money(ev, pid, "AR", sku, amt if amt is not None else (qty or 0) * (price or 0), 0, qty, price)
                continue
            s = site_of.get(site_key(r))
            dest = f"site:{s}" if s else "site:?"
            if not s:
                L.issue(src, i, "납품현장 없음", f"{no} {sku} {qty}")
            if sku == "C.PLT":
                L.move(ev, PALLET, qty or 0, "EA", "factory", dest, owner="KPP")
                if price is not None and amt:   # 2025-03 이전에는 청구한 기록이 있다. 그 외에는 청구하지 않는다(채권 없음)
                    L.money(ev, pid, "AR", sku, amt, round(amt * 0.1), qty, price)
                continue
            L.move(ev, sku, qty or 0, "", "factory", dest)
            state = "확정"
            if price is None or (amt in (None, 0) and qty):
                cand = last_price.get((pid, sku)) or last_price.get(sku)
                if cand is None:
                    L.issue(src, i, "단가 없음·임시 단가도 없음", f"{no} {sku} {qty}")
                    continue
                price, state = cand, "임시"
                amt = (qty or 0) * price
            else:
                last_price[(pid, sku)] = last_price[sku] = price
                if amt is None:
                    amt = (qty or 0) * price
            L.money(ev, pid, "AR", sku, amt, round(amt * 0.1), qty, price, state=state)
        fr = [(i, num(r[17])) for i, r in lines if num(r[17])]
        for i, f in fr:
            cp = L.party("운송사 미상", "운송사")
            L.money(ev, cp, "AP", "CHG.FRT.ETC", f, 0, method=None)
        if fr:
            L.issue(src, i0, "운반비 운송사 미상", f"{no} 운반비 {sum(f for _, f in fr):,.0f}")


def load_prod(L, path):
    src = "4.생산매입"
    for i, r in rows(path, 13):
        if not r[1] or not r[2]:
            continue
        sku = r[6]
        if not sku or sku == "SKU 미등록" or sku not in L.items:
            L.issue(src, i, "SKU 없음", " ".join(r[2:7]))
            continue
        if r[2] == "생산판매":
            q = num(r[9]) if num(r[9]) is not None else (num(r[7]) or 0) * (num(r[8]) or 0)
            ev = L.event(None, r[1], "생산", None, f"{src}!{i}", memo=r[12])
            L.move(ev, sku, q or 0, "", "production", "factory")
        elif r[2] == "소매판매":
            q = num(r[10])
            if q is None:
                continue
            pid = L.party(r[11], "공급처")
            ev = L.event(None, r[1], "매입", pid, f"{src}!{i}", memo="상품 입고(돈은 2.비용)")
            if sku == "C.PLT":
                L.move(ev, PALLET, q, "EA", f"supplier:{pid}", "factory", owner="KPP")
            else:
                L.move(ev, sku, q, "", f"supplier:{pid}", "factory")


def load_opening(L, raw1_path, stock5_path):
    names = {"규사#4": "MAT.AGG.SIL4", "규사#5": "MAT.AGG.SIL5", "블랙": "MAT.PIG.BLK", "아이보리": "MAT.PIG.IVR",
             "브라운": "MAT.PIG.BRN", "오렌지": "MAT.PIG.ORG", "KPP팔레트": PALLET, "포설규사": "C.SLC",
             "논슬립선형": "C.NSLN", "논슬립점형": "C.NSDT"}
    ev = L.event(None, "2024-09-01", "조정", None, "1.원자재재고 기초재고")
    for i, r in rows(raw1_path, 10):
        code, q, unit = names.get(r[1]), num(r[3]), r[2]
        if code and q and not code.startswith("MAT"):
            continue    # 상품·KPP 기초는 5.보유재고량 조정 하나만 쓴다(두 시트가 기초를 따로 가짐 — 결과 보고에 차이 표시)
        if code and q:
            L.move(ev, code, q, unit if code.startswith("MAT") else "", "opening", "factory",
                   owner="KPP" if code == PALLET else "own")
        elif r[1] and q and r[0].isdigit():
            L.issue("1.원자재재고", i, "기초재고 품목 대응 없음", f"{r[1]} {q}")
    d = json.loads(Path(stock5_path).read_text(encoding="utf-8"))
    ev2 = L.event(None, "2026-10-28", "조정", None, "5.보유재고량 조정(재고조사 2026-10-28)")
    for r in d["values"]:
        r = [c.strip() for c in r] + [""] * 14
        sku, adj = r[9], num(r[10])
        if sku and adj and sku in L.items and r[0].isdigit():
            if sku == "C.PLT":
                L.move(ev2, PALLET, adj, "EA", "adjust", "factory", owner="KPP")
            else:
                L.move(ev2, sku, adj, "", "adjust", "factory")


def migrate(src_dir, db_path):
    src = Path(src_dir)
    Path(db_path).unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    L = Loader(conn)
    kpp = json.loads((src / "kpp.json").read_text(encoding="utf-8")) if (src / "kpp.json").exists() else []
    contacts = {}
    for k in kpp:      # KPP 출하통보의 연락처 = 현장관리자 전화 → 납품현장 기준정보
        if k["phone"]:
            contacts[(re.sub(r"\s", "", k["addr"]), k["demand"])] = phone(k["phone"])
    with conn:
        base_items(L, src / "sku.json")
        load_cost(L, src / "cost.json")
        site_of = load_sites(L, src / "ship.json", contacts)
        load_ship(L, src / "ship.json", site_of)
        load_prod(L, src / "prod.json")
        load_opening(L, src / "raw1.json", src / "stock5.json")
        if kpp:
            check_kpp_notice(L, kpp)
    return conn


def check_kpp_notice(L, kpp):
    """실제 출하통보(손입력)와, 출하 기록에서 만든 통보를 날짜 × 주소 × 수요처로 대조한다."""
    from . import queries
    first, last = min(k["date"] for k in kpp), max(k["date"] for k in kpp)
    made = Counter()
    for r in queries.kpp_notice(L.c, first, last):
        made[(r["date"], re.sub(r"\s", "", r["address"] or ""), r["demand"] or "")] += r["qty"]
    sent = Counter()
    for k in kpp:
        if isinstance(k["qty"], (int, float)):
            sent[(k["date"], re.sub(r"\s", "", k["addr"]), k["demand"])] += k["qty"]
        else:
            L.issue("KPP출하통보", k["row"], "수량이 숫자 아님", f"{k['date']} {k['demand']} {k['qty']}")
    for key in sorted(set(made) | set(sent)):
        if abs(made[key] - sent[key]) > 0.5:
            L.issue("KPP출하통보", None, "통보 수량 ≠ 출하 기록",
                    f"{key[0]} {key[2]} {key[1][:24]} :: 통보 {sent[key]:g} / 출하 {made[key]:g}")


if __name__ == "__main__":
    c = migrate(sys.argv[1], sys.argv[2])
    for t in ("item", "party", "site", "event", "move", "money", "settle", "issue"):
        print(t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
