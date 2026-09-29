"""Unit coverage for the opt-in Browserless T3 transport and containment."""

import asyncio
import builtins
import json
import subprocess
from pathlib import Path

import pytest
from browser_smoke import remote, smoke


def test_remote_config_requires_both_explicit_values():
    with pytest.raises(remote.RemoteConfigurationError, match="ENDPOINT"):
        remote.RemoteConfig.from_environment({})
    with pytest.raises(remote.RemoteConfigurationError, match="SSH_TARGET"):
        remote.RemoteConfig.from_environment({"SIFTD_BROWSER_SMOKE_ENDPOINT": "wss://browser.test/ws"})


def test_remote_config_rejects_insecure_or_unsafe_destinations():
    with pytest.raises(remote.RemoteConfigurationError, match="secure wss"):
        remote.RemoteConfig.from_environment({
            "SIFTD_BROWSER_SMOKE_ENDPOINT": "ws://browser.test/ws?token=secret",
            "SIFTD_BROWSER_SMOKE_SSH_TARGET": "tester@host.test",
        })
    with pytest.raises(remote.RemoteConfigurationError, match="user@host"):
        remote.RemoteConfig.from_environment({
            "SIFTD_BROWSER_SMOKE_ENDPOINT": "wss://browser.test/ws",
            "SIFTD_BROWSER_SMOKE_SSH_TARGET": "-oProxyCommand=bad",
        })
    for endpoint in ("wss://user:secret@browser.test/ws", "wss://browser.test/ws#fragment"):
        with pytest.raises(remote.RemoteConfigurationError):
            remote.RemoteConfig.from_environment({
                "SIFTD_BROWSER_SMOKE_ENDPOINT": endpoint,
                "SIFTD_BROWSER_SMOKE_SSH_TARGET": "tester@host.test",
            })
    with pytest.raises(remote.RemoteConfigurationError, match="user@host"):
        remote.RemoteConfig.from_environment({
            "SIFTD_BROWSER_SMOKE_ENDPOINT": "wss://browser.test/ws",
            "SIFTD_BROWSER_SMOKE_SSH_TARGET": "-Efile@test.host",
        })


def test_endpoint_credentials_are_redacted_from_errors_and_receipts(tmp_path: Path):
    endpoint = "wss://browser.test/ws?token=secret"
    redacted = remote.redact_endpoint(endpoint)
    assert "secret" not in redacted
    assert redacted == "wss://browser.test/ws?<redacted>"
    assert "secret" not in remote.redact_text(f"could not connect {endpoint}", endpoint)

    smoke._write_receipt(tmp_path, {"endpoint": redacted, "error": "connection failed"})
    assert "secret" not in (tmp_path / "receipt.json").read_text()


def test_ssh_argv_is_private_and_disables_keychain_agent_forwarding():
    argv = remote.ssh_argv("tester@host.test", "-R", "127.0.0.1:0:127.0.0.1:43210")
    assert argv[:3] == ["ssh", "-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in argv
    assert "UseKeychain=no" in argv
    assert "AddKeysToAgent=no" in argv
    assert "ForwardAgent=no" in argv
    assert argv[-4:] == ["-R", "127.0.0.1:0:127.0.0.1:43210", "--", "tester@host.test"]
    assert "-L" not in argv


def test_local_chromium_argv_uses_an_isolated_mock_keychain(tmp_path: Path):
    argv = smoke.local_chromium_argv("chromium", tmp_path)
    assert "--use-mock-keychain" in argv
    assert f"--user-data-dir={tmp_path / 'profile'}" in argv
    assert "--no-first-run" in argv


def test_fixture_server_is_explicitly_private_and_never_uses_default_db(tmp_path: Path, monkeypatch):
    calls = []

    class FakeProcess:
        pass

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(smoke, "build_fixture", lambda path: "fixture-conversation")
    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    server, log, _ = smoke.start_fixture(tmp_path, 43210)
    log.close()
    assert isinstance(server, FakeProcess)
    argv, _ = calls[0]
    assert argv[1:3] == ["--db", str(tmp_path / "fixture.db")]
    assert argv[-6:] == ["serve", "--host", "127.0.0.1", "--port", "43210", "--no-auth"]


def test_forward_cleanup_terminates_owned_process_and_requires_listener_absence(monkeypatch):
    config = remote.RemoteConfig("wss://browser.test/ws", "tester@host.test")
    forward = remote.SSHReverseForward(config, 43210)

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            return -15

    process = FakeProcess()
    forward.process = process
    forward.remote_port = 54321
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(remote.subprocess, "run", fake_run)
    assert forward.close() is True
    assert process.terminated is True
    assert "-iTCP:54321" in calls[0][-1]


def test_forward_start_parses_allocated_port_and_verifies_loopback(monkeypatch):
    config = remote.RemoteConfig("wss://browser.test/ws", "tester@host.test")
    forward = remote.SSHReverseForward(config, 43210)

    class FakeProcess:
        def __init__(self):
            from io import StringIO

            self.stderr = StringIO("Allocated port 54321 for remote forward\n")

        def poll(self):
            return None

    captured = []
    monkeypatch.setattr(remote.subprocess, "Popen", lambda argv, **kwargs: captured.append(argv) or FakeProcess())
    monkeypatch.setattr(forward, "_listener_is_loopback", lambda: True)
    assert forward.start() == 54321
    assert captured[0][-2:] == ["--", "tester@host.test"]
    assert "127.0.0.1:0:127.0.0.1:43210" in captured[0]


def test_loopback_verification_rejects_wildcard_listener(monkeypatch):
    forward = remote.SSHReverseForward(
        remote.RemoteConfig("wss://browser.test/ws", "tester@host.test"), 43210
    )
    forward.remote_port = 54321
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "p12\nn*:54321\n", ""),
    )
    assert forward._listener_is_loopback() is False


def test_artifact_directory_refuses_to_overwrite_prior_fixture_evidence(tmp_path: Path):
    (tmp_path / "receipt.json").write_text("prior receipt")
    with pytest.raises(remote.RemoteConfigurationError, match="not empty"):
        smoke.prepare_artifacts(tmp_path)


def test_remote_mode_refuses_cleanly_when_optional_client_is_unavailable(monkeypatch, capsys, tmp_path: Path):
    original_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    config = remote.RemoteConfig("wss://browser.test/ws?token=secret", "tester@host.test")
    assert asyncio.run(smoke.run_remote(tmp_path, config, None)) == 2
    output = capsys.readouterr().out
    assert "optional browser test dependency" in output
    assert "secret" not in output


def test_session_wire_passes_page_cdp_events_and_errors_without_playwright():
    class Session:
        def __init__(self):
            self.handlers = {}

        def on(self, method, handler):
            self.handlers[method] = handler

        async def send(self, method, params):
            if method == "Bad.command":
                raise RuntimeError("expected failure")
            return {"method": method, "params": params}

    async def exercise():
        session = Session()
        wire = smoke.SessionWire(session)
        session.handlers["Log.entryAdded"]({"entry": {"source": "security", "text": "event"}})
        event = json.loads(await wire.recv())
        assert event["method"] == "Log.entryAdded"
        await wire.send(json.dumps({"id": 7, "method": "Bad.command"}))
        reply = json.loads(await wire.recv())
        assert reply["id"] == 7
        assert "expected failure" in reply["error"]["message"]

    asyncio.run(exercise())
