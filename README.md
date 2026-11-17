# ETM — Event-Based Transaction Management

실제 사건을 한 번 기록해 재고·채권·채무에 함께 반영합니다. 최우선 목적은 **사용자 입력 수고 감소**입니다.

이 저장소는 ② **처리·보관·조회**를 다룹니다. ① 스캔 추출은 별도로 개발 중입니다.
현재 범위는 DEV-001입니다. 출하 한 건을 입력 → 확인 → 재고·채권·관련 채무 반영 → DB 저장 → 이력 조회까지 처리합니다.

## 실행

Python 3.10 이상만 있으면 됩니다. 설치할 의존성은 없습니다.

```bash
git clone https://github.com/tricoretrust-del/ETM.git
```

```bash
cd ETM
```

가상 시험 데이터로 실행합니다(`data/etm_demo.db`, `--reset`을 주면 새로 만듭니다).

```bash
python3 -m etm demo --reset
```

브라우저에서 http://127.0.0.1:8000 을 엽니다. "확인 필요한 송장" 예제를 누르고 **접수**를 누르면 확인 흐름을 볼 수 있습니다.
입력 칸에는 JSON 이 아니어도 됩니다. `송장번호: …`, `품목: 보도블록A 600 EA` 같은 메모·메시지를 그대로 붙여넣으면 자동으로 변환합니다.

운영 DB로 실행합니다(`data/etm.db`, 시험 데이터 없음).

```bash
python3 -m etm serve
```

검증 테스트를 실행합니다.

```bash
python3 -m unittest -v
```

## 구조

```
etm/contract.py   입력 계약(스캔·직접입력 공통 형식) 검증·정규화
etm/textparse.py  JSON 이 아닌 입력(메모·메시지·틀린 JSON) → 입력 계약 자동 변환
etm/process.py    대조·확인·반영·수정·조회 (핵심)
etm/learn.py      운영 RBK·LAG: 판단 위임 → 사례 → LAG → 검증 → 판정 → 규칙 버전 승인 → 적용 기록
etm/receipt.py    입고(→ 공장) 처리
etm/plan.py       운영 PLN: 실행계획(조치·완료 조건·담당·진행·결과)과 LAG·사건·증빙·RBK 연결, 재분류
etm/db.py         SQLite 스키마·마이그레이션, 운영 룰북·커리큘럼 초기값
etm/seed.py       가상 시험 데이터·입력 예시
etm/server.py     HTTP API + 화면
etm/static/       화면(HTML 한 장)
tests/            DEV-001 §9 필수 검증
docs/             입력 계약, 설계 메모, 화면 캡처
examples/         입력 예시 JSON
```

API:
- `POST /api/intake` — 자료 접수
- `POST /api/events/{id}/answers` — 확인 응답 (`{"answers": {"<check_id>": {"choice": "...", "value": ...}}}`)
- `POST /api/events/{id}/revise` — 수정 (`{"changes": {"lines.0.qty": 550}}`)
- `GET /api/events/{id}` · `GET /api/history` · `GET /api/summary`

자세한 내용은 [docs/INPUT_CONTRACT.md](docs/INPUT_CONTRACT.md)와 [docs/DESIGN.md](docs/DESIGN.md)를 보세요.

## DEV-001 필수 검증 결과

`python3 -m unittest -v` → **50개 통과** (DEV-003 14개, DEV-002 11개, 자동 변환 6개, 학습 경로 4개 포함) (2026-11-17, Python 3.13 / SQLite 3.53)

| DEV-001 §9 항목 | 테스트 | 결과 |
|---|---|---|
| 정상 확정 후 재고·채권·히스토리 일치 | test_01 | 통과 |
| 재수신 시 이중 반영 없음 (+ 같은 전표·다른 내용, 다른 번호·같은 내용) | test_02, 02b | 통과 |
| 품목·담당자 불일치 시 후보 제안 (자동 덮어쓰기 없음) | test_03 | 통과 |
| 손글씨 확인 요구 (원본·추출값 표시) | test_04 | 통과 |
| 팔레트 송장·메시지 불일치 및 자료 누락 | test_05 | 통과 |
| 단가 미확정 상태 저장 (NULL ≠ 0) | test_06 | 통과 |
| 일부 필드 보완 후 미반영 부분만 반영 | test_07 | 통과 |
| 수량 수정 후 재고·채권 정확성 (차액·이력 보존) | test_08 | 통과 |
| 저장 도중 실패 후 일관성 (장애 주입 → 전부 롤백) | test_09 | 통과 |
| 새로고침·재접속 후 이력 유지 | test_10 + 화면 캡처 04 | 통과 |
| (추가) 값 없는 '맞음' 거부, 문자열 필드·형식 오류, HTTP 왕복 | test_11, 12, api | 통과 |

화면 캡처: `docs/screens/`
- 01: 확인할 것 5건
- 02: 일부 응답 후
- 03: 완료(금액 미확정·수금·회수 미결 표시)
- 04: 새로고침 후 이력 유지

## 운영 RBK·PLN·LAG (DEV-002·DEV-003)

프로그램은 활성 규칙 버전이 없는 판단을 모두 사용자에게 묻습니다. 반복된 판단은 후보로 보여 주기만 하고, 사용자가 **LAG 등록 → 검증 → 판정 → 권한자 승인**을 거쳐야 규칙이 활성화됩니다. 실행할 조치는 운영 **PLN**(실행계획)에 두고 LAG·사건·증빙·규칙 버전과 연결합니다. 상단 **RBK·PLN·LAG**에서 승인 대기 버전, 후보, LAG, 실행계획, 규칙 버전, 근거 추적, 재분류 기록을 볼 수 있습니다. 결과 보고: [DEV-002](docs/DEV-002_REPORT.md), [DEV-003](docs/DEV-003_REPORT.md).

## 미해결 사항 (TRNK 결정 필요)

0. DEV-002 미결 사항은 [docs/DEV-002_REPORT.md §7](docs/DEV-002_REPORT.md)을 보세요.

1. **스캔 실제 출력 샘플**이 아직 없습니다. 공통 형식 초안에 맞춰 어댑터를 만들어야 합니다.
2. **처리 건수 집계 규칙**이 정해지지 않았습니다. 지금은 처리 1회(접수·확인·수정)를 이력 1줄로 셉니다.
3. **세금 조건**: 단가에 부가세 조건(`tax`)을 저장만 합니다. 금액은 공급가 기준입니다.
4. **수금·지급·팔레트 회수 실행 화면**은 후속 과제입니다. 지금은 미결로 조회만 됩니다.
5. **담당자 연락처 판단**: 담당자를 '유지'하면 연락처는 입력값을 그대로 씁니다. 과거 연락처와 비교하지 않습니다.
6. **배포·다중 사용자**: 지금 구성은 한 대의 PC에서 쓰는 것을 전제로 합니다.
