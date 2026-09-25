"""合同、发票与交付物的稳定指纹。

同一证据内容生成同一指纹，与键序、首尾空白、数值写法无关；
指纹不含申请编号——同一张发票被扶持资金与银行贷款两边同时
用作进度材料时指纹相同，从而触发跨申请重叠提示。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal


def _canonical(value):
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(Decimal(str(value)).normalize())
    return value


def fingerprint_evidence(evidence_kind: str, content: dict) -> str:
    """对证据内容（合同/发票/交付物）计算稳定指纹。"""
    material = json.dumps(
        {"kind": evidence_kind, "content": _canonical(content)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
