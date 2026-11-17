"""DEV-003 §5 완료 조건 검증. 실행: python -m unittest tests.test_dev003 -v (시험용 가상 데이터)"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from etm import db, learn, plan, process, seed
from etm.learn import Conflict, Forbidden
from etm.process import Tx
from tests.test_dev002 import shipment


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.c = db.connect(self.path)
        seed.seed_demo(self.c)
        self.ev = process.intake(self.c, shipment(12, 13))["event_id"]
        self.intake = self.c.execute("SELECT id FROM intake WHERE event_id=?", (self.ev,)).fetchone()[0]
        with Tx(self.c):
            self.lag = learn.create_lag(self.c, actor="staff", question="부분 회수 처리 방식이 정확한가?",
                                        context={}, event_ids=[self.ev])

    def tearDown(self):
        process.FAULT_HOOK = None
        self.c.close()
        self.tmp.cleanup()

    def cmd(self, fn, **kw):
        with Tx(self.c):
            return fn(self.c, **kw)

    def new_plan(self, **kw):
        args = dict(actor="staff", title="부분 회수 사례 대조", purpose="LAG 질문 검증", action="부분 회수 사례 2건을 기존 기록과 대조",
                    done_criteria="대조 결과와 증빙 2건 연결", links=[{"role": "basis", "kind": "lag", "id": self.lag}])
        args.update(kw)
        return self.cmd(plan.create, **args)

    def run_to_progress(self, pid, by="owner"):
        self.cmd(plan.approve, plan_id=pid, actor=by)
        self.cmd(plan.transition, plan_id=pid, actor=by, to="IN_PROGRESS")

    def ledger(self):
        return (process.summary(self.c),
                self.c.execute("SELECT COUNT(*), COALESCE(SUM(qty),0) FROM value_move").fetchone()[:],
                self.c.execute("SELECT COUNT(*) FROM money_claim").fetchone()[0])


class TestDev003(Base):
    # 1) DB 저장·재접속 조회, 사건·증빙·LAG·RBK 버전 연결 추적
    def test_01_persist_and_trace_links(self):
        rv = self.c.execute("SELECT id FROM rule_version WHERE rule_id='R-PALLET-CHECK'").fetchone()[0]
        pid = self.new_plan(links=[{"role": "basis", "kind": "lag", "id": self.lag},
                                   {"role": "target", "kind": "event", "id": self.ev},
                                   {"role": "basis", "kind": "intake", "id": self.intake},
                                   {"role": "target", "kind": "rule_version", "id": rv}])
        self.c.close()
        self.c = db.connect(self.path)
        d = plan.detail(self.c, pid)
        self.assertEqual(d["status"], "DRAFT")
        self.assertEqual(sorted(l["kind"] for l in d["links"]), ["event", "intake", "lag", "rule_version"])
        self.assertTrue(any("R-PALLET-CHECK v1" == l["target"] for l in d["links"]))
        self.assertEqual(process.event_detail(self.c, self.ev)["plans"][0]["id"], pid)
        lag = next(x for x in learn.overview(self.c)["lags"] if x["id"] == self.lag)
        self.assertEqual(lag["plans"][0]["id"], pid)

    # 2) 한 질문에 복수 계획, 계획 완료와 질문 판정은 독립
    def test_02_many_plans_one_question_independent(self):
        p1 = self.new_plan(title="사례 확보")
        p2 = self.new_plan(title="기존 기록 대조")
        # 한 계획이 여러 LAG 에도 연결
        with Tx(self.c):
            lag2 = learn.create_lag(self.c, actor="staff", question="회수 수량 기준은?", context={})
        self.cmd(plan.link, plan_id=p2, actor="staff", role="basis", kind="lag", target_id=lag2)
        self.assertEqual({p["id"] for p in plan.list_plans(self.c, lag_id=self.lag)}, {p1, p2})
        for p in (p1, p2):
            self.run_to_progress(p)
            self.cmd(plan.complete, plan_id=p, actor="staff", result="대조 완료, 차이 없음",
                     evidence=[{"kind": "event", "id": self.ev}, {"kind": "intake", "id": self.intake}])
        lag = self.c.execute("SELECT status FROM lag WHERE id=?", (self.lag,)).fetchone()[0]
        self.assertEqual(lag, "open")       # 계획 완료가 LAG 판정을 일으키지 않음
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM judgment").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM rule_version WHERE status='active'").fetchone()[0], 0)
        # 근거가 불충분하면 LAG 는 계획 완료 후에도 보류로 남을 수 있다
        self.cmd(learn.judge_lag, lag_id=self.lag, actor="owner", kind="hold", reason="표본 부족")
        self.assertEqual(self.c.execute("SELECT status FROM lag WHERE id=?", (self.lag,)).fetchone()[0], "held")
        self.assertEqual({plan.detail(self.c, p)["status"] for p in (p1, p2)}, {"DONE"})

    # 3) 미승인 초안의 임의 실행 전환 금지
    def test_03_no_unapproved_execution(self):
        pid = self.new_plan()
        with self.assertRaises(Conflict):
            self.cmd(plan.transition, plan_id=pid, actor="owner", to="IN_PROGRESS")
        with self.assertRaises(Forbidden):          # 담당자도 승인권자도 아닌 직원
            self.cmd(plan.approve, plan_id=pid, actor="staff")
        with self.assertRaises(Forbidden):          # 시스템은 실행 지시 불가
            self.cmd(plan.approve, plan_id=pid, actor="system")
        with self.assertRaises(sqlite3.IntegrityError):   # DB 제약: 승인 없는 READY 불가
            self.c.execute("UPDATE plan SET status='READY' WHERE id=?", (pid,))
        # 담당자로 지정된 직원은 실행 지시 가능, 직접 지시는 생성과 한 동작
        p2 = self.new_plan(assignee="staff", execute_now=True)
        self.assertEqual(plan.detail(self.c, p2)["status"], "READY")
        self.assertEqual(plan.detail(self.c, p2)["approved_by"], "staff")

    # 4) 완료 근거 없는 DONE 금지
    def test_04_done_requires_evidence(self):
        pid = self.new_plan()
        self.run_to_progress(pid)
        with self.assertRaises(ValueError):
            self.cmd(plan.complete, plan_id=pid, actor="staff", result="끝", evidence=[])
        with self.assertRaises(ValueError):
            self.cmd(plan.complete, plan_id=pid, actor="staff", result="", evidence=[{"kind": "event", "id": self.ev}])
        with self.assertRaises(sqlite3.IntegrityError):   # DB 트리거·CHECK
            self.c.execute("UPDATE plan SET status='DONE', result='x', done_at='2026' WHERE id=?", (pid,))
        self.assertEqual(plan.detail(self.c, pid)["status"], "IN_PROGRESS")
        self.cmd(plan.complete, plan_id=pid, actor="staff", result="대조 완료", evidence=[{"kind": "event", "id": self.ev}])
        with self.assertRaises(sqlite3.IntegrityError):   # 완료 후 조건 덮어쓰기 금지
            self.c.execute("UPDATE plan SET done_criteria='바뀐 조건' WHERE id=?", (pid,))
        ev_link = next(l for l in plan.detail(self.c, pid)["links"] if l["role"] == "evidence")
        with self.assertRaises(Conflict):
            self.cmd(plan.unlink, link_id=ev_link["id"], actor="owner", reason="실수")

    # 5) 회사 경계를 넘는 참조 금지
    def test_05_company_boundary(self):
        self.c.execute("INSERT INTO company(id, name) VALUES (2, '다른 회사(시험)')")
        other = process.intake(self.c, shipment(5, 5, company_id=2))["event_id"]
        pid = self.new_plan()
        with self.assertRaises(Forbidden):
            self.cmd(plan.link, plan_id=pid, actor="staff", role="target", kind="event", target_id=other)
        with self.assertRaises(sqlite3.IntegrityError):   # DB 트리거
            self.c.execute("INSERT INTO plan_link(plan_id, role, event_id, created_by, created_at) VALUES (?, 'target', ?,"
                           " 'staff', '2026')", (pid, other))
        with self.assertRaises(sqlite3.IntegrityError):   # 연결 삭제 금지(해제만)
            self.c.execute("DELETE FROM plan_link WHERE plan_id=?", (pid,))

    # 6) 중복 재시도·동시 수정·중간 저장 실패
    def test_06_idempotent_concurrent_failure(self):
        for _ in range(2):
            with Tx(self.c):
                learn.idempotent(self.c, "req-plan-1", "plan.create",
                                 lambda: {"plan_id": plan.create(self.c, actor="staff", title="중복 시험")})
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM plan WHERE title='중복 시험'").fetchone()[0], 1)
        pid = self.new_plan()
        v = plan.detail(self.c, pid)["version"]
        self.cmd(plan.edit, plan_id=pid, actor="staff", changes={"due_date": "2026-12-01"}, expected_version=v)
        with self.assertRaises(Conflict):           # 다른 사람이 먼저 고친 뒤 옛 버전으로 수정 시도
            self.cmd(plan.edit, plan_id=pid, actor="owner", changes={"due_date": "2026-12-05"}, expected_version=v)
        self.cmd(plan.approve, plan_id=pid, actor="owner")
        before = plan.detail(self.c, pid)

        def boom():
            raise RuntimeError("저장 실패 흉내")
        process.FAULT_HOOK = boom
        with self.assertRaises(RuntimeError):
            self.cmd(plan.transition, plan_id=pid, actor="owner", to="IN_PROGRESS")
        process.FAULT_HOOK = None
        after = plan.detail(self.c, pid)
        self.assertEqual((after["status"], after["version"], len(after["log"])),
                         (before["status"], before["version"], len(before["log"])))

    # 7) 계획 상태 변경은 재고·채권·채무를 바꾸지 않음, 과거 승인·완료 근거 보존
    def test_07_no_ledger_effect_and_history_kept(self):
        before = self.ledger()
        pid = self.new_plan(links=[{"role": "target", "kind": "event", "id": self.ev}])
        self.run_to_progress(pid)
        self.cmd(plan.complete, plan_id=pid, actor="staff", result="1차 결과", evidence=[{"kind": "event", "id": self.ev}])
        self.cmd(plan.reopen, plan_id=pid, actor="owner", reason="추가 사례 확인")
        self.cmd(plan.complete, plan_id=pid, actor="staff", result="2차 결과", evidence=[{"kind": "intake", "id": self.intake}])
        self.cmd(plan.transition, plan_id=self.new_plan(), actor="owner", to="CANCELLED", reason="중복")
        self.assertEqual(self.ledger(), before)
        d = plan.detail(self.c, pid)
        acts = [x["action"] for x in d["log"]]
        self.assertEqual(acts[:2], ["create", "link"])
        self.assertIn("approve", acts)
        completes = [x for x in d["log"] if x["action"] == "complete"]
        self.assertEqual(len(completes), 2)                 # 1차 완료 근거도 이력에 남음
        self.assertIn("1차 결과", completes[0]["changes"])
        reopen = next(x for x in d["log"] if x["action"] == "reopen")
        self.assertIn("1차 결과", reopen["changes"])

    # 8) 승인된 범위 변경은 다시 판정
    def test_08_scope_change_needs_new_order(self):
        pid = self.new_plan()
        self.run_to_progress(pid)
        r = self.cmd(plan.edit, plan_id=pid, actor="staff", changes={"action": "범위 바꾼 조치"})
        self.assertEqual(r["status"], "DRAFT")
        self.assertIsNone(plan.detail(self.c, pid)["approved_by"])
        r = self.cmd(plan.edit, plan_id=pid, actor="staff", changes={"due_date": "2026-12-31"})
        self.assertEqual(r["status"], "DRAFT")
        # 범위 밖 항목(담당·기한) 변경은 승인을 유지
        p2 = self.new_plan()
        self.cmd(plan.approve, plan_id=p2, actor="owner")
        self.assertEqual(self.cmd(plan.edit, plan_id=p2, actor="staff", changes={"assignee": "staff"})["status"], "READY")

    # 9) 사유 필요 전환, 차단·보류·재개
    def test_09_block_defer_resume(self):
        pid = self.new_plan()
        self.run_to_progress(pid)
        with self.assertRaises(ValueError):
            self.cmd(plan.transition, plan_id=pid, actor="staff", to="BLOCKED")
        self.cmd(plan.transition, plan_id=pid, actor="staff", to="BLOCKED", reason="사례 자료 미수신")
        self.assertEqual(plan.detail(self.c, pid)["status_reason"], "사례 자료 미수신")
        with self.assertRaises(Forbidden):
            self.cmd(plan.transition, plan_id=pid, actor="staff", to="READY", reason="자료 옴")
        self.cmd(plan.transition, plan_id=pid, actor="owner", to="READY", reason="자료 옴")
        self.cmd(plan.transition, plan_id=pid, actor="owner", to="DEFERRED", reason="성수기")
        self.cmd(plan.transition, plan_id=pid, actor="owner", to="CANCELLED", reason="불필요")
        self.assertEqual(plan.detail(self.c, pid)["status"], "CANCELLED")

    # 10) 잘못 분류된 기존 항목: 출처 → 새 대상 → 근거 연결, 재실행 중복 없음, 가상 복원 없음
    def test_10_reclassification(self):
        rows = {(r["source_table"], r["source_id"]): r for r in plan.reclassification_table(self.c)}
        self.assertEqual(rows[("curriculum", 2)]["target_layer"], "TRNK-PLN")
        self.assertEqual(rows[("curriculum", 4)]["target_layer"], "TRNK-PLN")
        self.assertEqual(rows[("lag", 2)]["target_ref"], "Docs LAG v0.1 LAG-001")
        self.assertEqual(rows[("lag", 1)]["status"], "pending_trnk")       # Docs 대응 ID 없음 → 등록 대기
        self.assertEqual({r[0] for r in self.c.execute("SELECT layer FROM lag WHERE id IN (1,2)")}, {"trnk"})
        self.assertNotIn(1, [l["id"] for l in learn.overview(self.c)["lags"]])   # 운영 목록에서 제외
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM plan WHERE origin LIKE 'curriculum%'").fetchone()[0], 0)
        n = len(rows)
        self.assertEqual(plan.reclassify_legacy(self.c), [])               # 재실행: 변화 없음
        self.assertEqual(len(plan.reclassification_table(self.c)), n)
        notes = [r[0] for r in self.c.execute("SELECT note FROM lag_log WHERE lag_id=2 ORDER BY id")]
        self.assertTrue(any("DEV-003 재분류" in x for x in notes))         # 이력 보존
        with self.assertRaises(ValueError):                               # 재분류된 설계 LAG 에는 운영 계획 연결 안 함
            self.new_plan(links=[{"role": "basis", "kind": "lag", "id": 2}])


class TestPlanToLagLoop(Base):
    """수정 결함: PLN 실행 결과·사건·증빙을 LAG 검증 근거로 쓸 수 있어야 한다(DEV-003 §4)."""

    def done_plan(self, **kw):
        pid = self.new_plan(**kw)
        self.run_to_progress(pid)
        self.cmd(plan.complete, plan_id=pid, actor="staff", result="10건 중 2건에서 메시지가 1개 많음",
                 evidence=[{"kind": "event", "id": self.ev}])
        return pid

    def test_plan_result_closes_the_loop(self):
        pid = self.done_plan()
        vid = self.cmd(learn.add_verification, lag_id=self.lag, actor="staff", method="실행계획 결과 대조",
                       plan_ids=[pid], expected="반복 여부 확인")
        v = self.c.execute("SELECT * FROM verification WHERE id=?", (vid,)).fetchone()
        self.assertIn("10건 중 2건", v["observed"])                     # PLN 결과를 다시 입력하지 않고 참조
        self.assertEqual(learn.verification_evidence(self.c, vid), [f"PLN-{pid}"])
        self.assertEqual(self.c.execute("SELECT status FROM lag WHERE id=?", (self.lag,)).fetchone()[0], "awaiting")
        self.cmd(learn.judge_lag, lag_id=self.lag, actor="owner", kind="rejected", reason="상차 직후 추가분 — 규칙 불필요")
        self.assertEqual(self.c.execute("SELECT status, outcome FROM lag WHERE id=?", (self.lag,)).fetchone()[:],
                         ("closed", "rejected"))
        self.assertEqual(plan.detail(self.c, pid)["status"], "DONE")    # 판정이 계획을 바꾸지 않음

    def test_event_evidence_needs_observation(self):
        with self.assertRaises(ValueError):          # 사건만으로는 관찰 결과를 적어야 함
            self.cmd(learn.add_verification, lag_id=self.lag, actor="staff", method="현장 확인", event_ids=[self.ev],
                     expected="x")
        self.cmd(learn.add_verification, lag_id=self.lag, actor="staff", method="현장 확인", event_ids=[self.ev],
                 intake_ids=[self.intake], expected="x", observed="현장 수량 13개 확인")
        self.assertEqual(self.c.execute("SELECT status FROM lag WHERE id=?", (self.lag,)).fetchone()[0], "awaiting")

    def test_guards(self):
        draft = self.new_plan()
        with self.assertRaises(ValueError):          # 완료 전 계획은 근거가 아님
            self.cmd(learn.add_verification, lag_id=self.lag, actor="staff", method="m", plan_ids=[draft], expected="x")
        with self.assertRaises(ValueError):          # 근거 없음
            self.cmd(learn.add_verification, lag_id=self.lag, actor="staff", method="m", expected="x")
        with self.assertRaises(Forbidden):
            self.cmd(learn.add_verification, lag_id=self.lag, actor="system", method="m", event_ids=[self.ev],
                     expected="x", observed="o")
        self.c.execute("INSERT INTO company(id, name) VALUES (2, '다른 회사(시험)')")
        other = process.intake(self.c, shipment(5, 5, company_id=2))["event_id"]
        with self.assertRaises(Forbidden):           # 회사 경계
            self.cmd(learn.add_verification, lag_id=self.lag, actor="staff", method="m", event_ids=[other],
                     expected="x", observed="o")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM verification").fetchone()[0], 0)   # 실패는 흔적 없음


class TestMigrationOfExistingDb(unittest.TestCase):
    def test_upgrade_v3_database_preserves_data(self):
        """0003 까지 적용된 DB(기존 사용자 DB)에 0004 를 비파괴로 적용."""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        saved = db.MIGRATIONS[:]
        try:
            db.MIGRATIONS[:] = saved[:3]
            db.migrate(conn)
        finally:
            db.MIGRATIONS[:] = saved
        conn.close()
        c = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("rulebook", "curriculum", "lag", "rule_version")}
        db.migrate(c)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        self.assertEqual(before, after)                                    # 지운 것 없음
        self.assertEqual(c.execute("SELECT COUNT(*) FROM reclassification").fetchone()[0], 6)
        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
