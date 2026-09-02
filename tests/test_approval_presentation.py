"""整頓 U2: every approval kind renders a human title; summaries never raise."""

import pytest

from app.approval_presentation import KIND_TITLES, describe_approval
from app.db import VALID_APPROVAL_KINDS


def test_every_valid_kind_has_a_human_title_not_the_raw_kind():
    missing = sorted(VALID_APPROVAL_KINDS - set(KIND_TITLES))
    assert missing == [], f"kinds without a title: {missing}"
    for kind in VALID_APPROVAL_KINDS:
        title = describe_approval(kind, {})["title"]
        assert title and title != kind


@pytest.mark.parametrize("payload", [None, {}, [], "x", 7, {"nested": {"deep": object()}}])
def test_describe_never_raises_for_odd_payloads(payload):
    for kind in sorted(VALID_APPROVAL_KINDS):
        result = describe_approval(kind, payload)
        assert set(result) == {"title", "summary"}
        assert isinstance(result["summary"], str)


def test_happy_path_summaries_show_the_reviewable_fields():
    enqueue = describe_approval(
        "enqueue", {"project": "demo", "command": "python train.py", "pin_server": "server-a"}
    )
    assert "demo" in enqueue["summary"]
    assert "python train.py" in enqueue["summary"]
    assert "server-a" in enqueue["summary"]

    promote = describe_approval(
        "engineering_task_promote", {"project_name": "demo", "git_commit": "a" * 40}
    )
    assert "demo" in promote["summary"]
    assert "aaaa" in promote["summary"]
    assert "a" * 40 not in promote["summary"]  # short commit, not the full hash

    experiment = describe_approval("experiment_create_v2", {"run_count": 4})
    assert "4 個 run" in experiment["summary"]


def test_summaries_redact_absolute_paths():
    # The v2 list is a safe surface: no filesystem path may reach a summary.
    for kind, payload in [
        ("import_project", {"name": "demo", "server": "srv", "path": "/srv/projects/demo"}),
        ("inventory_scan", {"server": "srv", "project_roots": ["/srv/projects"]}),
        ("enqueue", {"project": "demo", "command": "python train.py --data /srv/data/x"}),
        ("dataset_publish_v2", {"dataset": "d", "source_path": "/srv/projects/out"}),
        ("git_init", {"path": "/srv/projects/demo"}),
    ]:
        summary = describe_approval(kind, payload)["summary"]
        assert "/srv" not in summary, (kind, summary)
    assert "python train.py" in describe_approval("enqueue", {"command": "python train.py --data /srv/x"})["summary"]


def test_generic_fallback_never_leaks_server_yaml_fields():
    summary = describe_approval(
        "server_update",
        {"yaml_after_utf8_b64": "QUJD", "server_name": "worker-1"},
    )["summary"]
    assert "QUJD" not in summary
    assert "worker-1" in summary
