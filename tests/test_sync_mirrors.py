"""`scripts/sync_mirrors.py` (整頓 C9): the two wheel-boundary mirrors stay in sync."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_mirrors  # noqa: E402


def test_repository_mirrors_are_in_sync():
    assert sync_mirrors.drift() == []
    assert sync_mirrors.main(["--check"]) == 0


def _copy_pairs(tmp_path: Path) -> Path:
    for canonical, mirror in sync_mirrors.MIRROR_PAIRS:
        for rel in (canonical, mirror):
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, tmp_path / rel)
    return tmp_path


def test_write_restores_a_drifted_mirror(tmp_path):
    root = _copy_pairs(tmp_path)
    canonical, mirror = sync_mirrors.MIRROR_PAIRS[0]
    (root / mirror).write_text("# drifted\n", encoding="utf-8")
    assert sync_mirrors.drift(root) == [(canonical, mirror)]
    assert sync_mirrors.write(root) == [(canonical, mirror)]
    assert (root / mirror).read_bytes() == (root / canonical).read_bytes()
    assert sync_mirrors.drift(root) == []


def test_write_never_touches_the_canonical_side(tmp_path):
    root = _copy_pairs(tmp_path)
    canonical, mirror = sync_mirrors.MIRROR_PAIRS[1]
    before = (root / canonical).read_bytes()
    (root / mirror).write_text("# drifted\n", encoding="utf-8")
    sync_mirrors.write(root)
    assert (root / canonical).read_bytes() == before
