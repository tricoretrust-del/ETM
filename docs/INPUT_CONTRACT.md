# 입력 계약 v0.1 — 스캔·직접입력 공통 형식 (초안)

스캔(①)과 직접입력은 아래 JSON 한 가지 형식으로 처리 솔루션(②)에 들어옵니다.
스캔 출력은 **어댑터**가 이 형식으로 바꿉니다(아래 §4). 구현: `etm/contract.py`.

## 1. 필드

| 필드 | 자료형 | 필수 | 설명 |
|---|---|---|---|
| `input_id` | 문자열 | ✔ | 받은 자료 한 건의 ID. 재전송이면 달라도 됨(내용으로 재수신 판별) |
| `channel` | `"scan"` \| `"manual"` | ✔ | 입력 경로 |
| `source_ref` | 문자열 \| null | | 원본 위치(사진 경로, 메시지 ID 등). 증빙 연결용 |
| `doc_type` | `"shipment_invoice"` | | 현재는 출하 송장만. 생략 시 출하 송장 |
| `doc_no` | 문자열 \| null | | 송장·전표번호. **문자열**(숫자로 오면 문자열로 바꾸고 불확실 표시) |
| `trade_date` | `"YYYY-MM-DD"` \| null | | 실제 거래(납품)일. 형식이 틀리면 null + 불확실 |
| `customer` | 문자열 \| null | | 공급받는 자(거래처) |
| `site` | 문자열 \| null | | 현장 주소 |
| `contact.name` / `contact.phone` | 문자열 \| null | | 현장 담당자. 전화는 문자열 |
| `lines[]` | 배열 | | 품목 줄 |
| `lines[].item` | 문자열 \| null | | 품목명·코드·별칭 |
| `lines[].qty` | 숫자 \| null | | 수량. 음수 불가. **null(미확정)과 0은 다름** |
| `lines[].unit` | 문자열 \| null | | 단위. 품목 기본 단위와 다르면 확인 요청 |
| `pallets.invoice` | 정수 \| null | | 송장에 적힌 팔레트 수 |
| `pallets.message` | 정수 \| null | | 상차자 메시지의 팔레트 수 |
| `pallets.message_ref` | 문자열 \| null | | 상차 메시지 원본 위치 |
| `vehicle_no` | 문자열 \| null | | 차량번호(문자열) |
| `driver_phone` | 문자열 \| null | | 기사 전화(문자열) |
| `transport.carrier` / `transport.fee` | 문자열 / 숫자 \| null | | 운송업체·운반비. 객체가 없으면 운반비 처리 안 함 |
| `price_ref` | 문자열 \| null | | 가격·계약 참조(보관만) |
| `meta` | 객체 | | 필드별 검증 정보. 아래 §2 |

경로 표기: 줄 필드는 `lines.0.item`, `lines.1.qty` 처럼 씁니다.

## 2. `meta` — 필드별 검증 정보 (스캔이 채움)

```json
"meta": {
  "vehicle_no":   {"handwritten": true, "raw": "8l23"},
  "lines.1.item": {"uncertain": true, "raw": "보도블럭 A"},
  "doc_no":       {"missing": true}
}
```

| 키 | 뜻 | 처리 |
|---|---|---|
| `raw` | 추출 원문(정규화 전) | 확인 화면에 추출값과 함께 표시 |
| `handwritten` | 손글씨 | 항상 사용자 확인(HW-CONFIRM) |
| `uncertain` | 판독 불확실 | 해당 필드만 확인(UNCERTAIN-FIELD) |
| `missing` | 누락 | 값이 없는 것으로 처리 |

기존 기록 후보와 사용자 확정값·확정 시각은 처리 솔루션이 만들어 저장합니다(`check_item`, `confirmed_case`). 입력에 넣을 필요는 없습니다.

## 3. 규칙

- 아는 값만 보내면 됩니다. 비어 있는 값은 처리에 필요할 때만 물어봅니다.
- 미확정은 `null`로 보냅니다. `0`은 "실제로 0"을 뜻합니다.
- 전화·전표·차량번호는 계산하지 않는 문자열입니다.
- 오류(필수 누락·자료형 오류·음수)는 400 응답과 `errors` 목록으로 돌려줍니다.

## 4. 스캔 솔루션 연결 방법

스캔 참고 문서(스캔데이터추출솔루션_Nov.11,2026)에 정리된 송장 항목은 이 형식에 그대로 대응합니다.

| 스캔 항목 | 입력 계약 |
|---|---|
| 송장번호 | `doc_no` |
| 납품일 | `trade_date` |
| 공급받는자 | `customer` |
| 담당자 / 담당자 전화 | `contact.name` / `contact.phone` |
| 현장 | `site` |
| 품목1, 품목2… | `lines[]` |
| 팔레트(송장) / 상차자 메시지 | `pallets.invoice` / `pallets.message` |
| 차량번호·기사전화(손글씨) | `vehicle_no`·`driver_phone` + `meta.*.handwritten=true` |

연결은 둘 중 하나로 하면 됩니다.
1. HTTP: 스캔 결과를 `POST /api/intake` 로 보냅니다(본문 = 위 JSON).
2. 파이썬: `from etm import db, process; process.intake(db.connect(), payload)`

스캔의 실제 출력 형식을 아직 받지 못했습니다. 샘플을 받으면 `etm/adapters/` 에 변환 함수를 추가합니다.
입고전표·거래명세서·그리스 라벨은 다음 단계 범위입니다.

## 5. 예시

`examples/` 폴더를 보세요.
- `scan_normal.json` — 정상 송장. 손글씨 확인 2건만 묻습니다
- `scan_issues.json` — 담당자 불일치, 품목 오탈자, 팔레트 불일치, 단가 없음, 운반비 미확정
- `manual_minimal.json` — 직접입력. 거래처·품목·수량만 있음
