from probe.app.cli import main


def test_probe_cli_requires_once_for_runtime(capsys) -> None:
    try:
        main([])
    except SystemExit as exc:
        exit_code = exc.code
    else:
        exit_code = 0

    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Only --once mode is implemented" in captured.err


def test_probe_agent_version(capsys) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PING probe agent 0.1.0" in captured.out
