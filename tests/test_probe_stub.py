from probe.app.cli import main


def test_probe_stub_runs_without_network(capsys) -> None:
    exit_code = main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "runtime logic is not implemented yet" in captured.out


def test_probe_stub_version(capsys) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PING probe stub 0.1.0" in captured.out
