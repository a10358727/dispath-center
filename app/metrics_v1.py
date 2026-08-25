"""Pure parser for the metrics-v1 file contract (DG-METRICS-CONTRACT v1,
Option A -- `docs/DG_METRICS_CONTRACT_DECISION.md`,
`docs/DECISIONS.md` 2026-08-24 「DG-METRICS-CONTRACT v1：A 核准」).

Workloads write a single flat JSON object to `results/{job_id}/metrics.json`,
collected by the *existing* rsync result pull -- zero new remote commands,
INV-SSH-4 unchanged. This module is the only place that interprets those
bytes; it is pure (no I/O, no DB access) so every caller controls exactly
which bytes are read and how many (the job-finish hook reads at most
`MAX_METRICS_BYTES + 1` bytes so an oversize file is classified without an
unbounded local read).

Every unexpected shape fails closed to `invalid`/`oversize` with a bounded,
fixed (never user-derived) reason string -- this module never raises for
malformed input. A parse failure here must never propagate into job status,
reconciliation, or scheduling; the sentinel exit_code remains the only
terminal-state source (INV-SSH-6). Callers are responsible for the fourth
contract status, `missing` (file absent), which this module never sees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

from app.project_bootstrap import canonical_decimal

#: Whole-file size ceiling from the metrics-v1 contract (§2 of the decision
#: packet). Callers must bound their read to this + 1 byte so an oversize
#: file never requires an unbounded local read to classify.
MAX_METRICS_BYTES = 64 * 1024
MAX_METRICS_KEYS = 256
MAX_STRING_VALUE_BYTES = 4096

#: Closed key charset/length from the contract: `[a-z0-9_.]{1,128}`.
_KEY_RE = re.compile(r"^[a-z0-9_.]{1,128}$")

MetricsStatus = Literal["collected", "invalid", "oversize"]
MetricValueType = Literal["int", "bool", "string", "decimal"]


@dataclass(frozen=True)
class MetricsEntry:
    """One accepted `key -> value` pair, value already in canonical text form."""

    key: str
    value_type: MetricValueType
    value_text: str


@dataclass(frozen=True)
class ParsedMetricsV1:
    """Outcome of parsing one `metrics.json` payload.

    `entries` is populated only when `status == "collected"`; `invalid` and
    `oversize` always carry a bounded, fixed `reason` string and empty
    `entries`.
    """

    status: MetricsStatus
    entries: tuple[MetricsEntry, ...]
    reason: str | None


class _RejectedNumberLiteral(ValueError):
    """Internal control-flow signal: a JSON float/NaN/Infinity literal was seen.

    `canonical_json()` (app/execution_contract.py) already bans Python
    `float` from every canonical Product contract; metrics-v1 keeps the same
    ban at the parser boundary via `json.loads()`'s `parse_float`/
    `parse_constant` hooks so no float literal ever reaches this module's
    value classification.
    """


def _reject_number_literal(_text: str) -> float:
    raise _RejectedNumberLiteral("float value")


def parse_metrics_v1(raw: bytes) -> ParsedMetricsV1:
    """Parse raw `metrics.json` bytes into structured metrics entries.

    Pure and total: never raises, performs no I/O. `raw` must already be
    bounded by the caller -- more than `MAX_METRICS_BYTES` bytes is
    classified `oversize` without attempting to parse.
    """

    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_METRICS_BYTES:
        return ParsedMetricsV1(status="oversize", entries=(), reason="raw_bytes_exceed_64kib")

    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        return ParsedMetricsV1(status="invalid", entries=(), reason="invalid_utf8")

    try:
        parsed = json.loads(
            text,
            parse_float=_reject_number_literal,
            parse_constant=_reject_number_literal,
        )
    except _RejectedNumberLiteral:
        return ParsedMetricsV1(status="invalid", entries=(), reason="float_value_rejected")
    except json.JSONDecodeError:
        return ParsedMetricsV1(status="invalid", entries=(), reason="invalid_json")

    if not isinstance(parsed, dict):
        return ParsedMetricsV1(status="invalid", entries=(), reason="top_level_not_object")
    if len(parsed) > MAX_METRICS_KEYS:
        return ParsedMetricsV1(status="invalid", entries=(), reason="too_many_keys")

    entries: list[MetricsEntry] = []
    for key, value in parsed.items():
        if not isinstance(key, str) or _KEY_RE.match(key) is None:
            return ParsedMetricsV1(status="invalid", entries=(), reason="invalid_key")
        classified = _classify_value(value)
        if classified is None:
            return ParsedMetricsV1(status="invalid", entries=(), reason="invalid_value_type")
        value_type, value_text = classified
        entries.append(MetricsEntry(key=key, value_type=value_type, value_text=value_text))

    entries.sort(key=lambda entry: entry.key)
    return ParsedMetricsV1(status="collected", entries=tuple(entries), reason=None)


def _classify_value(value: object) -> tuple[MetricValueType, str] | None:
    """Classify one JSON value per the metrics-v1 contract, or reject it.

    Order matters: a JSON string is checked against the canonical-decimal
    grammar (`canonical_decimal()`, `app/project_bootstrap.py:411`) first --
    that grammar always requires a decimal point with a non-zero final digit
    and is at most 128 bytes, so it can never collide with the plain-string
    branch's independent 4096-byte bound. Any string that does not parse as a
    canonical decimal is recorded as `string` instead of being rejected.
    """

    if isinstance(value, bool):
        return "bool", "true" if value else "false"
    if isinstance(value, int):
        # The contract does not name an explicit integer digit limit, but
        # `value_text` is stored as canonical text alongside bounded
        # string/decimal values -- keep every stored value under the same
        # 4096-byte ceiling so a pathological huge-digit literal cannot
        # widen the storage contract implicitly.
        text = str(value)
        if len(text.encode("ascii")) > MAX_STRING_VALUE_BYTES:
            return None
        return "int", text
    if isinstance(value, str):
        # `run_metrics.value_text` is CHECK-constrained to 1..4096 bytes, so
        # an empty string must be rejected here (fail-closed `invalid`) rather
        # than surfacing later as a storage error that would leave the job
        # with no collection-evidence row at all.
        if not value:
            return None
        try:
            canonical_decimal(value, "metrics_value")
        except ValueError:
            pass
        else:
            return "decimal", value
        if len(value.encode("utf-8")) > MAX_STRING_VALUE_BYTES:
            return None
        return "string", value
    return None


__all__ = [
    "MAX_METRICS_BYTES",
    "MAX_METRICS_KEYS",
    "MAX_STRING_VALUE_BYTES",
    "MetricsEntry",
    "MetricsStatus",
    "MetricValueType",
    "ParsedMetricsV1",
    "parse_metrics_v1",
]
