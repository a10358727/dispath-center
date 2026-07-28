from scripts.node_primitives_smoke import main


def test_node_daemon_is_runnable_outbound_only_and_still_gated(capsys):
    """Phase 0 pinned "no daemon exists". Phase 4 supplied one, so the boundary
    moves rather than disappears: the daemon must exist, import no
    control-plane module, open no listener, and still be unable to take work
    until DG-NODE-V2 is ruled on."""
    assert main() == 0
    output = capsys.readouterr().out
    assert "Node primitives smoke: PASS" in output
    assert "daemon=runnable" in output
    assert "outbound-only" in output
    assert "DG-NODE-V2" in output
