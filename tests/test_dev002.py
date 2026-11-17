"""DEV-002 §5 검증 시나리오. 실행: python -m unittest tests.test_dev002 -v
예시 데이터는 모두 시험용(가상)이다."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest

from etm import db, learn, process, seed
from etm.process import Tx

N = [0]


def shipment(invoice, message, customer="금성백조", company_id=1):
    """손글씨 표시 없는 시험용 출하. 팔레트 수만 바꿔 쓴다."""
    N[0] += 1
    p = copy.deepcopy(seed.examples()["scan_normal"])
    p.pop("meta")
    p.update(input_id=f"t-{N[0]}", doc_no=f"T-{N[0]:04d}", trade_date=f"2026-11-{10 + N[0] % 18:02d}",
             customer=customer, company_id=company_id)
    if customer != "금성백조":
        p["site"] = None
        p["contact"] = {}
    p["lines"] = [{"item": "보도블록A", "qty": 10, "unit": "EA"}]
    p["pallets"] = {"invoice": invoice, "message": message}
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.c = db.connect(self.path)
        seed.seed_demo(self.c)
        self.v1 = self.c.execute("SELECT id FROM rule_version WHERE rule_id='R-PALLET-CHECK'").fetchone()[0]

    def tearDown(self):
        process.FAULT_HOOK = None
        self.c.close()
        self.tmp.cleanup()

    def cmd(self, fn, **kw):
        with Tx(self.c):
            return fn(self.c, **kw)

    def pallet_check(self, r):
        return next((c for c in r["checks"] if c["path"] == "pallets.confirmed"), None)

    def party(self, name):
        return self.c.execute("SELECT id FROM party WHERE name=?", (name,)).fetchone()[0]


class TestDev002(Base):
    def test_00_initial_state_has_no_developer_approvals(self):
        self.assertEqual(self.c.execute("SELECT status FROM rule_version WHERE id=?", (self.v1,)).fetchone()[0], "pending")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM judgment").fetchone()[0], 0)
        basis = self.c.execute("SELECT basis FROM rule_version WHERE id=?", (self.v1,)).fetchone()[0]
        self.assertIn("DEV-002", basis)

    # 1) 단일 사건 수정: 사실은 보존, 일반 규칙은 생기지 않음
    def test_01_single_event_fact_only(self):
        r = process.intake(self.c, shipment(12, 13))
        chk = self.pallet_check(r)
        self.assertEqual(chk["kind"], "pallet_mismatch")
        self.assertIn("활성 규칙 없음", chk["detail"]["why"])
        process.answer(self.c, r["event_id"], {str(chk["id"]): {"value": 13, "reason": "현장 확인"}}, actor="staff")
        _, f = process.load_event(self.c, r["event_id"])
        self.assertEqual((f["pallets.confirmed"]["value"], f["pallets.confirmed"]["source"]), (13, "user"))
        moved = self.c.execute("SELECT SUM(qty) FROM value_move WHERE event_id=? AND owner LIKE 'party:%'",
                               (r["event_id"],)).fetchone()[0]
        self.assertEqual(moved, 13)
        cc = self.c.execute("SELECT * FROM confirmed_case WHERE event_id=? AND path='pallets.confirmed'",
                            (r["event_id"],)).fetchone()
        self.assertEqual((cc["actor"], cc["reason"]), ("staff", "현장 확인"))
        self.assertTrue(cc["source_ref"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM rule_version WHERE status='active'").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM rule").fetchone()[0], 1)
        # KPP 비용을 팔레트 규칙으로 계산하지 않음
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM money_claim WHERE party_name LIKE '%KPP%'").fetchone()[0], 0)

    # 2) LAG 검증·판정: 복수 근거·검증 기록, 보류·재개·기각 이력
    def test_02_lag_verify_judge_history(self):
        evs = [process.intake(self.c, shipment(5, 6))["event_id"] for _ in range(2)]
        lag = self.cmd(learn.create_lag, actor="staff", question="상차 메시지가 늘 1개 많은가?",
                       context={"note": "팔레트 차이 경향"}, event_ids=evs)
        with self.assertRaises(learn.Conflict):      # 검증 없이 판정 불가
            self.cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="rejected")
        self.cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="hold", reason="자료 더 필요")
        self.cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="reopen", reason="사례 추가")
        # 검증 기록 2건 (doc_kind 사례 사용)
        from tests.test_learning import TEXT
        from etm import textparse
        cases = []
        for n in (1, 2):
            p, _ = textparse.to_payload(TEXT.format(n=n), process.customer_names(self.c))
            r = process.intake(self.c, p)
            process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": "receipt"}})
            cases.append(self.c.execute("SELECT MAX(id) FROM decision_case").fetchone()[0])
        self.cmd(learn.add_verification, lag_id=lag, actor="staff", method="사례 대조", case_ids=cases[:1], expected="x")
        self.cmd(learn.add_verification, lag_id=lag, actor="staff", method="사례 대조", case_ids=cases, expected="x",
                 limits="표본 2건")
        self.cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="rejected", reason="관련 없음")
        self.cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="reopen", reason="새 증거")
        log = [r["to_status"] for r in self.c.execute("SELECT to_status FROM lag_log WHERE lag_id=? ORDER BY id", (lag,))]
        self.assertEqual(log, ["open", "held", "open", "awaiting", "closed", "open"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM verification WHERE lag_id=?", (lag,)).fetchone()[0], 2)
        self.assertGreaterEqual(self.c.execute("SELECT COUNT(*) FROM lag_link WHERE lag_id=?", (lag,)).fetchone()[0], 4)
        kinds = [r[0] for r in self.c.execute("SELECT kind FROM judgment WHERE lag_id=? ORDER BY id", (lag,))]
        self.assertEqual(kinds, ["hold", "reopen", "rejected", "reopen"])
        with self.assertRaises(learn.Forbidden):     # 시스템은 판정 불가
            self.cmd(learn.judge_lag, lag_id=lag, actor="system", kind="hold")

    # 3) 규칙 승인: 권한 있는 사용자가 정확한 버전만
    def test_03_approval_requires_authority(self):
        with self.assertRaises(learn.Forbidden):
            self.cmd(learn.approve_version, version_id=self.v1, actor="staff")
        self.assertEqual(self.c.execute("SELECT status FROM rule_version WHERE id=?", (self.v1,)).fetchone()[0], "pending")
        self.cmd(learn.approve_version, version_id=self.v1, actor="owner")
        j = self.c.execute("SELECT * FROM judgment WHERE rule_version_id=?", (self.v1,)).fetchone()
        self.assertEqual((j["kind"], j["actor"], j["role"]), ("approve", "owner", "approver"))
        with self.assertRaises(learn.Conflict):      # 이미 활성인 버전 재승인 불가
            self.cmd(learn.approve_version, version_id=self.v1, actor="owner")
        with self.assertRaises(sqlite3.IntegrityError):   # 버전 내용은 바꿀 수 없음
            self.c.execute("UPDATE rule_version SET action='{}' WHERE id=?", (self.v1,))

    # 4) 규칙 재사용: 버전·근거·결과가 남음
    def test_04_rule_reuse(self):
        self.cmd(learn.approve_version, version_id=self.v1, actor="owner")
        r = process.intake(self.c, shipment(10, 10))
        self.assertIsNone(self.pallet_check(r))
        a = self.c.execute("SELECT * FROM rule_application WHERE event_id=?", (r["event_id"],)).fetchone()
        self.assertEqual((a["rule_version_id"], a["result"]), (self.v1, "performed"))
        basis = json.loads(a["match_basis"])
        self.assertEqual((basis["company_id"], basis["event_kind"]), (1, "shipment"))
        self.assertEqual(json.loads(a["input_snapshot"])["invoice"], 10)
        r2 = process.intake(self.c, shipment(12, 13))
        chk = self.pallet_check(r2)
        self.assertEqual(chk["rule_id"], "R-PALLET-CHECK v1")      # 어느 버전이 왜 확인을 요구했는지
        a2 = self.c.execute("SELECT result FROM rule_application WHERE event_id=?", (r2["event_id"],)).fetchone()
        self.assertEqual(a2[0], "required_confirm")
        d = process.event_detail(self.c, r["event_id"])
        self.assertEqual(d["rule_applications"][0]["rule"], "R-PALLET-CHECK v1")

    # 5) 범위 격리: 다른 회사·거래처에는 적용 안 됨
    def test_05_scope_isolation(self):
        gs = self.party("금성백조")
        v2 = self.cmd(learn.create_version, rule_id="R-PALLET-CHECK", actor="owner", condition={},
                      action={"type": "compare_pallets", "on_match": "accept", "on_mismatch": "require_confirm"},
                      scope={"party_id": gs}, basis="금성백조 현장만", description="금성백조 출하만")
        self.cmd(learn.approve_version, version_id=v2, actor="owner")
        self.assertIsNone(self.pallet_check(process.intake(self.c, shipment(5, 5))))          # 금성백조: 적용
        r = process.intake(self.c, shipment(5, 5, customer="나성씨앤디"))                         # 다른 거래처
        self.assertEqual(self.pallet_check(r)["kind"], "pallet_confirm")
        self.c.execute("INSERT INTO company(id, name) VALUES (2, '다른 회사(시험)')")
        r = process.intake(self.c, shipment(5, 5, company_id=2))                                # 다른 회사
        self.assertEqual(self.pallet_check(r)["kind"], "pallet_confirm")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM rule_application WHERE event_id=?",
                                        (r["event_id"],)).fetchone()[0], 0)

    # 6) 충돌: 해당 처리만 보류, 이유 표시
    def test_06_conflict_holds_only_that(self):
        self.cmd(learn.approve_version, version_id=self.v1, actor="owner")
        self.c.execute("INSERT INTO rule(id, company_id, event_kind, decision, title, created_by, created_at)"
                       " VALUES ('R-TEST', 1, 'shipment', 'pallet_check', '시험: 일치해도 확인', 'owner', '2026-11-17')")
        v = self.cmd(learn.create_version, rule_id="R-TEST", actor="owner", condition={},
                     action={"type": "compare_pallets", "on_match": "require_confirm", "on_mismatch": "require_confirm"},
                     scope={}, basis="시험", description="시험")
        self.cmd(learn.approve_version, version_id=v, actor="owner")
        before = process.factory_stock(self.c, 1)
        r = process.intake(self.c, shipment(7, 7))
        chk = self.pallet_check(r)
        self.assertIn("충돌", chk["detail"]["why"])
        res = [x[0] for x in self.c.execute("SELECT result FROM rule_application WHERE event_id=?", (r["event_id"],))]
        self.assertEqual(res, ["held_conflict", "held_conflict"])
        self.assertEqual(process.factory_stock(self.c, 1), before - 10)   # 제품 출하는 진행

    # 7) 버전 교체·중지: 이후 적용만 바뀌고 과거 이력·사실은 유지
    def test_07_replace_and_suspend(self):
        self.cmd(learn.approve_version, version_id=self.v1, actor="owner")
        r1 = process.intake(self.c, shipment(8, 8))
        v2 = self.cmd(learn.create_version, rule_id="R-PALLET-CHECK", actor="owner", condition={},
                      action={"type": "compare_pallets", "on_match": "require_confirm", "on_mismatch": "require_confirm"},
                      scope={}, basis="항상 확인으로 변경", description="항상 확인")
        self.cmd(learn.approve_version, version_id=v2, actor="owner")
        self.assertEqual(self.c.execute("SELECT status FROM rule_version WHERE id=?", (self.v1,)).fetchone()[0], "superseded")
        r2 = process.intake(self.c, shipment(8, 8))
        self.assertEqual(self.pallet_check(r2)["rule_id"], "R-PALLET-CHECK v2")
        old = self.c.execute("SELECT rule_version_id FROM rule_application WHERE event_id=?", (r1["event_id"],)).fetchone()
        self.assertEqual(old[0], self.v1)                       # 과거 적용 이력 유지
        _, f = process.load_event(self.c, r1["event_id"])
        self.assertEqual(f["pallets.confirmed"]["value"], 8)     # 과거 사건 사실 유지(재계산 없음)
        self.cmd(learn.set_version_status, version_id=v2, actor="owner", to="suspended", reason="시험")
        r3 = process.intake(self.c, shipment(8, 8))
        self.assertIn("활성 규칙 없음", self.pallet_check(r3)["detail"]["why"])

    # 8) 중복 요청·동시 승인
    def test_08_idempotent_and_stale_approval(self):
        for _ in range(2):
            with Tx(self.c):
                learn.idempotent(self.c, "req-1", "approve",
                                 lambda: learn.approve_version(self.c, version_id=self.v1, actor="owner"))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM judgment WHERE rule_version_id=?", (self.v1,)).fetchone()[0], 1)
        # 다른 사람이 v2·v3 를 만든 뒤 오래된 v2 를 승인하려 하면 거부
        v2 = self.cmd(learn.create_version, rule_id="R-PALLET-CHECK", actor="owner", condition={},
                      action={"type": "compare_pallets", "on_match": "accept", "on_mismatch": "require_confirm"},
                      scope={}, basis="b", description="v2")
        self.cmd(learn.create_version, rule_id="R-PALLET-CHECK", actor="owner", condition={},
                 action={"type": "compare_pallets", "on_match": "require_confirm", "on_mismatch": "require_confirm"},
                 scope={}, basis="b", description="v3")
        with self.assertRaises(learn.Conflict):
            self.cmd(learn.approve_version, version_id=v2, actor="owner")
        # 같은 확인 응답을 두 번 보내도 효과 1번
        r = process.intake(self.c, shipment(12, 13))
        chk = self.pallet_check(r)
        for _ in range(2):
            process.answer(self.c, r["event_id"], {str(chk["id"]): {"value": 13}})
        q = self.c.execute("SELECT SUM(qty) FROM value_move WHERE event_id=? AND owner LIKE 'party:%'",
                           (r["event_id"],)).fetchone()[0]
        self.assertEqual(q, 13)

    # 9) 중간 저장 실패: 승인·활성화 불일치 없음
    def test_09_failure_mid_approval(self):
        self.cmd(learn.approve_version, version_id=self.v1, actor="owner")
        v2 = self.cmd(learn.create_version, rule_id="R-PALLET-CHECK", actor="owner", condition={},
                      action={"type": "compare_pallets", "on_match": "require_confirm", "on_mismatch": "require_confirm"},
                      scope={}, basis="b", description="v2")

        def boom():
            raise RuntimeError("저장 실패 흉내")
        process.FAULT_HOOK = boom
        with self.assertRaises(RuntimeError):
            self.cmd(learn.approve_version, version_id=v2, actor="owner")
        process.FAULT_HOOK = None
        st = dict(self.c.execute("SELECT id, status FROM rule_version WHERE rule_id='R-PALLET-CHECK'").fetchall())
        self.assertEqual((st[self.v1], st[v2]), ("active", "pending"))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM judgment WHERE rule_version_id=?", (v2,)).fetchone()[0], 0)

    # 10) 재시작 후 사례 → LAG → 검증 → 판정 → 규칙 버전 → 적용 사건 추적
    def test_10_trace_after_restart(self):
        from tests.test_learning import TEXT
        from etm import textparse
        cases = []
        for n in (1, 2, 3):
            p, _ = textparse.to_payload(TEXT.format(n=n), process.customer_names(self.c))
            r = process.intake(self.c, p)
            process.answer(self.c, r["event_id"], {str(r["checks"][0]["id"]): {"choice": "other"}})
        cand = next(x for x in learn.candidates(self.c) if x["decision"] == "doc_kind")
        lag = self.cmd(learn.create_lag, actor="staff", question="이 양식은 보관만?",
                       context={"decision": "doc_kind", "key": cand["key"], "label": cand["label"]}, case_ids=cand["cases"])
        self.cmd(learn.add_verification, lag_id=lag, actor="staff", method="사례 대조", case_ids=cand["cases"],
                 expected="모두 보관만")
        vid = self.cmd(learn.judge_lag, lag_id=lag, actor="owner", kind="rule")["rule_version_id"]
        self.cmd(learn.approve_version, version_id=vid, actor="owner")
        p, _ = textparse.to_payload(TEXT.format(n=4), process.customer_names(self.c))
        applied_event = process.intake(self.c, p)["event_id"]
        self.c.close()
        self.c = db.connect(self.path)
        t = learn.trace_version(self.c, vid)
        self.assertEqual(t["lag"]["id"], lag)
        self.assertEqual(len(t["cases"]), 3)
        self.assertEqual(len(t["verifications"]), 1)
        self.assertEqual([j["kind"] for j in t["judgments"]], ["rule", "approve"])
        self.assertEqual(t["applications"][0]["event_id"], applied_event)


if __name__ == "__main__":
    unittest.main()
