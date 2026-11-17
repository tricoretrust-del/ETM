"""DEV-001 §9 필수 검증. 실행: python -m unittest -v"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from etm import db, process, seed, server


def stock(conn, code):
    item_id = conn.execute("SELECT id FROM item WHERE code=?", (code,)).fetchone()[0]
    return process.factory_stock(conn, item_id)


def claim(conn, event_id, kind="receivable"):
    return conn.execute("SELECT * FROM money_claim WHERE event_id=? AND kind=?", (event_id, kind)).fetchone()


def obligations(conn, event_id):
    return {r["kind"]: (r["state"], r["note"]) for r in conn.execute(
        "SELECT * FROM obligation WHERE event_id=?", (event_id,))}


def answer_all_ok(conn, res):
    return process.answer(conn, res["event_id"], {str(c["id"]): {"choice": "ok"} for c in res["checks"]})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.path)
        seed.seed_demo(self.conn)
        self.ex = seed.examples()
        # 기초 5000 - 과거 출하 400
        self.assertEqual(stock(self.conn, "BLK-A"), 4600)

    def tearDown(self):
        process.FAULT_HOOK = None
        self.conn.close()
        self.tmp.cleanup()


class TestDev001(Base):
    # 1) 정상 확정 후 재고·채권·히스토리 일치
    def test_01_normal_confirm(self):
        r = process.intake(self.conn, self.ex["scan_normal"])
        self.assertEqual(r["outcome"], "new")
        answer_all_ok(self.conn, r)
        ev = r["event_id"]
        self.assertEqual(stock(self.conn, "BLK-A"), 4000)
        self.assertEqual(stock(self.conn, "CRB-B"), 760)
        self.assertEqual(claim(self.conn, ev)["amount"], 600 * 1200 + 40 * 8500)
        self.assertEqual(claim(self.conn, ev, "payable")["amount"], 180000)
        ob = obligations(self.conn, ev)
        self.assertEqual(ob["stock_out"][0], "done")
        self.assertEqual(ob["pallet_check"][0], "done")
        self.assertEqual(ob["collect"][0], "open")          # 재고 반영 완료 ≠ 거래 전체 완료
        self.assertEqual(ob["pallet_return"][0], "open")
        hist = [i for d in process.history(self.conn) for i in d["items"] if i["event_id"] == ev]
        self.assertTrue(any("1,060,000원" in h["summary"] for h in hist))
        s = process.summary(self.conn)
        self.assertEqual(next(x["qty"] for x in s["stock"] if x["name"].startswith("보도블록")), 4000)

    # 2) 재수신 시 이중 반영 없음
    def test_02_re_receive_no_double(self):
        r = process.intake(self.conn, self.ex["scan_normal"])
        answer_all_ok(self.conn, r)
        again = copy.deepcopy(self.ex["scan_normal"])
        again["input_id"] = "kakao-forward-77"
        again["source_ref"] = "kakao/forwarded.jpg"
        r2 = process.intake(self.conn, again)
        self.assertEqual(r2["outcome"], "re_receive")
        self.assertEqual(r2["event_id"], r["event_id"])
        self.assertEqual(stock(self.conn, "BLK-A"), 4000)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM money_claim WHERE state='active'").fetchone()[0], 3)  # 과거 출하 받을 돈 + 이번 받을 돈 + 운반비
        # 같은 전표번호·다른 내용 → 수정 후보. '같은 사건' 선택 시 차이만 반영
        changed = copy.deepcopy(self.ex["scan_normal"])
        changed["input_id"] = "scan-rescan"
        changed["lines"][0]["qty"] = 650
        r3 = process.intake(self.conn, changed)
        self.assertEqual(r3["outcome"], "revision_candidate")
        self.assertEqual(stock(self.conn, "BLK-A"), 4000)  # 확인 전에는 반영 없음
        ident = next(c for c in r3["checks"] if c["kind"] == "identity")
        self.assertIn("수량", ident["message"])
        process.answer(self.conn, r3["event_id"], {str(ident["id"]): {"choice": "same"}})
        self.assertEqual(stock(self.conn, "BLK-A"), 3950)
        self.assertEqual(claim(self.conn, r["event_id"])["amount"], 650 * 1200 + 40 * 8500)
        self.assertEqual(self.conn.execute("SELECT status FROM event WHERE id=?", (r3["event_id"],)).fetchone()[0], "merged")

    def test_02b_business_duplicate(self):
        r = process.intake(self.conn, self.ex["scan_normal"])
        dup = copy.deepcopy(self.ex["scan_normal"])
        dup.update(input_id="photo-2", doc_no="20261111-01B")
        r2 = process.intake(self.conn, dup)
        self.assertEqual(r2["outcome"], "duplicate_candidate")
        ident = next(c for c in r2["checks"] if c["kind"] == "identity")
        process.answer(self.conn, r2["event_id"], {str(ident["id"]): {"choice": "same"}})
        self.assertEqual(stock(self.conn, "BLK-A"), 4000)
        # '별개 출하' 선택 시에는 정상 반영
        dup2 = copy.deepcopy(dup)
        dup2.update(input_id="photo-3", doc_no="20261111-01C")
        r3 = process.intake(self.conn, dup2)
        ident = next(c for c in r3["checks"] if c["kind"] == "identity")
        process.answer(self.conn, r3["event_id"], {str(ident["id"]): {"choice": "separate"}})
        self.assertEqual(stock(self.conn, "BLK-A"), 3400)

    # 3) 품목·담당자 불일치 시 후보 제안 (과거값으로 덮어쓰지 않음)
    def test_03_candidates(self):
        r = process.intake(self.conn, self.ex["scan_issues"])
        kinds = {c["kind"]: c for c in r["checks"]}
        self.assertIn("contact", kinds)
        self.assertEqual(kinds["contact"]["detail"]["candidates"][0]["name"], "김경섭")
        _, f = process.load_event(self.conn, r["event_id"])
        self.assertEqual(f["contact.name"]["value"], "이주형")  # 자동 덮어쓰기 없음
        item_chk = next(c for c in r["checks"] if c["kind"] == "match" and c["path"] == "lines.1.item")
        self.assertEqual(item_chk["detail"]["candidates"][0]["label"].split(" (")[0], "보도블록 A형(가상)")
        # 판독 불확실 + 후보 선택이 같은 필드에 겹치면 한 번만 묻는다
        self.assertFalse(any(c["kind"] == "verify" and c["path"] == "lines.1.item" for c in r["checks"]))
        # 새 담당자를 그대로 유지(신규값 허용)
        process.answer(self.conn, r["event_id"], {str(kinds["contact"]["id"]): {"choice": "keep"}})
        _, f = process.load_event(self.conn, r["event_id"])
        self.assertEqual(f["contact.name"]["value"], "이주형")
        self.assertEqual(f["contact.name"]["source"], "user")

    # 4) 손글씨 확인 요구 — 원본과 추출값을 보여준다
    def test_04_handwriting(self):
        r = process.intake(self.conn, self.ex["scan_issues"])
        v = next(c for c in r["checks"] if c["path"] == "vehicle_no")
        self.assertIsNone(v["rule_id"])                          # 승인된 규칙이 아니라 기본 동작
        self.assertIn("손글씨", v["detail"]["why"])
        self.assertEqual(v["detail"]["raw"], "8l23")
        self.assertEqual(v["detail"]["extracted"], "8123")
        process.answer(self.conn, r["event_id"], {str(v["id"]): {"value": "8123"}})
        _, f = process.load_event(self.conn, r["event_id"])
        self.assertEqual(f["vehicle_no"]["value"], "8123")
        self.assertIsInstance(f["vehicle_no"]["value"], str)

    # 5) 팔레트: 송장·메시지 불일치 / 자료 누락 → 팔레트 확인 미결, 제품 출하는 진행
    def test_05_pallets(self):
        r = process.intake(self.conn, self.ex["scan_issues"])
        ev = r["event_id"]
        self.assertTrue(any(c["kind"] == "pallet_mismatch" for c in r["checks"]))
        self.assertEqual(obligations(self.conn, ev)["pallet_check"][0], "open")
        self.assertEqual(stock(self.conn, "ILK-C"), 180)  # 확정된 제품 줄은 반영됨
        moved = self.conn.execute("SELECT COUNT(*) FROM value_move WHERE event_id=? AND owner LIKE 'party:%'",
                                  (ev,)).fetchone()[0]
        self.assertEqual(moved, 0)
        chk = next(c for c in r["checks"] if c["kind"] == "pallet_mismatch")
        process.answer(self.conn, ev, {str(chk["id"]): {"choice": "message"}})
        self.assertEqual(obligations(self.conn, ev)["pallet_check"][0], "done")
        q = self.conn.execute("SELECT SUM(qty) FROM value_move WHERE event_id=? AND owner LIKE 'party:%'", (ev,)).fetchone()[0]
        self.assertEqual(q, 4)
        # 자료 누락
        p = copy.deepcopy(self.ex["scan_normal"])
        p.update(input_id="m-1", doc_no="X-1", trade_date="2026-11-13")
        p["pallets"] = {"invoice": 3, "message": None}
        r2 = process.intake(self.conn, p)
        self.assertTrue(any(c["kind"] == "pallet_missing" for c in r2["checks"]))
        self.assertEqual(obligations(self.conn, r2["event_id"])["pallet_check"][0], "open")
        # KPP 임대료 채무를 자동으로 만들지 않는다
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM money_claim WHERE party_name LIKE '%KPP%'").fetchone()[0], 0)

    # 6) 단가 미확정 → 금액 NULL(0 아님)로 저장, 미결로 조회
    def test_06_price_undetermined(self):
        r = process.intake(self.conn, self.ex["manual_minimal"])  # 나성씨앤디 경계석: 단가 없음
        c = claim(self.conn, r["event_id"])
        self.assertIsNotNone(c)
        self.assertIsNone(c["amount"])
        self.assertEqual(obligations(self.conn, r["event_id"])["amount_confirm"], ("open", "단가 미확정"))
        s = process.summary(self.conn)
        self.assertGreaterEqual(s["receivable"]["undetermined_count"], 1)
        self.assertEqual(stock(self.conn, "CRB-B"), 770)

    # 7) 일부 필드 보완 후 미반영 부분만 반영
    def test_07_partial_then_complete(self):
        r = process.intake(self.conn, self.ex["scan_issues"])
        ev = r["event_id"]
        self.assertEqual(stock(self.conn, "BLK-A"), 4600)   # 2번 줄 보류
        self.assertEqual(stock(self.conn, "ILK-C"), 180)    # 1번 줄 반영
        n_moves = self.conn.execute("SELECT COUNT(*) FROM value_move WHERE event_id=?", (ev,)).fetchone()[0]
        item_chk = next(c for c in r["checks"] if c["kind"] == "match")
        process.answer(self.conn, ev, {str(item_chk["id"]): {"choice": "item:1"}})
        self.assertEqual(stock(self.conn, "BLK-A"), 4400)
        self.assertEqual(stock(self.conn, "ILK-C"), 180)    # 이미 반영된 줄은 다시 차감하지 않음
        n2 = self.conn.execute("SELECT COUNT(*) FROM value_move WHERE event_id=?", (ev,)).fetchone()[0]
        self.assertEqual(n2, n_moves + 1)
        # 인터로킹 단가 없음 → 금액은 여전히 미확정
        self.assertIsNone(claim(self.conn, ev)["amount"])

    # 8) 수량 수정 후 재고·채권 정확성 (차액만 반영, 이력 보존)
    def test_08_revise_quantity(self):
        r = process.intake(self.conn, self.ex["scan_normal"])
        answer_all_ok(self.conn, r)
        ev = r["event_id"]
        process.revise(self.conn, ev, {"lines.0.qty": 550})
        self.assertEqual(stock(self.conn, "BLK-A"), 4050)
        self.assertEqual(claim(self.conn, ev)["amount"], 550 * 1200 + 40 * 8500)
        process.revise(self.conn, ev, {"lines.0.qty": 550})  # 같은 값 재수정: 변화 없음
        self.assertEqual(stock(self.conn, "BLK-A"), 4050)
        rows = self.conn.execute("SELECT qty, reason FROM value_move WHERE event_id=? AND item_id=1 ORDER BY id",
                                 (ev,)).fetchall()
        self.assertEqual([(x["qty"], x["reason"]) for x in rows], [(600, "apply"), (-50, "revise")])
        logs = self.conn.execute("SELECT old_amount, new_amount FROM money_log ml JOIN money_claim c ON c.id=ml.claim_id"
                                 " WHERE c.event_id=? AND c.kind='receivable' ORDER BY ml.id", (ev,)).fetchall()
        self.assertEqual([tuple(x) for x in logs], [(None, 1060000), (1060000, 1000000)])
        # 품목 자체 변경: 이전 품목 재고 복원
        process.revise(self.conn, ev, {"lines.1.item": "보도블록 A형(가상)"})
        self.assertEqual(stock(self.conn, "CRB-B"), 800)
        self.assertEqual(stock(self.conn, "BLK-A"), 4010)

    # 9) 저장 도중 실패 → 전부 롤백
    def test_09_failure_atomic(self):
        before = (stock(self.conn, "BLK-A"),
                  self.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0],
                  self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])

        def boom():
            raise RuntimeError("디스크 오류 흉내")
        process.FAULT_HOOK = boom
        with self.assertRaises(RuntimeError):
            process.intake(self.conn, self.ex["scan_normal"])
        process.FAULT_HOOK = None
        after = (stock(self.conn, "BLK-A"),
                 self.conn.execute("SELECT COUNT(*) FROM event").fetchone()[0],
                 self.conn.execute("SELECT COUNT(*) FROM history").fetchone()[0])
        self.assertEqual(before, after)
        # 다시 시도하면 정상 반영(실패 흔적 때문에 재수신으로 오인하지 않음)
        r = process.intake(self.conn, self.ex["scan_normal"])
        self.assertEqual(r["outcome"], "new")
        self.assertEqual(stock(self.conn, "BLK-A"), 4000)
        # 수정 도중 실패도 롤백
        process.FAULT_HOOK = boom
        with self.assertRaises(RuntimeError):
            process.revise(self.conn, r["event_id"], {"lines.0.qty": 100})
        process.FAULT_HOOK = None
        self.assertEqual(stock(self.conn, "BLK-A"), 4000)
        _, f = process.load_event(self.conn, r["event_id"])
        self.assertEqual(f["lines.0.qty"]["value"], 600)

    # 10) 재접속 후 이력 유지
    def test_10_persistence(self):
        r = process.intake(self.conn, self.ex["scan_normal"])
        answer_all_ok(self.conn, r)
        self.conn.close()
        conn2 = db.connect(self.path)
        try:
            days = process.history(conn2)
            self.assertTrue(any(i["event_id"] == r["event_id"] for d in days for i in d["items"]))
            self.assertEqual(stock(conn2, "BLK-A"), 4000)
            d = process.event_detail(conn2, r["event_id"])
            self.assertEqual(d["doc_no"], "20261111-01")
            self.assertEqual(d["trade_date"], "2026-11-11")
        finally:
            self.conn = conn2

    def test_11_manual_ok_without_value_is_rejected(self):
        p = copy.deepcopy(self.ex["scan_normal"])
        p.update(input_id="x", doc_no="Y-1", vehicle_no=None)
        r = process.intake(self.conn, p)
        v = next(c for c in r["checks"] if c["path"] == "vehicle_no")
        with self.assertRaises(ValueError):  # 승인 대상 값이 없으면 '맞음'으로 복원하지 않는다
            process.answer(self.conn, r["event_id"], {str(v["id"]): {"choice": "ok"}})

    def test_12_contract_strings_and_errors(self):
        from etm import contract
        h, f = contract.normalize({"input_id": "a", "channel": "scan", "doc_no": 20261111, "driver_phone": 1050555478})
        self.assertEqual(f["doc_no"]["value"], "20261111")
        self.assertIn("uncertain", f["driver_phone"]["flags"])
        with self.assertRaises(contract.ContractError):
            contract.normalize({"input_id": "a", "channel": "fax", "lines": [{"qty": -1}]})


class TestHttp(Base):
    def test_api_roundtrip(self):
        app = server.App(self.path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(path, body=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        try:
            r = call("/api/intake", self.ex["scan_normal"])
            r = call(f"/api/events/{r['event_id']}/answers",
                     {"answers": {str(c["id"]): {"choice": "ok"} for c in r["checks"]}})
            self.assertEqual(r["checks"], [])
            self.assertTrue(call("/api/history")["days"])
            self.assertEqual(call("/api/summary")["receivable"]["known"], 480000 + 1060000)
            with urllib.request.urlopen(base + "/") as resp:
                self.assertIn(b"ETM", resp.read())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
