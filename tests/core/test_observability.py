from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.core import observability


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_langfuse_uses_one_configured_project(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(observability.settings, "LANGFUSE_TRACING", enabled)
    monkeypatch.setattr(observability.settings, "LANGFUSE_HOST", "http://127.0.0.1:3000/")
    monkeypatch.setattr(observability.settings, "LANGFUSE_PUBLIC_KEY", SecretStr("example-public"))
    monkeypatch.setattr(observability.settings, "LANGFUSE_SECRET_KEY", SecretStr("example-secret"))
    client = Mock()
    client.auth_check.return_value = True
    callback = Mock()
    initialize_client = Mock(return_value=client)
    get_client = Mock(return_value=client)
    create_callback = Mock(return_value=callback)
    monkeypatch.setattr(observability, "Langfuse", initialize_client)
    monkeypatch.setattr(observability, "get_client", get_client)
    monkeypatch.setattr(observability, "CallbackHandler", create_callback)

    async with observability.langfuse_lifespan():
        assert observability.langfuse_callbacks() == ([callback] if enabled else [])
        assert await observability.langfuse_connected() is enabled

    if enabled:
        initialize_client.assert_called_once_with(
            public_key="example-public",
            secret_key="example-secret",
            base_url="http://127.0.0.1:3000/",
        )
        create_callback.assert_called_once_with(public_key="example-public")
        get_client.assert_called_once_with(public_key="example-public")
        client.flush.assert_called_once_with()
    else:
        initialize_client.assert_not_called()
        create_callback.assert_not_called()
        get_client.assert_not_called()
