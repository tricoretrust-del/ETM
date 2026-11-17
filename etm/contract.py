"""입력 계약: 스캔·직접입력이 합류하는 공통 형식.

자세한 규격은 docs/INPUT_CONTRACT.md 참고.
여기서는 형식 검증과 '경로 → 필드' 정규화만 한다. 업무 판단은 process.py 가 한다.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

DOC_TYPES = {"shipment_invoice": "shipment_invoice", "출하송장": "shipment_invoice", "송장": "shipment_invoice",
             "receipt": "receipt", "입고전표": "receipt", "other": "other", "statement": "other", "거래명세서": "other"}
# 재고 반영을 하는 종류. other 는 원본·변환값을 보관만 한다.
PROCESSED_DOC_TYPES = {"shipment_invoice", "receipt"}
DOC_LABEL = {"shipment_invoice": "출하송장", "receipt": "입고", "other": "기타 자료", "unknown": "종류 확인 대기"}
# 사용자에게 묻는 자료 종류(=가치 이동 방향)
DOC_KIND_CHOICES = {"shipment_invoice": "출하 (공장 → 거래처)", "receipt": "입고 (→ 공장)",
                    "other": "재고와 무관 (보관만)"}
CHANNELS = {"scan", "manual"}

# 숫자로 계산하지 않고 문자열로만 다루는 필드
STRING_FIELDS = {"doc_no", "vehicle_no", "driver_phone", "contact.phone"}


class ContractError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _field(value, meta: dict | None = None, source: str = "input") -> dict:
    meta = meta or {}
    flags = [f for f in ("handwritten", "uncertain") if meta.get(f)]
    if value is None or meta.get("missing"):
        flags.append("missing")
    return {
        "value": value,
        "orig": value,
        "raw": meta.get("raw", value),
        "flags": sorted(set(flags)),
        "source": source,
    }


def _str_or_none(v, path: str, errors: list[str], meta: dict) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        errors.append(f"{path}: 문자열이어야 합니다")
        return None
    if isinstance(v, (int, float)):
        # 전화·전표·차량번호를 숫자로 받으면 앞자리 0이 사라질 수 있다 → 문자열화 + 불확실 표시
        meta.setdefault(path, {})["uncertain"] = True
        meta[path].setdefault("raw", v)
        return str(v)
    if not isinstance(v, str):
        errors.append(f"{path}: 문자열이어야 합니다")
        return None
    v = v.strip()
    return v or None


def _num_or_none(v, path: str, errors: list[str]) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.replace(",", "").strip()):
            v = float(v.replace(",", ""))
        else:
            errors.append(f"{path}: 숫자 또는 null 이어야 합니다")
            return None
    if v < 0:
        errors.append(f"{path}: 음수는 허용하지 않습니다")
        return None
    return int(v) if float(v).is_integer() else float(v)


def normalize(payload: dict) -> tuple[dict, dict]:
    """payload → (header, fields). 오류가 있으면 ContractError."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ContractError(["입력은 JSON 객체여야 합니다"])
    meta: dict = {k: dict(v) for k, v in (payload.get("meta") or {}).items() if isinstance(v, dict)}

    input_id = payload.get("input_id")
    if not isinstance(input_id, str) or not input_id.strip():
        errors.append("input_id: 필수 문자열")
    channel = payload.get("channel")
    if channel not in CHANNELS:
        errors.append("channel: scan 또는 manual")
    doc_type = DOC_TYPES.get(payload.get("doc_type") or "shipment_invoice")
    if not doc_type:
        errors.append("doc_type: shipment_invoice(출하) | receipt(입고) | other(보관만)")

    trade_date = payload.get("trade_date")
    if trade_date is not None:
        try:
            date.fromisoformat(str(trade_date))
            trade_date = str(trade_date)
        except ValueError:
            meta.setdefault("trade_date", {}).update({"uncertain": True, "raw": trade_date})
            trade_date = None

    f: dict[str, dict] = {}

    def put(path, value):
        f[path] = _field(value, meta.get(path))

    put("doc_no", _str_or_none(payload.get("doc_no"), "doc_no", errors, meta))
    put("trade_date", trade_date)
    put("customer", _str_or_none(payload.get("customer"), "customer", errors, meta))
    put("site", _str_or_none(payload.get("site"), "site", errors, meta))
    put("supplier", _str_or_none(payload.get("supplier"), "supplier", errors, meta))     # 보낸 업체(입고)
    put("receiver", _str_or_none(payload.get("receiver"), "receiver", errors, meta))     # 받은 곳(입고)
    contact = payload.get("contact") or {}
    put("contact.name", _str_or_none(contact.get("name"), "contact.name", errors, meta))
    put("contact.phone", _str_or_none(contact.get("phone"), "contact.phone", errors, meta))
    put("vehicle_no", _str_or_none(payload.get("vehicle_no"), "vehicle_no", errors, meta))
    put("driver_phone", _str_or_none(payload.get("driver_phone"), "driver_phone", errors, meta))

    lines = payload.get("lines")
    if lines is None:
        lines = []
    if not isinstance(lines, list):
        errors.append("lines: 배열이어야 합니다")
        lines = []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            errors.append(f"lines[{i}]: 객체여야 합니다")
            continue
        put(f"lines.{i}.item", _str_or_none(ln.get("item"), f"lines.{i}.item", errors, meta))
        put(f"lines.{i}.qty", _num_or_none(ln.get("qty"), f"lines.{i}.qty", errors))
        put(f"lines.{i}.unit", _str_or_none(ln.get("unit"), f"lines.{i}.unit", errors, meta))
        if ln.get("owner") is not None:   # 임대품 소유자(확인된 경우만): 'own' 또는 'party:<id>'
            put(f"lines.{i}.owner", _str_or_none(ln.get("owner"), f"lines.{i}.owner", errors, meta))

    pallets = payload.get("pallets") or {}
    put("pallets.invoice", _num_or_none(pallets.get("invoice"), "pallets.invoice", errors))
    put("pallets.message", _num_or_none(pallets.get("message"), "pallets.message", errors))

    transport = payload.get("transport")
    if transport is not None:
        put("transport.carrier", _str_or_none(transport.get("carrier"), "transport.carrier", errors, meta))
        put("transport.fee", _num_or_none(transport.get("fee"), "transport.fee", errors))

    if errors:
        raise ContractError(errors)

    header = {
        "input_id": input_id.strip(),
        "channel": channel,
        "source_ref": payload.get("source_ref"),
        "doc_type": doc_type,
        "pallet_message_ref": pallets.get("message_ref"),
        "price_ref": payload.get("price_ref"),
        "line_count": len(lines),
    }
    return header, f


def payload_hash(payload: dict) -> str:
    """재수신 판별용. input_id·source_ref 등 전달 경로 정보는 제외하고 내용만 비교한다."""
    content = {k: v for k, v in payload.items() if k not in ("input_id", "source_ref", "received_at")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
