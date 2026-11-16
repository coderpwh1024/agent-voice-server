"""Block socket connections while running the curated contract suite."""

import socket
from typing import NoReturn

import pytest

_PATCHES = pytest.StashKey[pytest.MonkeyPatch]()


def _block_network(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("Network access is disabled in Agent contract evaluations")


def pytest_configure(config: pytest.Config) -> None:
    patches = pytest.MonkeyPatch()
    for target in ("connect", "connect_ex"):
        patches.setattr(socket.socket, target, _block_network)
    patches.setattr(socket, "getaddrinfo", _block_network)
    for name in (
        "LANGFUSE_TRACING",
        "LANGFUSE_TRACING_ENABLED",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
    ):
        patches.setenv(name, "false")
    patches.setenv("OTEL_SDK_DISABLED", "true")
    config.stash[_PATCHES] = patches


def pytest_unconfigure(config: pytest.Config) -> None:
    if _PATCHES in config.stash:
        config.stash[_PATCHES].undo()
