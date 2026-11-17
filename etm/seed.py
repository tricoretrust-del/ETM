"""가상 시험 데이터. 실제 운영 자료와 섞이지 않도록 is_test=1 로 표시하고,
기본적으로 별도 DB 파일(data/etm_demo.db)에만 넣는다.

- 거래처·현장 이름은 지시서의 실제 사례 구조를 따르되, 품목·단가·팔레트 수량·기초재고는 모두 가상값이다.
"""
from __future__ import annotations

import json

from . import process
from .process import Tx, jdump, now

DEMO_PARTIES = [
    ("금성백조", "customer", ["금성백조주택"]),
    ("나성씨앤디", "customer", []),
    ("KPP", "lessor", ["한국파렛트풀"]),
    ("한빛운수(가상)", "supplier", []),
]
DEMO_ITEMS = [  # code, name, unit, kind, aliases
    ("BLK-A", "보도블록 A형(가상)", "EA", "product", ["보도블록A", "A형블록"]),
    ("CRB-B", "경계석 B형(가상)", "EA", "product", ["경계석B"]),
    ("ILK-C", "인터로킹 C형(가상)", "m2", "product", ["인터로킹C"]),
    ("KPP-PLT", "KPP 팔레트", "개", "rental", ["팔레트", "파렛트"]),
]
DEMO_PRICES = [  # item code, party name or None, unit price, tax
    ("BLK-A", None, 1200, "excluded"),
    ("CRB-B", "금성백조", 8500, "excluded"),
    # ILK-C 단가 없음 → 금액 미확정 시나리오
]
DEMO_OPENING = {"BLK-A": 5000, "CRB-B": 800, "ILK-C": 300}


def seed_demo(conn) -> None:
    if conn.execute("SELECT COUNT(*) FROM party").fetchone()[0]:
        return
    with Tx(conn):
        pid = {}
        for name, kind, aliases in DEMO_PARTIES:
            pid[name] = conn.execute("INSERT INTO party(name, kind, aliases, is_test) VALUES (?,?,?,1)",
                                     (name, kind, jdump(aliases))).lastrowid
        conn.execute("INSERT INTO site(party_id, address, is_test) VALUES (?,?,1)",
                     (pid["금성백조"], "대전광역시 유성구 성북동 731-12"))
        iid = {}
        for code, name, unit, kind, aliases in DEMO_ITEMS:
            owner = pid["KPP"] if kind == "rental" else None
            iid[code] = conn.execute(
                "INSERT INTO item(code, name, unit, kind, owner_party_id, aliases, is_test) VALUES (?,?,?,?,?,?,1)",
                (code, name, unit, kind, owner, jdump(aliases))).lastrowid
        for code, party, up, tax in DEMO_PRICES:
            conn.execute("INSERT INTO price(item_id, party_id, unit_price, tax, is_test) VALUES (?,?,?,?,1)",
                         (iid[code], pid.get(party) if party else None, up, tax))
        for code, qty in DEMO_OPENING.items():
            conn.execute("INSERT INTO value_move(event_id, item_id, from_loc, to_loc, owner, qty, reason, created_at)"
                         " VALUES (NULL, ?, 'opening', 'factory', 'own', ?, 'opening', ?)", (iid[code], qty, now()))
    # 과거 출하 1건(가상): 담당자 후보 검색의 근거가 된다.
    process.intake(conn, PRIOR_SHIPMENT)


PRIOR_SHIPMENT = {
    "input_id": "demo-prior-20261104",
    "channel": "manual",
    "doc_type": "shipment_invoice",
    "doc_no": "20261104-02",
    "trade_date": "2026-11-04",
    "customer": "금성백조",
    "site": "대전광역시 유성구 성북동 731-12",
    "contact": {"name": "김경섭", "phone": "010-8265-3835"},
    "lines": [{"item": "보도블록 A형(가상)", "qty": 400, "unit": "EA"}],
    "pallets": {"invoice": 4, "message": 4},
}


def examples() -> dict[str, dict]:
    """화면의 '예제' 버튼과 테스트가 같이 쓰는 입력 예시."""
    return {
        # 실제 사례 구조: 송장 20261111-01 / 손글씨 차량번호·기사전화. 품목·수량·팔레트는 가상
        "scan_normal": {
            "input_id": "scan-20261111-0001",
            "channel": "scan",
            "source_ref": "photos/2026-11-11/invoice_20261111-01.jpg",
            "doc_type": "shipment_invoice",
            "doc_no": "20261111-01",
            "trade_date": "2026-11-11",
            "customer": "금성백조",
            "site": "대전광역시 유성구 성북동 731-12",
            "contact": {"name": "김경섭", "phone": "010-8265-3835"},
            "lines": [
                {"item": "보도블록A", "qty": 600, "unit": "EA"},
                {"item": "경계석 B형(가상)", "qty": 40, "unit": "EA"},
            ],
            "pallets": {"invoice": 6, "message": 6, "message_ref": "kakao/2026-11-11/상차-0812"},
            "vehicle_no": "5006",
            "driver_phone": "010-5055-5478",
            "transport": {"carrier": "한빛운수(가상)", "fee": 180000},
            "meta": {
                "vehicle_no": {"handwritten": True, "raw": "5006"},
                "driver_phone": {"handwritten": True, "raw": "010-5055-5478"},
            },
        },
        # 담당자 불일치(이주형) · 팔레트 불일치 · 단가 없는 품목 · 운반비 미확정
        "scan_issues": {
            "input_id": "scan-20261112-0003",
            "channel": "scan",
            "source_ref": "photos/2026-11-12/invoice_20261112-03.jpg",
            "doc_type": "shipment_invoice",
            "doc_no": "20261112-03",
            "trade_date": "2026-11-12",
            "customer": "(주)금성백조",
            "site": "대전광역시 유성구 성북동 731-12",
            "contact": {"name": "이주형", "phone": "010-0000-0000"},
            "lines": [
                {"item": "인터로킹C", "qty": 120, "unit": "m2"},
                {"item": "보도블럭 A", "qty": 200, "unit": "EA"},
            ],
            "pallets": {"invoice": 5, "message": 4, "message_ref": "kakao/2026-11-12/상차-0930"},
            "vehicle_no": "8123",
            "driver_phone": "010-1111-2222",
            "transport": {"carrier": "한빛운수(가상)", "fee": None},
            "meta": {
                "vehicle_no": {"handwritten": True, "raw": "8l23"},
                "driver_phone": {"handwritten": True, "raw": "010-1111-2222"},
                "lines.1.item": {"uncertain": True, "raw": "보도블럭 A"},
            },
        },
        # 직접입력: 아는 값만 먼저
        "manual_minimal": {
            "input_id": "manual-0001",
            "channel": "manual",
            "doc_type": "shipment_invoice",
            "customer": "나성씨앤디",
            "lines": [{"item": "경계석B", "qty": 30}],
        },
    }


def dump_examples(dirpath) -> None:
    from pathlib import Path
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    for name, payload in examples().items():
        (d / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
