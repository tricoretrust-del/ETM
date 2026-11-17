"""입고 자료로 본 학습 경로: 사례 → (후보 제시) → LAG → 검증 → 판정 → 승인 → 적용 → 중지."""
import unittest

from etm import db, learn, process, seed, textparse
from etm.process import Tx

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

    def answer_all(self, r, kind="receipt"):
        asked = []
        while r["checks"]:
            ch = r["checks"][0]
            asked.append(ch["kind"])
            a = {"doc_kind": {"choice": kind}, "item_map": {"choice": f"item:{self.kpp}"}}[ch["kind"]]
            r = process.answer(self.c, r["event_id"], {str(ch["id"]): a})
        return asked, r

    def factory_pallets(self):
        rf = process.summary(self.c)["rental_factory"]
        return rf[0]["qty"] if rf else 0

    def learn_cmd(self, fn, **kw):
        with Tx(self.c):
            return fn(self.c, **kw)

    def test_full_path_questions_decrease(self):
        base = self.factory_pallets()
        for n in (1, 2, 3):
            asked, _ = self.answer_all(self.send(n))
            self.assertEqual(asked, ["doc_kind", "item_map"])      # 규칙 전에는 매번 묻는다
        self.assertEqual(self.factory_pallets(), base + 291)      # 방향은 사용자가 정한 '입고'
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM lag WHERE origin IS NULL").fetchone()[0], 0)  # 자동 LAG 없음
        cands = learn.candidates(self.c)
        self.assertEqual({x["decision"] for x in cands}, {"doc_kind", "item_map"})

        version_ids = []
        for cand in cands:
            lag = self.learn_cmd(learn.create_lag, actor="staff", question=f"{cand['label']} 판정을 규칙으로 할 수 있나",
                                 context={"decision": cand["decision"], "key": cand["key"], "label": cand["label"]},
                                 case_ids=cand["cases"])
            self.learn_cmd(learn.add_verification, lag_id=lag, actor="staff", method="사례 대조",
                           case_ids=cand["cases"], expected="모든 사례가 같은 답")
            res = self.learn_cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="rule", reason="반복 확인됨")
            version_ids.append(res["rule_version_id"])
        r = self.send(4)
        self.assertEqual(r["checks"][0]["kind"], "doc_kind")      # 판정만으로는 활성화되지 않는다(승인 대기)
        self.answer_all(r)
        for vid in version_ids:
            self.learn_cmd(learn.approve_version, version_id=vid, actor="owner")
        r = self.send(5)
        self.assertEqual((r["outcome"], r["checks"]), ("new", []))   # 더 이상 묻지 않음
        self.assertEqual(self.factory_pallets(), base + 97 * 5)
        apps = self.c.execute("SELECT * FROM rule_application WHERE event_id=? AND result='performed'",
                              (r["event_id"],)).fetchall()
        self.assertEqual(len(apps), 2)
        self.assertIn("규칙 R-L", process.history(self.c)[0]["items"][0]["summary"])

        # 중지 → 다시 묻는다
        self.learn_cmd(learn.set_version_status, version_id=version_ids[0], actor="owner", to="suspended")
        r = self.send(6)
        self.assertTrue(r["checks"])

    def test_inconsistent_cases_cannot_become_rule(self):
        for n, kind in ((1, "receipt"), (2, "other"), (3, "receipt")):
            r = self.send(n)
            process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": kind}})
        cand = next(x for x in learn.candidates(self.c) if x["decision"] == "doc_kind")
        self.assertFalse(cand["consistent"])
        lag = self.learn_cmd(learn.create_lag, actor="staff", question="종류가 엇갈림",
                             context={"decision": "doc_kind", "key": cand["key"]}, case_ids=cand["cases"])
        self.learn_cmd(learn.add_verification, lag_id=lag, actor="staff", method="사례 대조", case_ids=cand["cases"],
                       expected="같은 답")
        with self.assertRaises(learn.Conflict):
            self.learn_cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="rule")

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
