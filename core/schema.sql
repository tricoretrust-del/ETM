-- ETM 보관 구조 v0 (docs/ETM_STORAGE_v0.md). 보관하는 것은 이것뿐이다. 재고·원가·합계·일일보고는 조회로 계산한다.

-- 기준정보 -------------------------------------------------------------------
CREATE TABLE item (                 -- 품목: 판매(P/C/S)·원자재(MAT)·부자재(SUB, 재고 관리 안 함)·비용(CHG)·임대 실물(RNT)·임시(X)
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,             -- P C S MAT CHG RNT X
    base_unit TEXT NOT NULL,        -- 원자재 KG, 제품 M2|EA …
    conv TEXT NOT NULL DEFAULT '{}',-- {"TON":1000,"M3":1450,"BAG":1600} 입력 단위 → 기본 단위
    owner TEXT NOT NULL DEFAULT 'own',
    cost_kind TEXT,                 -- 고정 / 변동 / 운영
    aliases TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE recipe (               -- 배합: 1 MIX 당 원자재 KG (작업일보 RCP)
    mix_code TEXT NOT NULL,
    item_code TEXT NOT NULL REFERENCES item(code),
    kg REAL NOT NULL,
    PRIMARY KEY (mix_code, item_code)
);
CREATE TABLE party (                -- 상대: 청구처·수요처·공급처·운송사·임대사
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    roles TEXT NOT NULL DEFAULT ''
);
-- 출하처 3단계: Lv1 채무자(party) → Lv2 사업(민간: 건설회사 / 관급: 사업명) → Lv3 하차지(현장명·주소)
-- 한 하차지에는 사업 하나, 한 사업에는 채무자 하나. 다른 채무자·사업이 끼면 1차·2차처럼 새 노드로 나눈다.
CREATE TABLE project (              -- Lv2
    id INTEGER PRIMARY KEY,
    bill_party_id INTEGER NOT NULL REFERENCES party(id),   -- Lv1 채무자 (관급이면 지자체)
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '민간' CHECK (kind IN ('민간','관급')),
    UNIQUE (bill_party_id, name)
);
CREATE TABLE site (                 -- Lv3 하차지
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id),
    name TEXT NOT NULL,             -- 현장명·주소 (예: OOO아파트 현장)
    contact TEXT,                   -- 현장관리자 전화 (KPP 출하통보에 필요)
    UNIQUE (project_id, name)
);

-- 사건과 그 결과 -----------------------------------------------------------------
CREATE TABLE event (                -- 일어난 일 1건
    id INTEGER PRIMARY KEY,
    no TEXT,                        -- 송장번호 등 문서번호
    date TEXT NOT NULL,
    kind TEXT NOT NULL,             -- 매입 비용 생산 사용 출하 조정
    party_id INTEGER REFERENCES party(id),
    site_id INTEGER REFERENCES site(id),
    source TEXT NOT NULL,           -- 원천(시트 탭·행) / 직접입력 / DEX
    memo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE move (                 -- 가치 이동
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id),
    item_code TEXT NOT NULL REFERENCES item(code),
    qty REAL NOT NULL,              -- 입력 그대로
    unit TEXT NOT NULL,
    base_qty REAL,                  -- 기본 단위 수량 (환산 불가면 NULL)
    from_loc TEXT NOT NULL,         -- supplier:<id> factory consumed site:<id> party:<id> production opening
    to_loc TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT 'own'
);
CREATE TABLE money (                -- 돈: 채권(AR) / 채무(AP)
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id),
    party_id INTEGER REFERENCES party(id),
    dir TEXT NOT NULL CHECK (dir IN ('AR','AP')),
    item_code TEXT REFERENCES item(code),
    qty REAL,
    unit_price REAL,
    price_state TEXT NOT NULL DEFAULT '확정' CHECK (price_state IN ('확정','미결')),  -- 미결 = 사용자 확인 전
    pending TEXT,                   -- 미결 사유: 단가 / 무상 제공 여부 / 운반비 부담
    amount REAL NOT NULL,           -- 공급가
    vat REAL NOT NULL DEFAULT 0,
    due TEXT,
    method TEXT,
    for_item TEXT REFERENCES item(code)   -- 운반비가 나른 자재 (원가 = 매입 + 그 자재의 운반비)
);
CREATE TABLE settle (               -- 완결 기록 (부분 허용, 취소는 표시만)
    id INTEGER PRIMARY KEY,
    money_id INTEGER NOT NULL REFERENCES money(id),
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    method TEXT,
    memo TEXT NOT NULL DEFAULT '',
    cancelled INTEGER NOT NULL DEFAULT 0
);

-- 이관 시험 기록 (보관 구조의 일부가 아니라, 들어가지 못한 줄의 목록) -------------------
CREATE TABLE issue (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,           -- 탭
    row INTEGER,                    -- 시트 행 번호
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
