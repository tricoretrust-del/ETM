"""판단 위임 → 확정 사례 → 규칙 제안 → 승인 → 묻지 않음 / 중지 → 다시 묻기."""
import unittest

from etm import db, learn, process, seed, textparse

TEXT = """1. 업체명: (사진에 안나옴) LOGISALL
2. 전표번호: A1488406{n}
3. 입고일: 2026-11-1{n}
4. 납품처: 주식회사 지오블록
5. 차량번호: 885968
6. 기사연락처: 01071550855
7. 유형: P10
8. 수량: 97"""


class TestLearning(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        seed.seed_demo(self.c)
        self.kpp = self.c.execute("SELECT id FROM item WHERE kind='rental'").fetchone()[0]

    def send(self, n):
        p, _ = textparse.to_payload(TEXT.format(n=n), process.customer_names(self.c))
        return process.intake(self.c, p)

    def answer_all(self, r):
        asked = []
        while r["checks"]:
            ch = r["checks"][0]
            asked.append(ch["kind"])
            a = {"doc_kind": {"choice": "receipt"}, "item_map": {"choice": f"item:{self.kpp}"}}[ch["kind"]]
            r = process.answer(self.c, r["event_id"], {str(ch["id"]): a})
        return asked, r

    def factory_pallets(self):
        rf = process.summary(self.c)["rental_factory"]
        return rf[0]["qty"] if rf else 0

    def test_questions_decrease_after_approval(self):
        own_before = [s["qty"] for s in process.summary(self.c)["stock"]]
        base = self.factory_pallets()
        for n in (1, 2, 3):
            asked, _ = self.answer_all(self.send(n))
            self.assertEqual(asked, ["doc_kind", "item_map"])           # 규칙 전에는 매번 묻는다
        self.assertEqual(self.factory_pallets(), base + 291)           # 입고 = 공장으로 (방향은 사용자가 정함)
        self.assertEqual([s["qty"] for s in process.summary(self.c)["stock"]], own_before)  # 임대품은 자사 재고 아님

        ov = learn.overview(self.c)
        proposals = [x for x in ov["curriculum"] if x["state"] == "proposed"]
        self.assertEqual(len(proposals), 2)
        self.assertFalse(learn.find_rule(self.c, "doc_kind", proposals[0]["decision_key"]))  # 자동 승격 없음

        # 두 번째 자료부터 지난 답을 '제안'으로 보여준다(결정은 아님)
        r = self.send(4)
        self.assertEqual(r["checks"][0]["detail"]["suggestion"]["answer"], "receipt")
        self.answer_all(r)

        for x in proposals:
            with process.Tx(self.c):
                learn.approve(self.c, x["id"])
        r = self.send(5)
        self.assertEqual(r["outcome"], "new")
        self.assertEqual(r["checks"], [])                               # 더 이상 묻지 않음
        self.assertEqual(self.factory_pallets(), base + 97 * 5)
        stats = learn.overview(self.c)["stats"]
        self.assertEqual(stats["auto"], 2)
        hist = process.history(self.c)[0]["items"][0]["summary"]
        self.assertIn("규칙 L-", hist)

        # 규칙 중지 → 다시 묻는다
        rid = learn.find_rule(self.c, "doc_kind", proposals[0]["decision_key"])["id"]
        with process.Tx(self.c):
            learn.suspend(self.c, rid)
        r = self.send(6)
        self.assertEqual([c["kind"] for c in r["checks"]], ["doc_kind"])

    def test_conflicting_answers_are_not_proposed(self):
        r = self.send(1)
        process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": "receipt"}})
        r = self.send(2)
        process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": "other"}})
        r = self.send(3)
        process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": "receipt"}})
        states = {x["state"] for x in learn.overview(self.c)["curriculum"] if x["decision_kind"] == "doc_kind"}
        self.assertEqual(states, {"conflict"})

    def test_other_kind_is_kept_only(self):
        before = process.summary(self.c)
        r = self.send(1)
        r = process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": "other"}})
        self.assertEqual(r["checks"], [])
        self.assertEqual(process.summary(self.c), before)

    def test_answer_then_reload_points_to_new_event(self):
        r = self.send(1)
        pending = r["event_id"]
        r = process.answer(self.c, pending, {str(r["checks"][0]["id"]): {"choice": "receipt"}})
        self.assertNotEqual(r["event_id"], pending)
        d = process.event_detail(self.c, pending)
        self.assertEqual((d["status"], d["merged_into"]), ("merged", r["event_id"]))


if __name__ == "__main__":
    unittest.main()
