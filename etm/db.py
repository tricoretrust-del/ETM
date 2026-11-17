"""SQLite 저장소와 스키마(마이그레이션).

설계 메모
- 한 파일 DB(SQLite). 표준 라이브러리만 사용한다.
- 논리 구성(기준정보 / 사건 / 가치 이동 / 돈 / 남은 처리 / 이력 / 룰북 / 커리큘럼)을
  최소 테이블로 묶었다. 표시용 값은 저장하지 않고 조회 시 계산한다.
- 시험 데이터는 is_test=1 로 표시하고, 기본적으로 별도 DB 파일에 둔다.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = os.environ.get("ETM_DB", str(ROOT / "data" / "etm.db"))

MIGRATIONS: list[str] = [
    # ---- 0001: 초기 구조 -------------------------------------------------
    """
    -- 기준정보 ---------------------------------------------------------
    CREATE TABLE party (            -- 거래 상대(거래처·공급업체·임대업체)
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,          -- customer | supplier | lessor
        aliases TEXT NOT NULL DEFAULT '[]',
        is_test INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE site (             -- 현장(거래처 소속)
        id INTEGER PRIMARY KEY,
        party_id INTEGER NOT NULL REFERENCES party(id),
        address TEXT NOT NULL,
        is_test INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE item (             -- 품목. kind=product(자사 재고) | rental(임대품, 예: KPP 팔레트)
        id INTEGER PRIMARY KEY,
        code TEXT,
        name TEXT NOT NULL,
        unit TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'product',
        owner_party_id INTEGER REFERENCES party(id),   -- 임대품 소유자
        aliases TEXT NOT NULL DEFAULT '[]',
        is_test INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE price (            -- 단가. party_id NULL = 기본 단가
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES item(id),
        party_id INTEGER REFERENCES party(id),
        unit_price INTEGER NOT NULL,
        tax TEXT NOT NULL DEFAULT 'unknown',          -- 부가세 조건: excluded | included | unknown
        is_test INTEGER NOT NULL DEFAULT 0
    );

    -- 입력 원본 --------------------------------------------------------
    CREATE TABLE intake (           -- 받은 자료 한 건(스캔·직접입력). 재수신도 각각 남긴다
        id INTEGER PRIMARY KEY,
        input_id TEXT NOT NULL,
        channel TEXT NOT NULL,       -- scan | manual
        source_ref TEXT,
        received_at TEXT NOT NULL,
        payload TEXT NOT NULL,       -- 공통 형식으로 받은 원문 JSON
        payload_hash TEXT NOT NULL,
        event_id INTEGER REFERENCES event(id),
        outcome TEXT NOT NULL        -- new | re_receive | revision_candidate | duplicate_candidate
    );

    -- 사건 -------------------------------------------------------------
    CREATE TABLE event (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,          -- shipment
        doc_type TEXT NOT NULL,
        doc_no TEXT,
        party_id INTEGER REFERENCES party(id),
        site_id INTEGER REFERENCES site(id),
        trade_date TEXT,             -- 실제 거래일
        created_at TEXT NOT NULL,    -- 처리(기록)일
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL,        -- active | merged | void
        merged_into INTEGER REFERENCES event(id),
        fields TEXT NOT NULL,        -- 경로별 {value, orig, raw, flags, source}
        is_test INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX event_doc ON event(doc_type, doc_no);

    CREATE TABLE check_item (       -- 사용자 확인 요청
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES event(id),
        path TEXT NOT NULL,
        kind TEXT NOT NULL,
        signature TEXT NOT NULL,
        rule_id TEXT,
        message TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}',   -- 원본값·추출값·후보·선택지
        state TEXT NOT NULL,                 -- open | answered | void
        answer TEXT,
        created_at TEXT NOT NULL,
        answered_at TEXT
    );
    CREATE INDEX check_event ON check_item(event_id, state);

    -- 가치 이동 --------------------------------------------------------
    -- qty 는 부호 있는 조정량. 취소·수정은 반대 부호 행을 추가한다(이력 보존).
    CREATE TABLE value_move (
        id INTEGER PRIMARY KEY,
        event_id INTEGER REFERENCES event(id),   -- NULL = 기초재고 등
        item_id INTEGER NOT NULL REFERENCES item(id),
        from_loc TEXT NOT NULL,
        to_loc TEXT NOT NULL,
        owner TEXT NOT NULL,         -- own | party:<id>
        qty REAL NOT NULL,
        reason TEXT NOT NULL,        -- apply | revise | opening
        created_at TEXT NOT NULL
    );
    CREATE INDEX move_event ON value_move(event_id);

    -- 돈의 이동 --------------------------------------------------------
    -- 채권·채무의 현재 상태. amount NULL = 금액 미확정(0과 다름)
    CREATE TABLE money_claim (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES event(id),
        kind TEXT NOT NULL,          -- receivable | payable
        role TEXT NOT NULL,          -- goods | freight
        party_id INTEGER REFERENCES party(id),
        party_name TEXT,
        amount INTEGER,
        tax TEXT NOT NULL DEFAULT 'unknown',
        state TEXT NOT NULL DEFAULT 'active',    -- active | void(수정으로 근거가 사라짐)
        settled INTEGER NOT NULL DEFAULT 0,      -- 수금·지급 누계(후속 기능)
        basis TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, kind, role)
    );
    CREATE TABLE money_log (        -- 금액 변경 이력
        id INTEGER PRIMARY KEY,
        claim_id INTEGER NOT NULL REFERENCES money_claim(id),
        old_amount INTEGER,
        new_amount INTEGER,
        reason TEXT NOT NULL,
        at TEXT NOT NULL
    );

    -- 미결·완료 --------------------------------------------------------
    CREATE TABLE obligation (
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES event(id),
        kind TEXT NOT NULL,          -- stock_out | pallet_check | amount_confirm | collect | freight_info | freight_pay | pallet_return | review
        state TEXT NOT NULL,         -- open | done
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, kind)
    );

    -- 처리 이력 --------------------------------------------------------
    CREATE TABLE history (
        id INTEGER PRIMARY KEY,
        event_id INTEGER REFERENCES event(id),
        at TEXT NOT NULL,
        action TEXT NOT NULL,        -- intake | re_receive | confirm | apply | revise | merge
        summary TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX history_at ON history(at);

    -- 확정 사례 / 운영 룰북 / 운영 커리큘럼 ----------------------------
    CREATE TABLE confirmed_case (   -- 사용자가 확정한 개별 사례. 규칙으로 자동 승격하지 않는다
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES event(id),
        check_id INTEGER REFERENCES check_item(id),
        path TEXT NOT NULL,
        before_value TEXT,
        after_value TEXT,
        at TEXT NOT NULL
    );
    CREATE TABLE rulebook (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,         -- 적용 범위(예: company:default)
        condition TEXT NOT NULL,
        action TEXT NOT NULL,
        basis TEXT NOT NULL,
        version TEXT NOT NULL,
        status TEXT NOT NULL         -- active | draft | retired
    );
    CREATE TABLE curriculum (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '',
        next_check TEXT NOT NULL DEFAULT '',
        done_criteria TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'open'
    );
    """,
    # ---- 0002: 학습 구조 (판단 위임 → 확정 사례 → 규칙 제안 → 승인된 규칙) ----
    """
    ALTER TABLE rulebook ADD COLUMN decision_kind TEXT;   -- 학습 규칙: 어떤 판단인가 (doc_kind, item_map …)
    ALTER TABLE rulebook ADD COLUMN decision_key TEXT;    -- 어떤 상황에서 (양식 키, 업체|품목 …)
    ALTER TABLE rulebook ADD COLUMN answer TEXT;          -- 무엇으로 정하는가 (JSON)
    ALTER TABLE rulebook ADD COLUMN case_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE rulebook ADD COLUMN created_at TEXT;
    ALTER TABLE curriculum ADD COLUMN decision_kind TEXT;
    ALTER TABLE curriculum ADD COLUMN decision_key TEXT;
    ALTER TABLE curriculum ADD COLUMN proposed_answer TEXT;
    ALTER TABLE curriculum ADD COLUMN case_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE curriculum ADD COLUMN rule_id TEXT;
    ALTER TABLE curriculum ADD COLUMN updated_at TEXT;
    -- 사용자에게 위임한 판단 하나하나(또는 규칙이 대신 내린 판단)
    CREATE TABLE decision_case (
        id INTEGER PRIMARY KEY,
        event_id INTEGER REFERENCES event(id),
        check_id INTEGER REFERENCES check_item(id),
        decision_kind TEXT NOT NULL,
        decision_key TEXT NOT NULL,
        label TEXT NOT NULL,          -- 사람이 읽는 상황 설명
        answer TEXT NOT NULL,         -- JSON
        rule_id TEXT,                 -- NULL = 사용자가 답함, 값 = 규칙이 처리함
        at TEXT NOT NULL
    );
    CREATE INDEX decision_case_key ON decision_case(decision_kind, decision_key);
    """,
    # ---- 0003: DEV-002 운영 RBK·LAG (규칙 버전·판정·검증·적용 기록) ----------------
    """
    CREATE TABLE company (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE app_user (                 -- 확인·판정·승인 주체
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL,                  -- operator(확인·LAG) | approver(회사 규칙 승인) | admin(공통 규칙 승인)
        company_id INTEGER REFERENCES company(id)
    );
    ALTER TABLE event ADD COLUMN company_id INTEGER NOT NULL DEFAULT 1 REFERENCES company(id);
    ALTER TABLE event ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
    -- 확인·수정 기록 보강: 확인자·이유·원본 참조
    ALTER TABLE confirmed_case ADD COLUMN actor TEXT;
    ALTER TABLE confirmed_case ADD COLUMN reason TEXT;
    ALTER TABLE confirmed_case ADD COLUMN source_ref TEXT;
    ALTER TABLE decision_case ADD COLUMN actor TEXT;
    ALTER TABLE decision_case ADD COLUMN company_id INTEGER NOT NULL DEFAULT 1 REFERENCES company(id);
    ALTER TABLE decision_case ADD COLUMN rule_version_id INTEGER REFERENCES rule_version(id);
    -- 이전 구조(rulebook·curriculum)는 지우지 않고 이관 위치만 기록한다
    ALTER TABLE rulebook ADD COLUMN moved_to TEXT;
    ALTER TABLE curriculum ADD COLUMN moved_to TEXT;

    -- LAG: 운영 중 발견한 문제·지식 공백·검증 과제 ------------------------------
    CREATE TABLE lag (
        id INTEGER PRIMARY KEY,
        company_id INTEGER NOT NULL REFERENCES company(id),
        question TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '{}',  -- 적용 맥락(JSON): decision, key, event_kind, scope
        status TEXT NOT NULL,                -- open(접수) | verifying(검증 중) | awaiting(판정 대기) | closed(종료) | held(보류)
        outcome TEXT,                        -- rule(규칙 반영) | rejected(기각) | narrowed(범위 축소) — closed 일 때
        status_reason TEXT,
        origin TEXT,                         -- 이관 출처 등
        created_by TEXT NOT NULL REFERENCES app_user(id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE lag_link (                 -- LAG ↔ 사건·확인 기록 (복수)
        id INTEGER PRIMARY KEY,
        lag_id INTEGER NOT NULL REFERENCES lag(id),
        event_id INTEGER REFERENCES event(id),
        decision_case_id INTEGER REFERENCES decision_case(id),
        confirmed_case_id INTEGER REFERENCES confirmed_case(id),
        CHECK (event_id IS NOT NULL OR decision_case_id IS NOT NULL OR confirmed_case_id IS NOT NULL)
    );
    CREATE TABLE lag_log (                  -- 상태 이력(보류·재개·기각 포함)
        id INTEGER PRIMARY KEY,
        lag_id INTEGER NOT NULL REFERENCES lag(id),
        from_status TEXT, to_status TEXT NOT NULL,
        note TEXT, actor TEXT NOT NULL REFERENCES app_user(id), at TEXT NOT NULL
    );
    CREATE TABLE verification (
        id INTEGER PRIMARY KEY,
        lag_id INTEGER NOT NULL REFERENCES lag(id),
        method TEXT NOT NULL,
        expected TEXT NOT NULL,
        observed TEXT NOT NULL,
        limits TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL REFERENCES app_user(id),
        at TEXT NOT NULL
    );
    CREATE TABLE verification_case (
        verification_id INTEGER NOT NULL REFERENCES verification(id),
        decision_case_id INTEGER NOT NULL REFERENCES decision_case(id),
        PRIMARY KEY (verification_id, decision_case_id)
    );

    -- RBK: 규칙(고정 ID)과 변경 불가능한 버전 --------------------------------
    CREATE TABLE rule (
        id TEXT PRIMARY KEY,                 -- 안정적인 규칙 ID
        company_id INTEGER REFERENCES company(id),   -- NULL = 공통 규칙(admin 승인 필요)
        event_kind TEXT NOT NULL,            -- shipment | receipt | intake
        decision TEXT NOT NULL,              -- pallet_check | doc_kind | item_map
        title TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE rule_version (
        id INTEGER PRIMARY KEY,              -- 변경 불가능한 버전 ID
        rule_id TEXT NOT NULL REFERENCES rule(id),
        version_no INTEGER NOT NULL,
        status TEXT NOT NULL,                -- draft | pending | active | suspended | superseded | retired
        description TEXT NOT NULL,
        condition TEXT NOT NULL,             -- JSON: 사실 일치 조건
        action TEXT NOT NULL,                -- JSON: 허용 목록의 동작만
        scope TEXT NOT NULL DEFAULT '{}',    -- JSON: party_id / site_id / item_id
        valid_from TEXT, valid_to TEXT,
        basis TEXT NOT NULL,                 -- 근거 설명
        lag_id INTEGER REFERENCES lag(id),
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (rule_id, version_no)
    );
    -- 원자성 보조: 한 규칙에 활성 버전은 최대 1개
    CREATE UNIQUE INDEX rule_one_active ON rule_version(rule_id) WHERE status = 'active';
    -- 내용 불변: 조건·동작·범위·근거는 수정 불가 (상태만 바뀐다)
    CREATE TRIGGER rule_version_immutable BEFORE UPDATE OF condition, action, scope, basis, rule_id, version_no, description
        ON rule_version BEGIN SELECT RAISE(ABORT, 'rule_version 내용은 수정할 수 없습니다. 새 버전을 만드세요'); END;

    CREATE TABLE judgment (                 -- 사용자 판정 (LAG 또는 정확한 규칙 버전 대상)
        id INTEGER PRIMARY KEY,
        lag_id INTEGER REFERENCES lag(id),
        rule_version_id INTEGER REFERENCES rule_version(id),
        kind TEXT NOT NULL,                  -- rule | rejected | narrowed | hold | reopen | approve | suspend | retire
        reason TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL REFERENCES app_user(id),
        role TEXT NOT NULL,
        at TEXT NOT NULL,
        CHECK ((lag_id IS NULL) <> (rule_version_id IS NULL))
    );
    CREATE TABLE rule_application (         -- 후속 사건에서 규칙을 쓴 기록
        id INTEGER PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES event(id),
        event_revision INTEGER NOT NULL,
        rule_version_id INTEGER NOT NULL REFERENCES rule_version(id),
        target TEXT NOT NULL,                -- 적용 대상(예: pallets.confirmed)
        match_basis TEXT NOT NULL,           -- JSON: 어떤 사실이 조건·범위에 맞았나
        input_snapshot TEXT NOT NULL,        -- JSON
        result TEXT NOT NULL,                -- performed | required_confirm | held_conflict
        reason TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL
    );
    CREATE INDEX rule_application_event ON rule_application(event_id);
    CREATE TABLE command_log (              -- 학습 명령 멱등성
        request_id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        result TEXT NOT NULL,
        at TEXT NOT NULL
    );
    """,
]

# 이번 구현에서 명시적으로 적용하는 고정 규칙(운영 룰북 초기값)
FIXED_RULES = [
    ("HW-CONFIRM", "company:default", "차량번호·기사전화가 손글씨로 표시됨",
     "원본과 추출값을 보여주고 사용자 확인을 받는다", "RBK v0.3 / DEV-001 §5"),
    ("PALLET-DOUBLE", "company:default", "출하 송장",
     "송장 팔레트 수량과 상차자 메시지 수량을 대조한다. 없거나 다르면 팔레트 확인 미결",
     "RBK v0.3 / DEV-001 §5"),
    ("NO-OVERWRITE", "company:default", "담당자·품목이 기존 기록과 다름",
     "기존 후보를 제안하고 확인받는다. 과거값으로 자동 덮어쓰지 않는다", "RBK v0.3"),
    ("UNCERTAIN-FIELD", "company:default", "스캔 결과에 불확실·누락 표시",
     "해당 필드만 확인 요청. 그 값에 의존하는 처리만 보류", "RBK v0.3 / DEV-001 §4"),
    ("KPP-NO-AUTO", "company:default", "KPP 팔레트 이동",
     "위치 이동만 기록. 임대료 채무를 자동 생성하지 않는다", "DEV-001 §6"),
    ("STOCK-WARN", "company:default", "재고 부족·단위 충돌",
     "경고하고 확인을 받는다. 자동 차단하지 않는다", "DEV-001 §5"),
]

INITIAL_CURRICULUM = [
    ("미결·완결 세부 상태와 완료 조건 검증", "DEV-001 §6", "실제 출하 사례로 obligation 종류 검증",
     "TRNK 확인"),
    ("처리 건수 집계 규칙", "CUR v0.3 §4", "한 입력이 여러 효과를 만들 때 이력 건수 정의", "TRNK 결정"),
    ("확정 사례 → 운영 규칙 승격 기준", "RBK R13", "반복 확정 사례 수집 후 검토", "TRNK 결정"),
    ("스캔 출력 어댑터 연결", "DEV-001 §3", "실제 스캔 출력 샘플 확보", "샘플 3건 이상 변환 성공"),
]


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL" if path != ":memory:" else "PRAGMA journal_mode = MEMORY")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN")
        for stmt in _split(sql):
            conn.execute(stmt)
        if i == 1:
            conn.executemany(
                "INSERT INTO rulebook(id, scope, condition, action, basis, version, status)"
                " VALUES (?,?,?,?,?,'v0.1','active')", FIXED_RULES)
            conn.executemany(
                "INSERT INTO curriculum(title, evidence, next_check, done_criteria) VALUES (?,?,?,?)",
                INITIAL_CURRICULUM)
        if i == 3:
            _migrate_0003(conn)
        if i == 2:
            conn.execute(
                "INSERT INTO rulebook(id, scope, condition, action, basis, version, status)"
                " VALUES ('MASTER-EXACT','company:default','입력값이 기준정보(거래처·현장·품목)의 이름·코드·별칭과 정확히 같음',"
                "'그 기준정보로 연결한다(묻지 않음)','v0.1 구현 기본값 — TRNK 검토 필요','v0.1','active')")
        conn.execute(f"PRAGMA user_version = {i}")
        conn.execute("COMMIT")


def _migrate_0003(conn) -> None:
    from datetime import datetime
    t = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO company(id, name) VALUES (1, '기본 회사')")
    conn.executemany("INSERT INTO app_user(id, name, role, company_id) VALUES (?,?,?,1)", [
        ("owner", "대표(승인권자)", "approver"), ("staff", "직원", "operator"),
        ("system", "시스템(이관·초기 제안 기록용, 판정 권한 없음)", "system")])
    # 이전 고정 규칙: 개발자가 넣은 '활성' 기록이었으므로 승인 기록으로 옮기지 않는다.
    moved = {
        "PALLET-DOUBLE": "rule:R-PALLET-CHECK v1 (승인 대기 초안)",
        "HW-CONFIRM": "code-default(손글씨는 항상 확인)",
        "UNCERTAIN-FIELD": "code-default(불확실 표시는 항상 확인)",
        "NO-OVERWRITE": "code-default(과거값 자동 덮어쓰기 없음)",
        "STOCK-WARN": "code-default(경고 후 확인, 차단 없음)",
        "KPP-NO-AUTO": "code-default(임대료 채무 자동 생성 없음)",
        "MASTER-EXACT": "code-default(기준정보 정확 일치 연결)",
    }
    for rid, to in moved.items():
        conn.execute("UPDATE rulebook SET moved_to=?, status='retired' WHERE id=?", (to, rid))
    conn.execute("UPDATE rulebook SET moved_to='재승인 필요: 이전 방식 승인은 권한·버전 기록이 없음', status='retired'"
                 " WHERE id LIKE 'L-%'")
    # 팔레트 대조 규칙: 승인 대기 초안으로만 등록(근거 제시). 사용자가 앱에서 범위·동작을 확인하고 승인한다.
    conn.execute("INSERT INTO rule(id, company_id, event_kind, decision, title, created_by, created_at)"
                 " VALUES ('R-PALLET-CHECK', 1, 'shipment', 'pallet_check', '출하 팔레트 송장·상차 메시지 대조', 'system', ?)", (t,))
    conn.execute(
        "INSERT INTO rule_version(rule_id, version_no, status, description, condition, action, scope, basis, created_by, created_at)"
        " VALUES ('R-PALLET-CHECK', 1, 'pending', ?, '{}', ?, '{}', ?, 'system', ?)",
        ("회사: 기본 회사 / 사건: 출하 전체. 송장과 상차 메시지의 팔레트 수가 같고 판독 문제가 없으면 그 수를 확정, "
         "다르면 사용자 확인 필수, 자료가 없으면 팔레트 확인 미결",
         '{"type": "compare_pallets", "on_match": "accept", "on_mismatch": "require_confirm"}',
         "DEV-002 §4 지시와 RBK v0.3 사용자 결정(출하 팔레트는 매번 송장·상차 메시지로 더블체크). "
         "개발 초기 제안이며 승인 기록 아님", t))
    # 이전 커리큘럼 항목 분류: 개발 계획은 PLN(문서), 검증 질문은 LAG 로 옮긴다(원래 ID·상태 보존)
    plan_like = ("처리 건수 집계 규칙", "스캔 출력 어댑터 연결")
    for r in conn.execute("SELECT * FROM curriculum").fetchall():
        if r["title"] in plan_like:
            conn.execute("UPDATE curriculum SET moved_to='PLN' WHERE id=?", (r["id"],))
            continue
        lid = conn.execute(
            "INSERT INTO lag(company_id, question, context, status, origin, created_by, created_at, updated_at)"
            " VALUES (1, ?, ?, 'open', ?, 'system', ?, ?)",
            (r["title"], '{}', f"curriculum#{r['id']} (상태 {r['state']}, 근거 {r['evidence']})", t, t)).lastrowid
        conn.execute("INSERT INTO lag_log(lag_id, from_status, to_status, note, actor, at) VALUES (?,NULL,'open',?, 'system', ?)",
                     (lid, f"커리큘럼 #{r['id']}에서 이관", t))
        conn.execute("UPDATE curriculum SET moved_to=? WHERE id=?", (f"LAG:{lid}", r["id"]))


def _split(sql: str) -> list[str]:
    out, buf = [], []
    for line in sql.splitlines():
        stripped = line.split("--", 1)[0]
        buf.append(stripped)
        if stripped.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt.strip(";").strip():
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out
