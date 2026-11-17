"""JSON 이 아닌 입력의 자동 변환."""
import json
import unittest

from etm import db, process, seed, textparse

INVOICE_TEXT = """송장번호: 20261111-01
납품일: 2026-11-11
공급받는자: 금성백조
담당자: 김경섭 소장님
담당자 전화: 010-8265-3835
현장: 대전광역시 유성구 성북동 731-12
품목1: 보도블록A 600 EA
품목2: 경계석 B형(가상) 40EA
팔레트: 6
상차 메시지 팔레트: 6개
차량번호: 5006 (손글씨)
기사전화: 010-5055-5478 (손글씨)
"""


RECEIPT_TEXT = """1. 업체명: (사진에 안나옴, 다음부터는 제대로 나오게 사진을 찍을 것) LOGISALL
2. 전표번호: A14884069
3. 입고일: 2026-11-11
4. 납품처: 주식회사 지오블록
5. 차량번호: 885968
6. 기사연락처: 01071550855
7. 유형: P10
8. 수량: 97"""


class TestTextParse(unittest.TestCase):
    def test_receipt_is_held_not_shipped(self):
        p, _ = textparse.to_payload(RECEIPT_TEXT)
        self.assertEqual(p["doc_type"], "receipt")
        self.assertEqual(p["supplier"], "LOGISALL")            # 업체명 = 보낸 업체
        self.assertEqual(p["receiver"], "주식회사 지오블록")      # 납품처 = 받은 곳 (거래처 아님)
        self.assertNotIn("customer", p)
        self.assertEqual(p["trade_date"], "2026-11-11")
        self.assertEqual(p["driver_phone"], "010-7155-0855")
        self.assertEqual(p["lines"], [{"item": "P10", "qty": 97, "unit": None}])
        conn = db.connect(":memory:")
        seed.seed_demo(conn)
        before = process.summary(conn)
        r = process.intake(conn, p)
        self.assertEqual(r["outcome"], "held")
        self.assertEqual(process.summary(conn), before)          # 재고·돈 변화 없음
        self.assertEqual(process.intake(conn, p)["outcome"], "re_receive")

    def test_code_like_item_not_split(self):
        p, _ = textparse.to_payload("품명: SDC08\n발주수량: 18\n단위: 백")
        self.assertEqual(p["lines"], [{"item": "SDC08", "qty": 18, "unit": "백"}])

    def test_labeled_text(self):
        p, _ = textparse.to_payload(INVOICE_TEXT)
        self.assertEqual(p["doc_no"], "20261111-01")
        self.assertEqual(p["contact"], {"name": "김경섭", "phone": "010-8265-3835"})
        self.assertEqual(p["lines"][1], {"item": "경계석 B형(가상)", "qty": 40, "unit": "EA"})
        self.assertEqual(p["pallets"], {"invoice": 6, "message": 6})
        self.assertTrue(p["meta"]["vehicle_no"]["handwritten"])
        self.assertEqual(p["raw_text"], INVOICE_TEXT.strip())  # 원문 보존

    def test_loose_json(self):
        p, notes = textparse.to_payload("{'input_id': 'a', 'channel': 'manual', 'lines': [{'item': 'x', 'qty': 1,},],}")
        self.assertEqual(p["lines"][0]["qty"], 1)
        self.assertTrue(notes)

    def test_free_text_guesses_are_uncertain(self):
        p, notes = textparse.to_payload("11월 12일 나성씨앤디 출하\n품명: 경계석B\n수량: 30 EA\n010-9999-0000",
                                        ["나성씨앤디"])
        self.assertEqual(p["customer"], "나성씨앤디")
        self.assertTrue(p["meta"]["trade_date"]["uncertain"])   # 연도 추정
        self.assertNotIn("contact", p)                           # 누구 번호인지 모르면 넣지 않음
        self.assertTrue(any("알 수 없어" in n for n in notes))

    def test_nothing_recognized(self):
        with self.assertRaises(ValueError):
            textparse.to_payload("안녕하세요")

    def test_end_to_end_and_re_receive(self):
        conn = db.connect(":memory:")
        seed.seed_demo(conn)
        p, _ = textparse.to_payload(INVOICE_TEXT)
        r = process.intake(conn, p)
        self.assertEqual({c["path"] for c in r["checks"]}, {"vehicle_no", "driver_phone"})
        p2, _ = textparse.to_payload(INVOICE_TEXT)
        self.assertEqual(process.intake(conn, p2)["outcome"], "re_receive")


if __name__ == "__main__":
    unittest.main()
