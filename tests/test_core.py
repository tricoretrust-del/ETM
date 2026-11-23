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
            ["4", "2026-01-02", "변동", "운송", "25톤", "석분", "M3", "10", "4,500", "45,000", "4,500", "49,500", "상원", "", "계산서", "", ""],
            ["5", "2026-01-05", "변동", "운송", "25톤", "없음", "회", "1", "50,000", "50,000", "", "", "상원", "", "계산서", "", ""],
            ["6", "2026-01-06", "고정", "공과금", "전기", "없음", "회", "1", "300", "300", "", "", "한전", "Y", "자동이체", "", ""],
            ["7", "2026-01-07", "운영", "자산", "상품", "포설규사", "EA", "1", "700", "700", "", "", "에스피", "Y", "계산서", "", ""]])
        write(self.d, "ship.json", [[""] * 19,
            ["1", "2026-01-05", "S1", "생산판매", "인조화강", "226T.보통", "그레이", "P.PV.226N.GRY", "100", "10,000", "1,000,000", "", "반석", "라인", "A현장", "", "", "50,000", ""],
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
        self.assertEqual(rows, [(3, 10000, "확정")])            # 단가 없는 팔레트 출하는 채권 없음

    def test_missing_price_is_provisional_from_latest_actual(self):
        rows = self.c.execute("SELECT qty, unit_price, price_state FROM money WHERE item_code='P.PV.226N.GRY' ORDER BY id").fetchall()
        self.assertEqual(rows, [(100, 10000, "확정"), (10, 10000, "미결"), (5, 9000, "확정")])
        notes = Q.confirmations(self.c)
        self.assertEqual([n["reason"] for n in notes], ["단가"])
        self.assertIn("2026-01-05 10,000원", notes[0]["basis"])

    def test_ship_to_is_three_levels_with_contact(self):
        # Lv1 채무자(거래처) → Lv2 사업(업체) → Lv3 하차지. 같은 주소라도 사업이 다르면 다른 하차지 노드
        sites = self.c.execute("SELECT p.name, pj.name, s.name, s.contact FROM site s JOIN project pj ON pj.id=s.project_id"
                               " JOIN party p ON p.id=pj.bill_party_id ORDER BY pj.name").fetchall()
        self.assertEqual(sites, [("반석", "라인", "A현장", "010-1234-5678"), ("반석", "주원", "A현장", None)])
        self.assertTrue(self.c.execute("SELECT 1 FROM issue WHERE kind LIKE '같은 하차지가 여러 사업에%'").fetchone())
        notice = Q.kpp_notice(self.c, "2026-01-01", "2026-01-31")
        self.assertEqual([(r["date"], r["demand"], r["qty"], r["contact"]) for r in notice],
                         [("2026-01-05", "라인", 2, "010-1234-5678"), ("2026-01-06", "라인", 3, "010-1234-5678"),
                          ("2026-01-07", "라인", 1, "010-1234-5678")])

    def test_settled_vs_open(self):
        self.assertEqual(len(Q.open_money(self.c, "AP")), 3)          # 석분 매입·석분 운반비·출하 운반비 미결(수리·전기·상품은 지출)
        self.assertFalse(self.c.execute("SELECT 1 FROM money WHERE pending='운반비 부담'").fetchone())  # 운반비는 지오 채무(확정)
        self.assertEqual(len(Q.open_money(self.c, "AR")), 4)

    def test_shipment_freight_recorded_once(self):
        # 출고 탭 운반비와 비용 탭 운송 줄은 같은 운행이다. 비용 탭(운송사 있음) 한 줄만 채무로 둔다
        rows = self.c.execute("SELECT e.kind, m.amount, p.name FROM money m JOIN event e ON e.id=m.event_id"
                              " JOIN party p ON p.id=m.party_id WHERE m.item_code LIKE 'CHG.FRT%' AND m.for_item IS NULL").fetchall()
        self.assertEqual(rows, [("비용", 50000, "상원")])

    def test_month_cost(self):
        r = Q.month_cost(self.c, "2026-01")
        raw = 2000 * 195000 / 14500                      # 사용량 × (매입 + 그 자재 운반비) / 매입 KG
        self.assertAlmostEqual(r["raw"], raw)
        self.assertEqual((r["고정"], r["운영"]), (300, 100 + 700))
        self.assertAlmostEqual(r["변동"], raw + 50000)
        self.assertEqual((r["goods"], r["freight"]), (700, 50000))
        self.assertAlmostEqual(r["production_cost"], raw + 300 + 100)
        self.assertEqual(r["made_m2"], 150)
        self.assertIn("㎡당", Q.month_cost_text(self.c, "2026-01"))


if __name__ == "__main__":
    unittest.main()
