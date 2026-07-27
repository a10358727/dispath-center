from scripts.node_primitives_smoke import main


def test_node_protocol_primitives_import_without_claiming_runnable_daemon(capsys):
    assert main() == 0
    output = capsys.readouterr().out
    assert "Node primitives smoke: PASS" in output
    assert "protocol-primitives-only" in output
    assert "daemon=absent" in output
