"""학습 폐루프(DEV-004 v0.2 후속 ①): 두 규칙 생성 경로가 같은 조건 형식·같은 적용 절차를 쓰는지.
사례 → LAG → PLN → 검증 → 판정 → 승인 → 다음 자료 자동 처리. 실행: python -m unittest tests.test_loop -v"""
from __future__ import annotations

import json

from etm import db, dialog, learn, plan, process
from etm.process import Tx
from tests.test_dev004 import Base, line, resp

NO_MARK = "LOGISALL 전표 A{n}\n유형: P10\n수량: {q}"     # 원문에 종류 표지 없음 → 종류는 사람이 판단


class Loop(Base):
    def ask_until_done(self, text, kind_choice="입고"):
        self.queue.append(resp(status="ready_for_validation", action="none", kind_source="none", hyp="unknown"))
        d = dialog.analyze_input(self.c, text=text, actor="owner")
        asked = []
        while d["open_question"]:
            q = d["open_question"]
            asked.append(q["target"])
            d = self.answer(d, choice=kind_choice if q["target"] == "kind" else "KPP 팔레트")
        return d, asked

    def full_cycle(self, decision):
        """같은 판단의 사례 3건 → 후보 → LAG → PLN(실행·완료) → 검증 → 판정 → 승인. 규칙 버전을 돌려준다."""
        for n in (1, 2, 3):
            d, asked = self.ask_until_done(NO_MARK.format(n=n, q=10 * n))
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
        self.queue.append(resp(status="ready_for_validation", action="none", kind_source="none", hyp="unknown"))
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
            self.c.execute("PRAGMA user_version = 6")
        self.c.close()
        self.c = db.connect(path)
        keys = [json.loads(r[0]) for r in self.c.execute("SELECT decision_key FROM decision_case ORDER BY id DESC LIMIT 2")]
        self.assertIn({"supplier": "logisall", "item_text": "p10"}, keys)
        self.assertIn({"form_key": "수량·업체명"}, keys)


class Counterparty(Base):
    def test_empty_counterparty_asked_then_known_from_cases(self):
        self.queue.append(resp(counterparty=None))
        d = dialog.analyze_input(self.c, text=NO_MARK.format(n=1, q=1), actor="owner")
        self.assertEqual(d["open_question"]["target"], "counterparty")
        d = self.answer(d, text="LOGISALL")
        while d["open_question"]:
            d = self.answer(d, choice="입고" if d["open_question"]["target"] == "kind" else "KPP 팔레트")
        self.assertEqual(d["status"], "applied")
        # 다음 자료: 모델이 또 비워도, 원문에 적힌 이름이 확정 사례의 업체와 같으면 묻지 않는다
        self.queue.append(resp(counterparty=None))
        d2 = dialog.analyze_input(self.c, text=NO_MARK.format(n=2, q=2), actor="owner")
        self.assertNotEqual((d2["open_question"] or {}).get("target"), "counterparty")
        self.assertEqual(d2["state"]["counterparty"]["source"], "document_name")
