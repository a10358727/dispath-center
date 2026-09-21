"""WP9 bounded, read-only Product Run evidence bridge tools."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.mcp_bridge import _MAX_RESULT_CHARS, _build_mcp
from tests.test_mcp_bridge import _call_tool, _tool_text, make_config


PLAN = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def _json_result(handler, tool: str, arguments: dict) -> tuple[dict, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    async def recorded(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return await handler(request)

    result = asyncio.run(_call_tool(make_config(), recorded, tool, arguments))
    text = _tool_text(result)
    assert len(text) <= _MAX_RESULT_CHARS
    return json.loads(text), requests


def test_get_run_uses_product_detail_and_bounds_timeline_as_valid_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "plan_id": PLAN, "project_id": "project-1", "state": "running",
            "plan_digest": "a" * 64,
            "timeline": [{"kind": "event", "note": "x" * 900} for _ in range(40)],
        })

    body, requests = _json_result(handler, "get_run", {"plan_id": PLAN})
    assert [(r.method, r.url.path) for r in requests] == [("GET", f"/api/v2/runs/{PLAN}")]
    assert len(body["timeline"]) == 12
    assert body["timeline_truncated"] is True
    assert body["plan_digest"] == "a" * 64


def test_metrics_resolves_job_from_run_then_bounds_entries():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v2/runs/{PLAN}":
            return httpx.Response(200, json={"plan_id": PLAN, "job": {"id": 37}})
        assert request.url.path == "/jobs/37/metrics"
        return httpx.Response(200, json={
            "job_id": 37, "collection_status": "collected", "source_sha256": "b" * 64,
            "metrics": [{"key": f"m{i}", "value_text": str(i)} for i in range(60)],
        })

    body, requests = _json_result(handler, "get_run_metrics", {"plan_id": PLAN})
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", f"/api/v2/runs/{PLAN}"), ("GET", "/jobs/37/metrics")
    ]
    assert body["availability"] == "known"
    assert body["source_sha256"] == "b" * 64
    assert len(body["metrics"]) == 32
    assert body["truncated"] is True


def test_metrics_and_log_are_unknown_without_job_and_make_no_second_call():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"plan_id": PLAN, "job": None})

    for tool in ("get_run_metrics", "get_run_log_tail"):
        body, requests = _json_result(handler, tool, {"plan_id": PLAN})
        assert body["availability"] == "unknown"
        assert body["reason"] == "job_not_materialized"
        assert len(requests) == 1


def test_metrics_endpoint_unavailable_remains_unknown():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v2/runs/{PLAN}":
            return httpx.Response(200, json={"plan_id": PLAN, "job": {"id": 37}})
        return httpx.Response(404, json={"detail": "metrics-v1 is disabled"})

    body, requests = _json_result(handler, "get_run_metrics", {"plan_id": PLAN})
    assert len(requests) == 2
    assert body == {
        "plan_id": PLAN, "job_id": 37, "availability": "unknown",
        "reason": "metrics_unavailable", "metrics": [], "truncated": False,
    }


def test_artifacts_are_metadata_only_and_page_is_clamped_with_cursor():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "plan_id": PLAN, "availability": "known", "metadata_only": True,
            "items": [{"relative_path": "result.json", "sha256": "c" * 64,
                       "size_bytes": 12, "metadata_only": True}],
            "next_cursor": "next", "declared_outputs": {"availability": "known", "items": []},
        })

    body, requests = _json_result(
        handler, "get_run_artifacts", {"plan_id": PLAN, "limit": 999, "cursor": "page"}
    )
    assert requests[0].url.path == f"/api/v2/runs/{PLAN}/artifacts"
    assert dict(requests[0].url.params) == {"limit": "25", "cursor": "page"}
    assert body["metadata_only"] is True
    assert body["items"][0]["sha256"] == "c" * 64
    assert "content" not in body["items"][0]


def test_log_resolves_job_clamps_lines_and_reports_character_truncation():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v2/runs/{PLAN}":
            return httpx.Response(200, json={"plan_id": PLAN, "job": {"id": 9}})
        assert request.url.params["lines"] == "80"
        return httpx.Response(200, json={"job_id": 9, "status": "running", "log_tail": "z" * 9000})

    body, requests = _json_result(
        handler, "get_run_log_tail", {"plan_id": PLAN, "lines": 500}
    )
    assert [r.url.path for r in requests] == [f"/api/v2/runs/{PLAN}", "/jobs/9/log"]
    assert body["availability"] == "known"
    assert len(body["log_tail"]) == 2400
    assert body["original_characters"] == 9000
    assert body["truncated"] is True


def test_compare_runs_forwards_exact_query_and_preserves_unknown():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "left_plan_id": PLAN, "right_plan_id": OTHER,
            "dimensions": {"terminal_result": {"availability": "unknown"}},
            "unknown_dimensions": ["terminal_result"], "truncated": False,
        })

    body, requests = _json_result(
        handler, "compare_runs", {"left_plan_id": PLAN, "right_plan_id": OTHER}
    )
    assert requests[0].url.path == "/api/v2/runs/compare"
    assert dict(requests[0].url.params) == {"left_plan_id": PLAN, "right_plan_id": OTHER}
    assert body["dimensions"]["terminal_result"]["availability"] == "unknown"


def test_wp9_tools_are_read_only_and_no_forbidden_authority_is_registered():
    tools = _build_mcp(make_config())._tool_manager._tools
    for name in ("get_run", "get_run_metrics", "get_run_artifacts", "get_run_log_tail", "compare_runs"):
        assert tools[name].annotations.readOnlyHint is True
    forbidden = {"approve", "reject", "shell", "credential", "download_artifact"}
    assert forbidden.isdisjoint(tools)
