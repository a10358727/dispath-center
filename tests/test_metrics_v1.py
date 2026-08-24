"""Pure parser tests for the metrics-v1 file contract (DG-METRICS-CONTRACT
v1, `docs/DG_METRICS_CONTRACT_DECISION.md`, approved 2026-08-24)."""

from __future__ import annotations

import json

from app.metrics_v1 import (
    MAX_METRICS_BYTES,
    MAX_METRICS_KEYS,
    MAX_STRING_VALUE_BYTES,
    MetricsEntry,
    parse_metrics_v1,
)


def _encode(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_collected_status_with_every_value_type():
    result = parse_metrics_v1(
        _encode(
            {
                "loss": 1,
                "accuracy.top1": True,
                "note": "ok",
                "lr": "0.0001",
            }
        )
    )
    assert result.status == "collected"
    assert result.reason is None
    assert result.entries == (
        MetricsEntry(key="accuracy.top1", value_type="bool", value_text="true"),
        MetricsEntry(key="loss", value_type="int", value_text="1"),
        MetricsEntry(key="lr", value_type="decimal", value_text="0.0001"),
        MetricsEntry(key="note", value_type="string", value_text="ok"),
    )


def test_empty_object_is_collected_with_zero_entries():
    result = parse_metrics_v1(_encode({}))
    assert result.status == "collected"
    assert result.entries == ()


def test_negative_int_and_bool_false():
    result = parse_metrics_v1(_encode({"delta": -5, "converged": False}))
    assert result.status == "collected"
    assert result.entries == (
        MetricsEntry(key="converged", value_type="bool", value_text="false"),
        MetricsEntry(key="delta", value_type="int", value_text="-5"),
    )


def test_oversize_raw_bytes_is_rejected_without_parsing():
    raw = b'{"k":"' + b"x" * (MAX_METRICS_BYTES + 10) + b'"}'
    assert len(raw) > MAX_METRICS_BYTES
    result = parse_metrics_v1(raw)
    assert result.status == "oversize"
    assert result.entries == ()
    assert result.reason


def test_raw_bytes_at_exact_limit_is_not_oversize():
    # A payload whose bytes land exactly on the ceiling still parses
    # normally -- only bytes *beyond* the ceiling are oversize. Pad with
    # leading JSON whitespace (insignificant, ignored by the parser) so the
    # padding itself never has to satisfy any value-shape bound.
    body = _encode({"k": "v"})
    raw = b" " * (MAX_METRICS_BYTES - len(body)) + body
    assert len(raw) == MAX_METRICS_BYTES
    result = parse_metrics_v1(raw)
    assert result.status == "collected"


def test_invalid_utf8_is_rejected():
    result = parse_metrics_v1(b"\xff\xfe\x00\x01")
    assert result.status == "invalid"
    assert result.reason


def test_invalid_json_syntax_is_rejected():
    result = parse_metrics_v1(b"{not json")
    assert result.status == "invalid"


def test_top_level_array_is_rejected():
    result = parse_metrics_v1(_encode([1, 2, 3]))
    assert result.status == "invalid"


def test_top_level_scalar_is_rejected():
    result = parse_metrics_v1(_encode(1))
    assert result.status == "invalid"


def test_nested_object_value_is_rejected():
    result = parse_metrics_v1(_encode({"a": {"b": 1}}))
    assert result.status == "invalid"


def test_array_value_is_rejected():
    result = parse_metrics_v1(_encode({"a": [1, 2]}))
    assert result.status == "invalid"


def test_null_value_is_rejected():
    result = parse_metrics_v1(_encode({"a": None}))
    assert result.status == "invalid"


def test_float_value_is_rejected():
    result = parse_metrics_v1(b'{"loss": 1.5}')
    assert result.status == "invalid"
    assert result.reason == "float_value_rejected"


def test_float_like_exponent_is_rejected():
    result = parse_metrics_v1(b'{"loss": 1e10}')
    assert result.status == "invalid"


def test_nan_and_infinity_constants_are_rejected():
    for literal in (b"NaN", b"Infinity", b"-Infinity"):
        result = parse_metrics_v1(b'{"a": ' + literal + b"}")
        assert result.status == "invalid", literal


def test_key_charset_rejects_uppercase_and_symbols():
    for bad_key in ("Loss", "loss!", "loss key", "loss/rate", "-loss"):
        result = parse_metrics_v1(_encode({bad_key: 1}))
        assert result.status == "invalid", bad_key


def test_key_length_bounds():
    ok_key = "a" * 128
    too_long_key = "a" * 129
    assert parse_metrics_v1(_encode({ok_key: 1})).status == "collected"
    assert parse_metrics_v1(_encode({too_long_key: 1})).status == "invalid"


def test_empty_key_is_rejected():
    result = parse_metrics_v1(_encode({"": 1}))
    assert result.status == "invalid"


def test_key_count_limit():
    ok_payload = {f"k{i}": 1 for i in range(MAX_METRICS_KEYS)}
    too_many_payload = {f"k{i}": 1 for i in range(MAX_METRICS_KEYS + 1)}
    assert parse_metrics_v1(_encode(ok_payload)).status == "collected"
    result = parse_metrics_v1(_encode(too_many_payload))
    assert result.status == "invalid"
    assert result.reason == "too_many_keys"


def test_empty_string_value_is_invalid():
    # run_metrics.value_text is CHECK-constrained to >= 1 byte; the parser
    # rejects the empty string up front so the failure is a recorded
    # `invalid` outcome, never a silent storage error.
    result = parse_metrics_v1(_encode({"note": ""}))
    assert result.status == "invalid"
    assert result.reason == "invalid_value_type"
    assert result.entries == ()


def test_string_value_byte_bound():
    ok_value = "x" * MAX_STRING_VALUE_BYTES
    too_long_value = "x" * (MAX_STRING_VALUE_BYTES + 1)
    assert parse_metrics_v1(_encode({"note": ok_value})).status == "collected"
    assert parse_metrics_v1(_encode({"note": too_long_value})).status == "invalid"


def test_string_value_byte_bound_counts_utf8_bytes_not_characters():
    # Each 'é' is 2 UTF-8 bytes -- half as many characters fit as ASCII.
    value = "é" * (MAX_STRING_VALUE_BYTES // 2)
    assert len(value.encode("utf-8")) == MAX_STRING_VALUE_BYTES
    assert parse_metrics_v1(_encode({"note": value})).status == "collected"
    over = value + "x"
    assert parse_metrics_v1(_encode({"note": over})).status == "invalid"


def test_canonical_decimal_strings_are_accepted_and_typed_decimal():
    for value in ("0.1", "-0.5", "123.456", "0.0001"):
        result = parse_metrics_v1(_encode({"v": value}))
        assert result.status == "collected", value
        assert result.entries[0].value_type == "decimal"
        assert result.entries[0].value_text == value


def test_non_canonical_decimal_strings_fall_back_to_plain_string():
    # canonical_decimal() requires a decimal point with a non-zero trailing
    # digit -- integers, trailing-zero, and leading-zero-less forms are not
    # canonical decimals and are recorded as ordinary strings instead of
    # being rejected.
    for value in ("1", "1.0", "1.10", "01.5", ".5", "1.", "abc"):
        result = parse_metrics_v1(_encode({"v": value}))
        assert result.status == "collected", value
        assert result.entries[0].value_type == "string", value
        assert result.entries[0].value_text == value


def test_huge_integer_literal_is_rejected_once_it_exceeds_the_text_bound():
    ok_digits = "9" * MAX_STRING_VALUE_BYTES
    too_many_digits = "9" * (MAX_STRING_VALUE_BYTES + 1)
    ok_result = parse_metrics_v1(_encode({"n": int(ok_digits)}))
    assert ok_result.status == "collected"
    too_big_result = parse_metrics_v1(_encode({"n": int(too_many_digits)}))
    assert too_big_result.status == "invalid"


def test_entries_are_returned_sorted_by_key():
    result = parse_metrics_v1(_encode({"z": 1, "a": 2, "m": 3}))
    assert [entry.key for entry in result.entries] == ["a", "m", "z"]


def test_non_bytes_input_is_oversize_not_a_crash():
    result = parse_metrics_v1("not bytes")  # type: ignore[arg-type]
    assert result.status == "oversize"


def test_parser_never_raises_on_adversarial_input():
    adversarial_payloads = [
        b"",
        b"null",
        b"true",
        b'"just a string"',
        b"[]",
        b"{",
        b'{"a": 1,}',
        b"\x00\x01\x02",
        b'{"a": [1, [2, [3]]]}',
    ]
    for payload in adversarial_payloads:
        result = parse_metrics_v1(payload)
        assert result.status in ("collected", "invalid", "oversize")
