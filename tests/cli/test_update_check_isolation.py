"""Routine CLI calls must not start background update checks in the test suite."""

from siftd.cli import main


def test_main_does_not_start_update_check(monkeypatch, capsys):
    # Trap thread creation rather than using the network, even if the suite
    # guard regresses. Fresh sandbox config/state otherwise enables the check.
    attempted = []

    class RecordingThread:
        def __init__(self, *, target, daemon):
            attempted.append((target, daemon))

        def start(self):
            attempted.append("started")

    monkeypatch.setattr("siftd.cli.upgrade.threading.Thread", RecordingThread)
    assert main(["config", "path"]) == 0
    assert "config.toml" in capsys.readouterr().out
    assert attempted == [], "ordinary main() tried to launch a live update check"
