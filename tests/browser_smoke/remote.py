"""Private, opt-in Browserless transport for the T3 smoke fixture.

This module deliberately lives beside the standalone smoke, not in product code:
it is developer-test plumbing and has no siftd CLI/API surface.
"""

from __future__ import annotations

import queue
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class RemoteConfigurationError(ValueError):
    """The explicit remote-smoke configuration is absent or unsafe."""


SSH_OPTIONS = (
    "-F", "/dev/null",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "UseKeychain=no",
    "-o", "AddKeysToAgent=no",
    "-o", "ForwardAgent=no",
    "-o", "ConnectTimeout=10",
)
_TARGET = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.:-]*$")
_ALLOCATED_PORT = re.compile(r"Allocated port (\d+) for remote forward")


def redact_endpoint(value: str) -> str:
    """Keep an endpoint useful for diagnosis without retaining credentials."""
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = host if port is None else f"{host}:{port}"
    if parsed.username:
        authority = f"<redacted>@{authority}"
    return urlunsplit((parsed.scheme, authority, parsed.path, "<redacted>" if parsed.query else "", ""))


def redact_text(value: str, endpoint: str) -> str:
    """Remove the supplied endpoint and its query from third-party exceptions."""
    return value.replace(endpoint, redact_endpoint(endpoint))


@dataclass(frozen=True)
class RemoteConfig:
    """Narrow, caller-supplied endpoint and SSH destination for one smoke run."""

    endpoint: str
    ssh_target: str

    @classmethod
    def from_environment(cls, environ: dict[str, str]) -> RemoteConfig:
        endpoint = environ.get("SIFTD_BROWSER_SMOKE_ENDPOINT", "")
        target = environ.get("SIFTD_BROWSER_SMOKE_SSH_TARGET", "")
        if not endpoint:
            raise RemoteConfigurationError("remote mode requires SIFTD_BROWSER_SMOKE_ENDPOINT")
        if not target:
            raise RemoteConfigurationError("remote mode requires SIFTD_BROWSER_SMOKE_SSH_TARGET")
        parsed = urlsplit(endpoint)
        try:
            parsed.port
            invalid_port = False
        except ValueError:
            invalid_port = True
        if parsed.scheme != "wss" or not parsed.hostname or parsed.fragment or invalid_port:
            raise RemoteConfigurationError(
                "SIFTD_BROWSER_SMOKE_ENDPOINT must be a secure wss:// URL "
                f"(got {redact_endpoint(endpoint)})"
            )
        if parsed.username or parsed.password:
            raise RemoteConfigurationError(
                "SIFTD_BROWSER_SMOKE_ENDPOINT credentials belong in its query, not userinfo "
                f"(got {redact_endpoint(endpoint)})"
            )
        if not _TARGET.fullmatch(target):
            raise RemoteConfigurationError("SIFTD_BROWSER_SMOKE_SSH_TARGET must be user@host")
        return cls(endpoint=endpoint, ssh_target=target)


def reserve_loopback_port() -> int:
    """Reserve a local loopback port until the fixture server is started."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def ssh_argv(target: str, *options: str, command: str | None = None) -> list[str]:
    """Build fixed SSH options, keeping a remote command after its destination."""
    argv = ["ssh", *SSH_OPTIONS, *options, "--", target]
    if command is not None:
        argv.append(command)
    return argv


class SSHReverseForward:
    """One verified loopback-only reverse forward owned by a remote smoke run."""

    def __init__(self, config: RemoteConfig, local_port: int) -> None:
        self.config = config
        self.local_port = local_port
        self.process: subprocess.Popen[str] | None = None
        self.remote_port: int | None = None
        self.lines: list[str] = []
        self._messages: queue.Queue[str] = queue.Queue()
        self._reader: threading.Thread | None = None

    def _collect_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self.lines.append(line)
            self._messages.put(line)

    def start(self) -> int:
        argv = ssh_argv(
            self.config.ssh_target,
            "-v", "-N", "-T", "-o", "ExitOnForwardFailure=yes",
            "-R", f"127.0.0.1:0:127.0.0.1:{self.local_port}",
        )
        self.process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True,
        )
        self._reader = threading.Thread(target=self._collect_stderr, daemon=True)
        self._reader.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("SSH reverse forward exited during setup")
            try:
                line = self._messages.get(timeout=0.25)
            except queue.Empty:
                continue
            match = _ALLOCATED_PORT.search(line)
            if match:
                self.remote_port = int(match.group(1))
                break
        if self.remote_port is None:
            raise RuntimeError("SSH did not report an allocated remote loopback port")
        if not self._listener_is_loopback():
            raise RuntimeError("SSH remote forward was not verified as loopback-only")
        return self.remote_port

    def _listener_check(self) -> subprocess.CompletedProcess[str]:
        assert self.remote_port is not None
        return subprocess.run(
            ssh_argv(
                self.config.ssh_target,
                command=f"/usr/sbin/lsof -nP -iTCP:{self.remote_port} -sTCP:LISTEN -F pn",
            ),
            capture_output=True, text=True, check=False,
        )

    def _listener_is_loopback(self) -> bool:
        check = self._listener_check()
        bindings = [line[1:] for line in check.stdout.splitlines() if line.startswith("n")]
        return check.returncode == 0 and bindings == [f"127.0.0.1:{self.remote_port}"]

    def close(self) -> bool:
        """Stop the owned SSH process and prove its remote listener disappeared."""
        if self.process is None:
            return True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self.remote_port is None:
            return True
        check = self._listener_check()
        return check.returncode == 1 and not check.stdout.strip()
