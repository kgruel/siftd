"""Fake SSH connection for testing.

Adapted from hlab's FakeSSH with siftd-specific factory classmethods.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


class FakeStdout:
    """Fake async iterator for process stdout lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._index = 0

    def __aiter__(self) -> "FakeStdout":
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._index]
        self._index += 1
        return line


class FakeProcess:
    """Fake SSH process for streaming output."""

    def __init__(self, stdout_lines: list[str]) -> None:
        self.stdout = FakeStdout(stdout_lines)


@dataclass
class FakeSSHResult:
    """Fake SSH command result."""

    stdout: str | bytes = ""
    stderr: str | bytes = ""
    returncode: int = 0

    def as_binary(self) -> "FakeSSHResult":
        """Return a copy with stdout/stderr as bytes."""
        return FakeSSHResult(
            stdout=self.stdout.encode() if isinstance(self.stdout, str) else self.stdout,
            stderr=self.stderr.encode() if isinstance(self.stderr, str) else self.stderr,
            returncode=self.returncode,
        )


class FakeSSH:
    """Fake SSH connection that returns predefined responses by command pattern.

    Direct usage:
        fake = FakeSSH({"siftd": FakeSSHResult(stdout='{"status":"ok"}')})
        with patch("asyncssh.connect", AsyncMock(return_value=fake)):
            ...

    Dynamic responses (for stateful tests):
        def make_response(cmd, **kwargs):
            return FakeSSHResult(stdout='{"status":"ok"}')
        fake = FakeSSH(response_func=make_response)
    """

    def __init__(
        self,
        responses: dict[str, FakeSSHResult] | None = None,
        response_func: Callable[..., FakeSSHResult] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.response_func = response_func
        self.commands_run: list[str] = []
        self.inputs_received: list[bytes | str | None] = []
        self.connect_kwargs: dict[str, Any] = {}

    async def run(
        self,
        cmd: str,
        *,
        input: bytes | str | None = None,  # noqa: A002
        encoding: str | None = "utf-8",
        timeout: float | None = None,
    ) -> FakeSSHResult:
        """Run a command and return matching response."""
        self.commands_run.append(cmd)
        self.inputs_received.append(input)

        # Dynamic response function takes precedence
        if self.response_func is not None:
            result = self.response_func(cmd, input=input, timeout=timeout)
        else:
            result = FakeSSHResult(stderr="no match", returncode=1)
            for pattern, r in self.responses.items():
                if pattern in cmd:
                    result = r
                    break

        return result.as_binary() if encoding is None else result

    async def __aenter__(self) -> "FakeSSH":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def close(self) -> None:
        """Close the fake connection (no-op for fake)."""

    @asynccontextmanager
    async def create_process(self, cmd: str) -> AsyncIterator[FakeProcess]:
        """Create a fake process for streaming output."""
        self.commands_run.append(cmd)
        for pattern, result in self.responses.items():
            if pattern in cmd:
                lines = result.stdout.split("\n") if result.stdout else []
                yield FakeProcess(lines)
                return
        yield FakeProcess([])

    # -- siftd-specific factories --

    @classmethod
    def push_success(cls) -> "FakeSSH":
        """Factory for successful push — responds to siftd commands with success."""
        return cls({
            "siftd": FakeSSHResult(stdout='{"status":"ok","conversations":3}'),
        })

    @classmethod
    def pull_success(cls, data: bytes) -> "FakeSSH":
        """Factory for successful pull — responds with binary data on stdout.

        The stderr carries the metadata JSON that ``siftd db send`` emits.
        """
        def respond(cmd: str, **kwargs: Any) -> FakeSSHResult:
            if "siftd" in cmd:
                return FakeSSHResult(
                    stdout=data.decode("latin-1"),
                    stderr='{"conversations":1}',
                    returncode=0,
                )
            return FakeSSHResult(stderr="no match", returncode=1)

        return cls(response_func=respond)

    @classmethod
    def connection_refused(cls) -> "FakeSSH":
        """Factory for connection error."""
        return cls({
            "siftd": FakeSSHResult(stderr="Connection refused", returncode=1),
        })
