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
            ["3", "2026-01-04", "운영", "유지보수", "설비", "수리(파트)", "EA", "1", "100", "100", "10", "110", "신원", "Y", "계산서", "2026-01-31", ""]])
        write(self.d, "ship.json", [[""] * 19,
            ["1", "2026-01-05", "S1", "생산판매", "인조화강", "226T.보통", "그레이", "P.PV.226N.GRY", "100", "10,000", "1,000,000", "", "반석", "라인", "A현장", "", "", "", ""],
            ["2", "2026-01-05", "S1", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "2", "", "-", "", "반석", "라인", "A현장", "", "", "", ""],
            ["3", "2026-01-06", "S2", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "3", "10,000", "30,000", "", "반석", "라인", "A현장", "", "", "", ""],
            ["4", "2026-01-07", "S3", "소매판매", "KPP팔레트", "없음", "없음", "C.PLT", "1", "", "-", "", "반석", "라인", "A현장", "", "", "", ""]])
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
        n = self.c.execute("SELECT COUNT(*) FROM money m JOIN event e ON e.id=m.event_id WHERE e.kind='사용'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_shipment_moves_product_and_kpp_with_owner(self):
        st = Q.stock(self.c)
        self.assertEqual(st["P.PV.226N.GRY"], 150 - 100)
        self.assertEqual(st["PLT.KPP"], 10 - 2 - 3 - 1)
        self.assertEqual(sum(q for _, q in Q.pallets_at_sites(self.c)), 6)

    def test_missing_price_is_provisional_from_latest_actual(self):
        rows = self.c.execute("SELECT qty, unit_price, price_state FROM money WHERE item_code='C.PLT' ORDER BY id").fetchall()
        # 첫 줄은 앞선 실제 단가가 없어 임시 단가도 못 정함(issue), 셋째 줄은 둘째 줄 실제 단가로 임시
        self.assertEqual(rows, [(3, 10000, "확정"), (1, 10000, "임시")])
        self.assertTrue(self.c.execute("SELECT 1 FROM issue WHERE kind LIKE '단가 없음%'").fetchone())

    def test_settled_vs_open(self):
        self.assertEqual(len(Q.open_money(self.c, "AP")), 1)          # 석분 매입만 미결(수리는 지출여부 Y)
        self.assertEqual(len(Q.open_money(self.c, "AR")), 3)


if __name__ == "__main__":
    unittest.main()
