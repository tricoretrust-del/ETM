"""보관 구조 v0 핵심 규칙 시험 (가상 자료). 실행: python3 -m unittest tests.test_core"""
import json
import os
import tempfile
import unittest

from core import queries as Q
from core.migrate import migrate, num


def write(d, name, values, start=0):
    json.dump({"values": values, "startRow": start}, open(os.path.join(d, name), "w"), ensure_ascii=False)


class Core(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        H17 = [""] * 17
        write(self.d, "sku.json", [["h"], ["1", "P.PV.226N.GRY", "생산판매", "P.", "인조화강", "PV.", "226T.보통", "226N.", "그레이", "GRY."],
                                   ["2", "MAT.AGG.STD", "원자재", "MAT.", "골재", "AGG.", "석분", "STD.", "없음", "NA."],
                                   ["3", "C.PLT", "소매판매", "C.", "KPP팔레트", "", "없음", "PLT.", "없음", "NA."]])
        write(self.d, "cost.json", [H17,
            ["1", "2026-01-02", "제외(통계)", "원자재", "골재", "석분", "M3", "10", "15,000", "150,000", "15,000", "165,000", "에스피", "", "계산서", "", ""],
            ["2", "2026-01-03", "변동", "원자재", "골재", "석분", "TON", "2", "", "", "", "", "다성", "", "원가용", "", ""],
            ["3", "2026-01-04", "운영", "유지보수", "설비", "수리(파트)", "EA", "1", "100", "100", "10", "110", "신원", "Y", "계산서", "2026-01-31", ""],
            ["4", "2026-01-02", "변동", "운송", "25톤", "석분", "M3", "10", "4,500", "45,000", "4,500", "49,500", "상원", "", "계산서", "", ""]])
        write(self.d, "ship.json", [[""] * 19,
            ["1", "2026-01-05", "S1", "생산판매", "인조화강", "226T.보통", "그레이", "P.PV.226N.GRY", "100", "10,000", "1,000,000", "", "반석", "라인", "A현장", "", "", "", ""],
            ["2", "2026-01-05", "S1", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "2", "", "-", "", "반석", "라인", "A현장", "", "", "", ""],
            ["3", "2026-01-06", "S2", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "3", "10,000", "30,000", "", "반석", "라인", "A현장", "", "", "", ""],
            ["4", "2026-01-07", "S3", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "1", "", "-", "", "반석", "라인", "A현장", "", "", "", ""],
            ["5", "2026-01-08", "S4", "생산판매", "인조화강", "226T.보통", "그레이", "P.PV.226N.GRY", "10", "", "-", "", "반석", "라인", "A현장", "", "", "", ""],
            ["6", "2026-01-08", "S5", "생산판매", "인조화강", "226T.보통", "그레이", "P.PV.226N.GRY", "5", "9,000", "45,000", "", "반석", "주원", "A현장", "", "", "", ""]])
        json.dump([{"row": 5, "date": "2026-01-05", "bill": "반석", "demand": "라인", "addr": "A현장", "phone": "1012345678",
                    "type": "P10", "qty": 2, "car": "", "memo": ""}], open(os.path.join(self.d, "kpp.json"), "w"), ensure_ascii=False)
        write(self.d, "prod.json", [[""] * 13, ["1", "2026-01-04", "생산판매", "인조화강", "226T.보통", "그레이", "P.PV.226N.GRY", "150", "1.0", "150", "", "", ""],
                                    ["2", "2026-01-04", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "", "", "", "10", "한국파렛트풀", ""]])
        write(self.d, "raw1.json", [["h"]])
        write(self.d, "stock5.json", [])
        self.c = migrate(self.d, os.path.join(self.d, "t.db"))

    def test_num(self):
        self.assertEqual(num("- 150,000"), -150000)
        self.assertIsNone(num("-"))
        self.assertEqual(num("₩9,589,200"), 9589200)

    def test_purchase_and_usage_are_separate_flows(self):
        # 매입 10 M3 = 14,500 KG (가치) + 채무 1건, 사용 2 TON = 2,000 KG (가치만, 돈 없음)
        self.assertAlmostEqual(Q.stock(self.c)["MAT.AGG.STD"], 14500 - 2000)
        # 원가 단가 = (매입 150,000 + 그 자재 운반비 45,000) / 14,500 KG
        _, avg = Q.material_cost(self.c)
        self.assertAlmostEqual(avg["MAT.AGG.STD"], 195000 / 14500)
        n = self.c.execute("SELECT COUNT(*) FROM money m JOIN event e ON e.id=m.event_id WHERE e.kind='사용'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_shipment_moves_product_and_kpp_with_owner(self):
        st = Q.stock(self.c)
        self.assertEqual(st["P.PV.226N.GRY"], 150 - 100 - 10 - 5)
        self.assertEqual(st["PLT.KPP"], 10 - 2 - 3 - 1)
        self.assertEqual(sum(q for _, q in Q.pallets_at_sites(self.c)), 6)

    def test_kpp_pallets_not_billed_unless_price_recorded(self):
        rows = self.c.execute("SELECT qty, unit_price, price_state FROM money WHERE item_code='C.PLT'").fetchall()
        self.assertEqual(rows, [(3, 10000, "확정")])            # 단가 없는 팔레트 출하는 채권·임시 단가 없음

    def test_missing_price_is_provisional_from_latest_actual(self):
        rows = self.c.execute("SELECT qty, unit_price, price_state FROM money WHERE item_code='P.PV.226N.GRY' ORDER BY id").fetchall()
        self.assertEqual(rows, [(100, 10000, "확정"), (10, 10000, "임시"), (5, 9000, "확정")])

    def test_site_is_address_plus_demand_with_contact(self):
        sites = self.c.execute("SELECT s.address, p.name, s.contact FROM site s JOIN party p ON p.id=s.demand_party_id"
                               " ORDER BY p.name").fetchall()
        self.assertEqual(sites, [("A현장", "라인", "010-1234-5678"), ("A현장", "주원", None)])
        notice = Q.kpp_notice(self.c, "2026-01-01", "2026-01-31")
        self.assertEqual([(r["date"], r["demand"], r["qty"], r["contact"]) for r in notice],
                         [("2026-01-05", "라인", 2, "010-1234-5678"), ("2026-01-06", "라인", 3, "010-1234-5678"),
                          ("2026-01-07", "라인", 1, "010-1234-5678")])

    def test_settled_vs_open(self):
        self.assertEqual(len(Q.open_money(self.c, "AP")), 2)          # 석분 매입·운반비 미결(수리는 지출여부 Y)
        self.assertEqual(len(Q.open_money(self.c, "AR")), 4)


if __name__ == "__main__":
    unittest.main()
