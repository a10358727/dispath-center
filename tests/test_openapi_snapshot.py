"""The HTTP surface is pinned as a readable JSON snapshot (整頓 C2b).

A route/model change fails here with a line diff and the update command,
instead of an opaque sha256 mismatch.
"""

from scripts.openapi_snapshot import build_snapshot, diff_lines, load_snapshot


def test_openapi_surface_matches_the_reviewed_snapshot():
    expected = load_snapshot()
    actual = build_snapshot()
    lines = diff_lines(expected, actual)
    assert not lines, (
        "OpenAPI surface drifted from tests/openapi_snapshot.json — review the diff, "
        "then run `python scripts/openapi_snapshot.py --update`:\n" + "\n".join(lines)
    )


def test_snapshot_is_non_trivial_and_sorted():
    snapshot = load_snapshot()
    operations = snapshot["operations"]
    assert len(operations) > 200
    assert list(operations) == sorted(operations)
    assert snapshot["schemas"] == sorted(snapshot["schemas"])
