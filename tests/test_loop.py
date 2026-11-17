"""학습 폐루프(DEV-004 v0.2 후속 ①): 두 규칙 생성 경로가 같은 조건 형식·같은 적용 절차를 쓰는지.
사례 → LAG → PLN → 검증 → 판정 → 승인 → 다음 자료 자동 처리. 실행: python -m unittest tests.test_loop -v"""
from __future__ import annotations

import json

from etm import db, dialog, learn, plan, process
from etm.process import Tx
from etm.learn import Conflict
from tests.test_dev004 import Base, line, resp

NO_MARK = "LOGISALL 전표 A{n}\n유형: P10\n수량: {q}"     # 원문에 종류 표지 없음 → 종류는 사람이 판단


class Loop(Base):
    def ask_until_done(self, text, kind_choice="입고", owner="KPP"):
        import re
        q_ = float(re.search(r"수량: (\d+)", text)[1])
        self.queue.append(resp(status="ready_for_validation", action="none", kind_source="none", hyp="unknown",
                               lines=[line(qty=q_)]))
        d = dialog.analyze_input(self.c, text=text, actor="owner")
        asked = []
        while d["open_question"]:
            q = d["open_question"]
            asked.append(q["target"])
            d = self.answer(d, choice=kind_choice if q["target"] == "kind" else
                            owner if q["target"].endswith(".owner") else "KPP 팔레트")
        return d, asked

    def full_cycle(self, decision, owner="KPP"):
        """같은 판단의 사례 3건 → 후보 → LAG → PLN(실행·완료) → 검증 → 판정 → 승인. 규칙 버전을 돌려준다."""
        for n in (1, 2, 3):
            d, asked = self.ask_until_done(NO_MARK.format(n=n, q=10 * n), owner=owner)
            self.assertEqual(d["status"], "applied")
            self.assertIn("kind", asked)
        cand = next(x for x in learn.candidates(self.c) if x["decision"] == decision)
        self.assertTrue(cand["consistent"])
        with Tx(self.c):
            lag = learn.create_lag(self.c, actor="staff", question=f"{cand['label']} — 항상 같은가?",
                                   context={"decision": decision, "key": cand["key"], "label": cand["label"]},
                                   case_ids=cand["cases"])
            pid = plan.create(self.c, actor="staff", title="실물 대조", action="전표·실물 대조", done_criteria="3건",
                              links=[{"role": "basis", "kind": "lag", "id": lag}])
            plan.approve(self.c, plan_id=pid, actor="owner")
            plan.transition(self.c, plan_id=pid, actor="staff", to="IN_PROGRESS")
            evs = [r[0] for r in self.c.execute("SELECT event_id FROM decision_case WHERE id IN (%s)"
                                                % ",".join(map(str, cand["cases"])))]
            plan.complete(self.c, plan_id=pid, actor="staff", result="3건 일치",
                          evidence=[{"kind": "event", "id": e} for e in evs])
            learn.add_verification(self.c, lag_id=lag, actor="staff", method="사례+실물", case_ids=cand["cases"],
                                   plan_ids=[pid], expected="일치")
            vid = learn.judge_lag(self.c, lag_id=lag, actor="owner", kind="rule", reason="일치")["rule_version_id"]
            learn.approve_version(self.c, version_id=vid, actor="owner")
        return self.c.execute("SELECT * FROM rule_version WHERE id=?", (vid,)).fetchone()

    def test_doc_kind_loop_closes(self):
        v = self.full_cycle("doc_kind")
        self.assertEqual(json.loads(v["condition"]), {"supplier": "logisall", "marker": "없음"})
        d, asked = self.ask_until_done(NO_MARK.format(n=9, q=7))
        self.assertNotIn("kind", asked, "LAG 경로로 만든 종류 규칙이 문답에서 실행되지 않음")
        ev = process.event_detail(self.c, d["event_id"])
        self.assertIn(f"{v['rule_id']} v1", [a["rule"] for a in ev["rule_applications"]])
        # 같은 업체라도 원문에 '반납' 표지가 있으면 이 규칙(표지 없음)은 맞지 않는다
        self.queue.append(resp(status="ready_for_validation", action="none", kind_source="none", hyp="unknown",
                               lines=[line(qty=3)]))
        d2 = dialog.analyze_input(self.c, text="반납전표\n" + NO_MARK.format(n=8, q=3), actor="owner")
        self.assertEqual(d2["open_question"]["target"], "kind")

    def test_item_map_loop_closes(self):
        v = self.full_cycle("item_map")
        self.assertEqual(json.loads(v["condition"]), {"supplier": "logisall", "item_text": "p10"})
        d, asked = self.ask_until_done(NO_MARK.format(n=9, q=7))
        self.assertNotIn("lines.0.item", asked)
        ev = process.event_detail(self.c, d["event_id"])
        self.assertIn(f"{v['rule_id']} v1", [a["rule"] for a in ev["rule_applications"]])

    def test_both_paths_same_condition_shape(self):
        """직접 지시 규칙과 LAG 판정 규칙이 같은 사실 정의(facts_for)를 쓴다."""
        lag_v = self.full_cycle("item_map")
        d, _ = self.ask_until_done(NO_MARK.format(n=5, q=1))
        cid = self.c.execute("SELECT id FROM decision_case WHERE decision_kind='doc_kind' ORDER BY id DESC").fetchone()[0]
        key = self.c.execute("SELECT decision_key FROM decision_case WHERE id=?", (cid,)).fetchone()[0]
        self.assertEqual(json.loads(key), learn.facts_for("doc_kind", supplier="LOGISALL", raw=NO_MARK))
        with Tx(self.c):
            r = learn.directive_rule(self.c, actor="owner", company_id=1, decision="item_map",
                                     condition=learn.facts_for("item_map", supplier="LOGISALL", item_text="P20"),
                                     action={"type": "map_item", "choice": f"item:{self.kpp()}", "label": "KPP"},
                                     title="t", instruction="앞으로 P20 은 KPP", answer_id=None, event_id=None)
        dv = self.c.execute("SELECT condition FROM rule_version WHERE id=?", (r["rule_version_id"],)).fetchone()[0]
        self.assertEqual(set(json.loads(dv)), set(json.loads(lag_v["condition"])))

    def test_unspecific_rule_refused(self):
        with Tx(self.c), self.assertRaises(learn.Conflict):
            learn.directive_rule(self.c, actor="owner", company_id=1, decision="doc_kind",
                                 condition=learn.facts_for("doc_kind", supplier="", raw="메모"),
                                 action={"type": "set_doc_kind", "value": "receipt"}, title="t", instruction="x",
                                 answer_id=None, event_id=None)


class Migration0007(Base):
    def test_legacy_keys_converted(self):
        path = self.c.execute("PRAGMA database_list").fetchone()[2]
        with Tx(self.c):
            self.c.execute("INSERT INTO decision_case(event_id, decision_kind, decision_key, label, answer, actor, at)"
                           " VALUES (1, 'item_map', 'logisall|p10', 'x', '{}', 'owner', 't')")
            self.c.execute("INSERT INTO decision_case(event_id, decision_kind, decision_key, label, answer, actor, at)"
                           " VALUES (1, 'doc_kind', '수량·업체명', 'x', '\"other\"', 'owner', 't')")
            self.c.execute("ALTER TABLE learning_candidate DROP COLUMN payload")   # 0008 이전 상태로 되돌림
            self.c.execute("PRAGMA user_version = 6")
        self.c.close()
        self.c = db.connect(path)
        keys = [json.loads(r[0]) for r in self.c.execute("SELECT decision_key FROM decision_case ORDER BY id DESC LIMIT 2")]
        self.assertIn({"supplier": "logisall", "item_text": "p10"}, keys)
        self.assertIn({"form_key": "수량·업체명"}, keys)


class Counterparty(Base):
    def test_empty_counterparty_asked_then_known_from_cases(self):
        self.queue.append(resp(counterparty=None, lines=[line(qty=1)]))
        d = dialog.analyze_input(self.c, text=NO_MARK.format(n=1, q=1), actor="owner")
        self.assertEqual(d["open_question"]["target"], "counterparty")
        d = self.answer(d, text="LOGISALL")
        while d["open_question"]:
            t = d["open_question"]["target"]
            d = self.answer(d, choice="입고" if t == "kind" else "KPP" if t.endswith(".owner") else "KPP 팔레트")
        self.assertEqual(d["status"], "applied")
        # 다음 자료: 모델이 또 비워도, 원문에 적힌 이름이 확정 사례의 업체와 같으면 묻지 않는다
        self.queue.append(resp(counterparty=None, lines=[line(qty=2)]))
        d2 = dialog.analyze_input(self.c, text=NO_MARK.format(n=2, q=2), actor="owner")
        self.assertNotEqual((d2["open_question"] or {}).get("target"), "counterparty")
        self.assertEqual(d2["state"]["counterparty"]["source"], "document_name")


class Slots(Base):
    """② 근거별 확정 범위: 근거는 자기가 확정할 수 있는 칸만 채운다."""

    def conf(self, d):
        v = self.c.execute("SELECT validation FROM dialog_proposal WHERE dialog_id=? AND status='applied'",
                           (d["id"],)).fetchone()[0]
        return json.loads(v)["confirmations"]

    def test_model_qty_not_in_document_is_confirmed(self):
        self.queue = [resp(lines=[line(qty=97)])]
        d = dialog.analyze_input(self.c, text=NO_MARK.format(n=1, q=10).replace("LOGISALL 전표", "LOGISALL 입고 전표"),
                                 actor="owner")
        q = d["open_question"]
        self.assertEqual((q["target"], q["proposed"]), ("lines.0.qty", "97"))
        d = self.answer(d, text="10")
        d = self.finish(self.answer(d, choice="KPP 팔레트"))
        self.assertEqual(d["status"], "applied")
        self.assertEqual(self.conf(d)["lines.0.qty"], {"value": 10.0, "evidence": "user_answer",
                                                        "answer_id": d["answers"][0]["id"]})

    def test_evidence_per_slot(self):
        self.queue = [resp()]
        from tests.test_dev004 import J01
        d = dialog.analyze_input(self.c, text=J01, actor="owner")
        d = self.finish(self.answer(d, choice="KPP 팔레트"), owner="KPP")
        c = self.conf(d)
        self.assertEqual(c["kind"]["evidence"], "document")           # '입고' 표기 → 종류만
        self.assertEqual(c["lines.0.qty"]["evidence"], "document_value")  # 원문 숫자 → 수량만
        self.assertEqual(c["lines.0.item"]["evidence"], "user_answer")    # 사용자 답 → 품목
        self.assertEqual(c["lines.0.owner"]["evidence"], "user_answer")   # 소유자는 따로 확인

    def test_master_exact_confirms_item_only(self):
        """기준정보 정확 일치는 품목만 확정. 보낸 업체가 등록 소유자(KPP)면 소유자도 확정(근거 다름)."""
        self.queue = [resp(counterparty="KPP", lines=[line("KPP 팔레트", 30)])]
        d = dialog.analyze_input(self.c, text="KPP 입고전표\n품목: KPP 팔레트\n수량: 30", actor="owner")
        self.assertEqual(d["status"], "applied", d["open_question"])
        c = self.conf(d)
        self.assertEqual(c["lines.0.item"]["evidence"], "master_exact")
        self.assertEqual(c["lines.0.owner"]["evidence"], "sender_is_owner")

    def test_owner_answer_changes_ledger_owner(self):
        before = self.factory_kpp()
        self.queue = [resp(lines=[line(qty=20)])]
        d = dialog.analyze_input(self.c, text="LOGISALL 입고전표\n유형: P10\n수량: 20", actor="owner")
        d = self.answer(d, choice="KPP 팔레트")
        self.assertEqual(d["open_question"]["target"], "lines.0.owner")
        self.assertIn("LOGISALL", d["open_question"]["options"])
        d = self.answer(d, choice="LOGISALL")
        self.assertEqual(d["status"], "applied")
        pid_ = self.c.execute("SELECT id FROM party WHERE name='LOGISALL'").fetchone()[0]
        mv = self.c.execute("SELECT owner, qty FROM value_move WHERE event_id=?", (d["event_id"],)).fetchone()
        self.assertEqual((mv["owner"], mv["qty"]), (f"party:{pid_}", 20))
        self.assertEqual(self.factory_kpp(), before, "KPP 소유 수량에 섞이지 않음")


class OwnerLoop(Loop):
    def test_owner_map_loop_closes(self):
        v = self.full_cycle("owner_map", owner="LOGISALL")
        pid_ = self.c.execute("SELECT id FROM party WHERE name='LOGISALL'").fetchone()[0]
        self.assertEqual(json.loads(v["action"])["owner"], f"party:{pid_}")
        d, asked = self.ask_until_done(NO_MARK.format(n=9, q=7), owner="LOGISALL")
        self.assertNotIn("lines.0.owner", asked)
        ev = process.event_detail(self.c, d["event_id"])
        self.assertIn(f"{v['rule_id']} v1", [a["rule"] for a in ev["rule_applications"]])


class CounterpartyEvidence(Base):
    def test_counterparty_not_in_document_is_confirmed(self):
        self.queue = [resp(counterparty="로지스올", lines=[line(qty=5)])]
        d = dialog.analyze_input(self.c, text="입고전표\n유형: P10\n수량: 5", actor="owner")
        q = d["open_question"]
        self.assertEqual((q["target"], q["proposed"]), ("counterparty", "로지스올"))


class ReturnByText(Base):
    def test_return_answered_in_words_is_pending_not_kept(self):
        """'반납이야'처럼 말로 답하고 모델이 동작을 '보관만'으로 내도, 종류 해석이 반납이면 장부 반영 미결."""
        self.queue = [resp(status="needs_user", action="none", kind_source="none", hyp="unknown",
                           question={"question_id": "q1", "target": "kind", "reason": "unknown_meaning",
                                     "text": "무엇인가요?", "options": [], "proposed": None}, lines=[line(qty=20)]),
                      resp(action="keep_only", kind_source="model_inference", hyp="return", lines=[line(qty=20)])]
        d = dialog.analyze_input(self.c, text="반납전표\nLOGISALL\n유형: P10\n수량: 20", actor="owner")
        d = self.answer(d, text="반납이야")
        self.assertEqual(d["status"], "pending_ledger")


class Concurrency(Base):
    """④ API 응답을 기다리는 동안 다른 요청(조회)이 막히지 않고, 같은 문답의 중복 호출은 거부된다."""

    def test_api_wait_does_not_block_and_inflight_rejected(self):
        import threading
        import time
        from etm import llm as llm_
        from etm.server import App
        from tests.test_dev004 import J01
        app = App.__new__(App)
        app.conn, app.lock = self.c, dialog.LOCK
        started, release = threading.Event(), threading.Event()

        def slow(messages):
            started.set()
            release.wait(5)
            return resp()
        llm_.FIXTURE_RESPONDER = slow
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", app.handle("POST", "/api/dialogs", {"text": J01})))
        t.start()
        self.assertTrue(started.wait(5))
        t0 = time.time()
        st, summ = app.handle("GET", "/api/summary", None)      # API 대기 중 다른 요청
        self.assertEqual(st, 200)
        self.assertLess(time.time() - t0, 1.0)
        did = dialog.list_dialogs(self.c)[0]["id"]
        with self.assertRaises(Conflict):                         # 같은 문답 중복 호출
            dialog.retry(self.c, dialog_id=did, actor="owner")
        release.set()
        t.join(5)
        self.assertEqual(out["r"][1]["open_question"]["target"], "lines.0.item")
        n = self.c.execute("SELECT COUNT(*) FROM llm_call WHERE dialog_id=?", (did,)).fetchone()[0]
        self.assertEqual(n, 1)


class SentenceAnswer(Base):
    """'P10은 KPP 팔레트의 모델명이고 이 전표는 LOGISALL이 지오블록의 하차담당자에게 전달한 거야'"""
    A = "P10은 KPP 팔레트의 모델명이고 이 전표는 LOGISALL이 지오블록의 하차담당자에게 전달한 거야"

    def test_sentence_to_owner_question_is_interpreted_not_stored_as_name(self):
        from tests.test_dev004 import J01
        kpp_party = self.c.execute("SELECT id FROM party WHERE name='KPP'").fetchone()[0]
        n_party = self.c.execute("SELECT COUNT(*) FROM party").fetchone()[0]
        self.queue = [resp()]
        d = dialog.analyze_input(self.c, text=J01, actor="owner")
        d = self.answer(d, choice="KPP 팔레트")
        self.assertEqual(d["open_question"]["target"], "lines.0.owner")
        self.queue = [resp(lines=[line(ref=f"item:{self.kpp()}", src="user_answer",
                                       owner=f"party:{kpp_party}", osrc="user_answer")])]
        d = self.answer(d, text=self.A)
        self.assertEqual(d["status"], "applied")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM party").fetchone()[0], n_party, "문장이 업체로 등록됨")
        mv = self.c.execute("SELECT owner FROM value_move WHERE event_id=?", (d["event_id"],)).fetchone()
        self.assertEqual(mv["owner"], f"party:{kpp_party}")

    def test_alias_candidate_then_master_exact(self):
        from tests.test_dev004 import J01
        kpp = self.kpp()
        kpp_party = self.c.execute("SELECT id FROM party WHERE name='KPP'").fetchone()[0]
        self.queue = [resp(),
                      resp(lines=[line(ref=f"item:{kpp}", src="user_answer", owner=f"party:{kpp_party}",
                                       osrc="user_answer")],
                           cands=[{"kind": "alias", "text": "P10 은 KPP 팔레트의 모델명", "scope": "",
                                   "explicit_user_instruction": False, "target_ref": f"item:{kpp}", "alias": "P10"},
                                  {"kind": "alias", "text": "지어낸 별칭", "scope": "",
                                   "explicit_user_instruction": False, "target_ref": f"item:{kpp}", "alias": "Z99"}])]
        d = dialog.analyze_input(self.c, text=J01, actor="owner")
        d = self.answer(d, text=self.A)          # 품목 질문에 문장으로 답함
        self.assertEqual(d["status"], "applied")
        al = [c for c in d["candidates"] if c["kind"] == "alias"]
        self.assertEqual(len(al), 1, "답변에 없는 별칭(Z99)은 후보가 되면 안 됨")
        with self.assertRaises(learn.Forbidden):
            dialog.accept_candidate(self.c, candidate_id=al[0]["id"], actor="staff")
        dialog.accept_candidate(self.c, candidate_id=al[0]["id"], actor="owner")
        # 다음 자료: P10 은 기준정보 정확 일치 → 품목 질문 없음(업체와 무관)
        self.queue = [resp(counterparty="다른업체", lines=[line(qty=5)])]
        d2 = dialog.analyze_input(self.c, text="다른업체 입고전표\n유형: P10\n수량: 5", actor="owner")
        self.assertNotEqual((d2["open_question"] or {}).get("target"), "lines.0.item")


class DocumentMarkerByServer(Base):
    def test_marker_confirms_kind_even_if_model_unsure(self):
        self.queue = [resp(action="none", kind_source="none", hyp="unknown", lines=[line(qty=5)])]
        d = dialog.analyze_input(self.c, text="LOGISALL 입고전표\n유형: P10\n수량: 5", actor="owner")
        self.assertNotEqual((d["open_question"] or {}).get("target"), "kind")
        self.assertEqual(d["state"]["kind"]["source"], "document")

    def test_marker_vs_model_direction_conflict_asks(self):
        self.queue = [resp(action="shipment_apply", kind_source="model_inference", hyp="shipment", lines=[line(qty=5)])]
        d = dialog.analyze_input(self.c, text="LOGISALL 입고전표\n유형: P10\n수량: 5", actor="owner")
        self.assertEqual(d["open_question"]["target"], "kind")


class InterpretationGuard(Base):
    A = "P10은 KPP 팔레트의 모델명이고 이 전표는 LOGISALL이 지오블록의 하차담당자에게 전달한 거야"

    def test_item_from_sentence_without_reask_and_wrong_owner_not_recorded(self):
        """실제 API 에서 관찰: 문장 답을 모델이 '소유자=자사'로 읽음 → 기준정보(KPP 소유)와 모순 → 기록하지 않고 확인."""
        from tests.test_dev004 import J01
        self.queue = [resp(), resp(lines=[line(owner="own", osrc="user_answer")])]
        d = dialog.analyze_input(self.c, text=J01, actor="owner")
        d = self.answer(d, text=self.A)                      # 품목 질문에 문장으로 답함
        self.assertEqual(d["state"]["items"]["0"]["via"], "text_match")
        q = d["open_question"]
        self.assertEqual((q["target"], q["reason"]), ("lines.0.owner", "conflict"))
        self.assertIsNone(d["event_id"], "모순된 소유자가 장부에 들어가면 안 됨")
        d = self.answer(d, choice="KPP")
        mv = self.c.execute("SELECT owner FROM value_move WHERE event_id=?", (d["event_id"],)).fetchone()
        kpp = self.c.execute("SELECT id FROM party WHERE name='KPP'").fetchone()[0]
        self.assertEqual(mv["owner"], f"party:{kpp}")
