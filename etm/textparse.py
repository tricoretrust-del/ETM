"""JSON 이 아닌 입력을 입력 계약(JSON) 형식으로 자동 변환한다.

받는 것
1. 조금 틀린 JSON (작은따옴표, 끝 쉼표, True/False/None, 코드블록 ```)
2. "항목: 값" 형태의 메모·메시지·스캔 텍스트
   예) 송장번호: 20261111-01 / 납품일: 2026-11-11 / 공급받는자: 금성백조
       담당자: 김경섭 소장님 / 품목1: 보도블록A 600 EA / 팔레트: 6
       차량번호: 5006 (손글씨)
3. 이름표 없는 문장 — 날짜·전표번호·전화번호 모양만 골라 '불확실'로 표시

원문은 payload["raw_text"] 에 그대로 남겨 증빙과 연결한다.
라벨 없이 추측한 값은 meta.uncertain=true 로 표시해 사용자 확인을 받는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

# 항목 이름(공백 제거·소문자) → 입력 계약 경로. 앞에 있는 것이 우선.
# 입고전표에서는 '업체명'이 보낸 업체, '납품처'가 받은 곳이다(출하와 반대). 먼저 확인한다.
RECEIPT_KEYS: list[tuple[str, str]] = [
    (r"(업체명|공급자|공급업체|보낸곳|발송처|출고처)", "supplier"),
    (r"(납품처|받는곳|입고처|인수처)", "receiver"),
]
KEYS: list[tuple[str, str]] = [
    (r"(상차|메시지|메세지|카톡).*(팔레트|파렛트|빠레트|plt)", "pallets.message"),
    (r"(팔레트|파렛트|빠레트|plt)", "pallets.invoice"),
    (r"(담당자|현장담당|소장).*(전화|연락처|핸드폰|휴대폰|hp|tel)", "contact.phone"),
    (r"(기사|운전).*(전화|연락처|핸드폰|휴대폰|hp|tel)", "driver_phone"),
    (r"^(기사|운전기사|기사님)$", "driver_phone"),
    (r"(차량|차번|차량번호)", "vehicle_no"),
    (r"(담당자|인수자|받는분|소장)", "contact.name"),
    (r"(송장|전표|문서)(번호|no)?|^no$|^번호$", "doc_no"),
    (r"(납품일|출하일|입고일|거래일|일자|날짜|date)", "trade_date"),
    (r"(공급받는자|거래처|납품처|고객|업체|customer)", "customer"),
    (r"(현장|주소|납품장소|site)", "site"),
    (r"(운송사|운송업체|운반업체|carrier)", "transport.carrier"),
    (r"(운반비|운송비|운임|freight)", "transport.fee"),
    (r"(품목|제품|품명|상품|유형|규격|타입|종류|item)\d*", "line.item"),
    (r"(수량|qty)", "line.qty"),
    (r"(단위|unit)", "line.unit"),
]
TITLES = r"\s*(소장님|소장|과장님|과장|부장님|부장|대리님|대리|팀장님|팀장|실장님|실장|기사님|님)$"
CONFIRM_WORDS = {"맞음", "맞습니다", "동일", "위와같음", "위와 같음", "ok", "o"}

DATE_RE = re.compile(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
PHONE_RE = re.compile(r"(?<!\d)(01[016789])[-.\s]?(\d{3,4})[-.\s]?(\d{4})(?!\d)")
DOCNO_RE = re.compile(r"(?<!\d)(20\d{6}-\d{1,3})(?!\d)")
# 이름과 수량 사이에는 공백이나 x·× 가 있어야 한다. 'P10', 'SDC08' 같은 코드는 쪼개지 않는다.
LINE_RE = re.compile(r"^(?P<name>.*?\S)(?:\s+|\s*[x×*]\s*)(?P<qty>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>[A-Za-z가-힣㎡]{0,4}\d?)\s*$")


def to_payload(text: str, known_customers: list[str] | None = None,
               doc_type: str | None = None) -> tuple[dict, list[str]]:
    """텍스트 → (입력 계약 payload, 변환 메모). 변환할 내용이 없으면 ValueError.

    doc_type 을 주지 않으면 문서 종류를 **판단하지 않는다**. payload["doc_type"] 은 None 으로 두고,
    참고용 추정(doc_type_guess)과 양식 키(form_key)만 붙인다. 종류는 사용자 확인 또는 승인된 규칙이 정한다.
    doc_type 을 주면 그 종류의 항목 해석으로 다시 변환한다."""
    text = (text or "").strip()
    if not text:
        raise ValueError("입력 내용이 비어 있습니다")
    loose = _loose_json(text)
    if loose is not None:
        return loose, ["JSON 형식 오류를 자동으로 고쳤습니다"]
    return _from_text(text, known_customers or [], doc_type)


# --------------------------------------------------------------------------- 1) 느슨한 JSON
def _loose_json(text: str) -> dict | None:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    if not t.startswith("{"):
        return None
    t = re.sub(r",\s*([}\]])", r"\1", t)
    t = re.sub(r"\bTrue\b", "true", re.sub(r"\bFalse\b", "false", re.sub(r"\bNone\b", "null", t)))
    if "'" in t and '"' not in t:
        t = t.replace("'", '"')
    t = re.sub(r"([{,]\s*)([A-Za-z_][\w.]*)\s*:", r'\1"\2":', t)  # 따옴표 없는 키
    try:
        v = json.loads(t)
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


# --------------------------------------------------------------------------- 2) 항목: 값 텍스트
def _from_text(text: str, known_customers: list[str], decided: str | None) -> tuple[dict, list[str]]:
    guess = _doc_type(text)
    doc_type = decided or guess          # 항목 해석에만 쓴다. 결정은 아님
    p: dict = {"channel": "manual", "doc_type": decided, "raw_text": text}
    if not decided:
        p["doc_type_guess"] = guess
    labels: list[str] = []
    meta: dict[str, dict] = {}
    notes: list[str] = []
    lines: list[dict] = []
    unknown: list[str] = []
    found = 0

    def put(path, value, raw=None, **flags):
        nonlocal found
        found += 1
        cur = p
        parts = path.split(".")
        for k in parts[:-1]:
            cur = cur.setdefault(k, {})
        cur[parts[-1]] = value
        m = {k: v for k, v in flags.items() if v}
        if raw is not None and str(raw) != str(value):
            m["raw"] = raw
        if m:
            meta.setdefault(path, {}).update(m)

    for raw_line in text.splitlines():
        line = re.sub(r"^\s*([-*•·]|\d+[.)]|[a-z][.)]|[ivx]+\.)\s+", "", raw_line).strip()
        if not line:
            continue
        m = re.match(r"^(?P<k>[^:：=]{1,20})\s*[:：=]\s*(?P<v>.*)$", line)
        if not m:
            # 콜론 없이 "기사 010-…", "팔레트 6" 처럼 쓴 경우: 첫 낱말이 항목 이름이면 받아들인다
            m = re.match(r"^(?P<k>\S{1,10})\s+(?P<v>.+)$", line)
            if not m or _path_for(m["k"], doc_type) is None:
                unknown.append(line)
                continue
        key, val = m["k"].strip(), m["v"].strip()
        labels.append(re.sub(r"[\s\d]", "", key))
        # 괄호 안이 메모(손글씨·확인 요구 등)일 때만 떼어낸다. '(가상)', '(주)' 같은 이름 일부는 유지
        note_parts = [x for x in re.findall(r"\(([^)]*)\)", val) if _is_note(x)]
        note = " ".join(note_parts)
        for x in note_parts:
            val = val.replace(f"({x})", "")
        val = re.sub(r"\s{2,}", " ", val).strip()
        hand = "손글씨" in note or "수기" in note or "수기" in val
        unsure = any(w in note for w in ("불확실", "확인필요", "확인 필요", "?")) or val.endswith("?")
        val = val.rstrip("?").strip()
        path = _path_for(key, doc_type)
        if path is None:
            unknown.append(line)
            continue
        if not val or val.replace(" ", "").lower() in CONFIRM_WORDS:
            notes.append(f"'{key}: {m['v'].strip()}' — 값이 없어 건너뜀")
            continue

        if path == "line.item":
            ln = _parse_line(val)
            ln["_hand"], ln["_unsure"] = hand, unsure
            lines.append(ln)
            found += 1
        elif path in ("line.qty", "line.unit"):
            if not lines:
                lines.append({"item": None, "qty": None, "unit": None, "_hand": False, "_unsure": False})
            if path == "line.qty":
                q, u = _num_unit(val)
                lines[-1]["qty"] = q
                if u and not lines[-1].get("unit"):
                    lines[-1]["unit"] = u
            else:
                lines[-1]["unit"] = val
            found += 1
        elif path == "trade_date":
            d, guessed = _date(val)
            put(path, d, raw=val, uncertain=guessed or unsure or d is None, handwritten=hand)
        elif path in ("contact.phone", "driver_phone"):
            ph = _phone(val)
            put(path, ph or val, raw=val, uncertain=unsure or ph is None, handwritten=hand)
        elif path == "contact.name":
            ph = _phone(val)
            name = re.sub(TITLES, "", PHONE_RE.sub("", val)).strip(" ,/")
            put(path, name or None, raw=val, uncertain=unsure, handwritten=hand)
            if ph and "contact" in p and "phone" not in p["contact"]:
                put("contact.phone", ph, handwritten=hand)
        elif path in ("pallets.invoice", "pallets.message", "transport.fee"):
            q, _ = _num_unit(val)
            put(path, q, raw=val, uncertain=unsure or q is None, handwritten=hand)
        else:
            put(path, val, uncertain=unsure, handwritten=hand)

    # 3) 이름표 없는 줄에서 모양으로 찾기 (불확실 표시)
    rest = "\n".join(unknown)
    if "doc_no" not in p and (m := DOCNO_RE.search(rest)):
        put("doc_no", m[1], uncertain=True)
        notes.append(f"전표번호 추정: {m[1]}")
    if "trade_date" not in p:
        d, _ = _date(rest)
        if d:
            src = (DATE_RE.search(rest) or SHORT_DATE_RE.search(rest))[0]
            put("trade_date", d, raw=src, uncertain=True)
            notes.append(f"날짜 추정: {d}")
    for u in unknown:
        for pm in PHONE_RE.finditer(u):
            ph = _phone(pm[0])
            if re.search(r"기사|운전|차량", u) and "driver_phone" not in p:
                put("driver_phone", ph, uncertain=True)
                notes.append(f"기사전화 추정: {ph}")
            elif re.search(r"담당|소장|현장", u) and "phone" not in p.get("contact", {}):
                put("contact.phone", ph, uncertain=True)
                notes.append(f"담당자 전화 추정: {ph}")
            else:
                notes.append(f"전화번호 {ph} — 누구 번호인지 알 수 없어 넣지 않음")
    if "customer" not in p and known_customers:
        hits = [n for n in known_customers if n and n in text]
        if len(hits) == 1:
            put("customer", hits[0])
            notes.append(f"거래처: 본문에서 기존 거래처 '{hits[0]}' 확인")

    if lines:
        p["lines"] = []
        for i, ln in enumerate(lines):
            p["lines"].append({"item": ln.get("item"), "qty": ln.get("qty"), "unit": ln.get("unit")})
            if ln["_hand"] or ln["_unsure"]:
                for f in ("item", "qty"):
                    meta[f"lines.{i}.{f}"] = {"handwritten": ln["_hand"], "uncertain": ln["_unsure"]}
            if ln.get("item") and ln.get("qty") is None:
                notes.append(f"{i + 1}번 품목 '{ln['item']}' 수량을 찾지 못함")
    if unknown:
        shown = [u for u in unknown if not (DOCNO_RE.search(u) or PHONE_RE.search(u) or DATE_RE.search(u))]
        if shown:
            notes.append("인식하지 못한 줄: " + " / ".join(shown[:5]) + (" …" if len(shown) > 5 else ""))
    if found == 0:
        raise ValueError("인식할 수 있는 항목이 없습니다. '송장번호: …', '거래처: …', '품목: 이름 수량 단위' 처럼 '항목: 값' 형태로 적어 주세요")
    if meta:
        p["meta"] = meta
    p["input_id"] = "text-" + hashlib.sha256(text.encode()).hexdigest()[:12]
    # 양식 키: 어떤 항목들로 이루어진 자료인지. 같은 양식이면 같은 판단을 재사용할 수 있다.
    p["form_key"] = "·".join(sorted(set(labels)))
    return p, notes


NOTE_WORDS = ("손글씨", "수기", "확인", "불확실", "요구", "자동", "?", "메모", "참고")


def _is_note(x: str) -> bool:
    return any(w in x for w in NOTE_WORDS) or (" " in x.strip() and len(x) > 6)


def _doc_type(text: str) -> str:
    keys = " ".join(re.findall(r"^\s*(?:[-*•·]|\d+[.)])?\s*([^:：=\n]{1,20})\s*[:：=]", text, re.M))
    if "입고" in keys or "입고전표" in text:
        return "receipt"
    if "거래명세서" in text:
        return "other"
    return "shipment_invoice"


def _path_for(key: str, doc_type: str = "shipment_invoice") -> str | None:
    k = re.sub(r"\s", "", key).lower()
    for pat, path in (RECEIPT_KEYS + KEYS if doc_type == "receipt" else KEYS):
        if re.search(pat, k):
            return path
    return None


def _num_unit(val: str):
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z가-힣㎡]{0,4})", val)
    if not m:
        return None, None
    n = float(m[1].replace(",", ""))
    return (int(n) if n.is_integer() else n), (m[2] or None)


def _parse_line(val: str) -> dict:
    m = LINE_RE.match(val)
    if m and m["name"] and not re.fullmatch(r"[\d\s,.-]+", m["name"]):
        n = float(m["qty"].replace(",", ""))
        return {"item": m["name"].strip(" ,/-"), "qty": int(n) if n.is_integer() else n, "unit": m["unit"] or None}
    return {"item": val, "qty": None, "unit": None}


def _phone(val: str) -> str | None:
    m = PHONE_RE.search(val)
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def _date(val: str) -> tuple[str | None, bool]:
    m = DATE_RE.search(val)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat(), False
        except ValueError:
            return None, True
    m = SHORT_DATE_RE.search(val)
    if m:
        try:
            return date(date.today().year, int(m[1]), int(m[2])).isoformat(), True  # 연도 추정
        except ValueError:
            return None, True
    return None, True
