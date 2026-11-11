from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr

import app.services.memory.local_extraction as extraction
from app.core import settings
from app.schemas.local_memory import LocalMemoryContext, LocalMemoryExtractionInput
from app.services.auth import create_access_token


@pytest.fixture
def local_api(monkeypatch, test_client):
    monkeypatch.setattr(settings, "AUTH_SECRET", SecretStr("service-" + "s" * 32))
    monkeypatch.setattr(settings, "APP_TOKEN_SECRET", SecretStr("a" * 48))
    monkeypatch.setattr(settings, "LOCAL_MEMORY_ENABLED", True)
    chain = Mock(ainvoke=AsyncMock(return_value={"proposals": []}))
    model = Mock()
    model.with_structured_output.return_value.with_config.return_value = chain
    monkeypatch.setattr(extraction, "get_model", lambda *args, **kwargs: model)
    token, _ = create_access_token("alice")
    return test_client, chain, {"Authorization": f"Bearer {token}"}


def body(**kwargs):
    return {"source_id": str(uuid4()), "text": "我喜欢简短的回答", **kwargs}


def test_local_proposals_do_not_claim_device_write_and_do_not_log_content(local_api, caplog):
    client, chain, auth = local_api
    caplog.set_level("INFO")
    chain.ainvoke.return_value = {
        "proposals": [
            {
                "action": "add",
                "category": "profile",
                "text": "偏好简短回答",
                "evidence": "喜欢简短的回答",
                "requires_confirmation": False,
            }
        ]
    }
    request = body()
    response = client.post("/local-memories/extract", json=request, headers=auth)
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["source_id"] == request["source_id"]
    assert result["status"] == "proposed"
    assert len(result["proposals"]) == 1
    assert "extraction completed" in caplog.text
    assert request["text"] not in caplog.text
    assert "偏好简短回答" not in caplog.text


@pytest.mark.parametrize("headers,user,status", [({}, None, 401), (None, "bob", 403)])
def test_local_extraction_requires_bound_user(local_api, headers, user, status):
    client, chain, auth = local_api
    response = client.post(
        "/local-memories/extract",
        json=body(),
        headers=auth if headers is None else headers,
        params={"user_id": user} if user else {},
    )
    assert response.status_code == status
    chain.ainvoke.assert_not_awaited()


@pytest.mark.parametrize(
    "text",
    [
        "这条不要记，我喜欢咖啡",
        "我的密码是 secret-for-test",
        "don't remember this",
        "Do Not Remember this",
    ],
)
def test_private_or_credential_input_is_not_sent_to_extraction_model(local_api, text):
    client, chain, auth = local_api
    response = client.post("/local-memories/extract", json=body(text=text), headers=auth)
    assert response.json()["data"]["status"] == "no_change"
    chain.ainvoke.assert_not_awaited()


@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("private provider payload")])
def test_extraction_failure_is_actionable_without_exposing_content(local_api, failure):
    client, chain, auth = local_api
    chain.ainvoke.side_effect = failure
    response = client.post("/local-memories/extract", json=body(), headers=auth)
    assert response.status_code == (504 if isinstance(failure, TimeoutError) else 503)
    assert "private provider payload" not in response.text
    assert "no device files were changed" in response.text


def test_fabricated_evidence_or_target_is_rejected(local_api):
    client, chain, auth = local_api
    for proposal in [
        {"action": "add", "category": "profile", "text": "外推", "evidence": "不存在的原文"},
        {"action": "delete", "category": "profile", "target_id": str(uuid4()), "evidence": "我"},
    ]:
        chain.ainvoke.return_value = {"proposals": [proposal]}
        assert client.post("/local-memories/extract", json=body(), headers=auth).status_code == 503


def test_mutation_proposal_always_requires_device_confirmation(local_api):
    client, chain, auth = local_api
    target = str(uuid4())
    chain.ainvoke.return_value = {
        "proposals": [
            {
                "action": "delete",
                "category": "profile",
                "target_id": target,
                "evidence": "删除",
                "requires_confirmation": False,
            }
        ]
    }
    response = client.post(
        "/local-memories/extract",
        headers=auth,
        json=body(
            text="删除我的记忆",
            context={"memories": [{"id": target, "category": "profile", "text": "旧偏好"}]},
        ),
    )
    assert response.json()["data"]["proposals"][0]["requires_confirmation"] is True


def test_disabled_device_memory_does_not_fall_back_to_mem0(local_api, monkeypatch):
    client, chain, auth = local_api
    monkeypatch.setattr(settings, "LOCAL_MEMORY_ENABLED", False)
    assert client.post("/local-memories/extract", json=body(), headers=auth).status_code == 503
    chain.ainvoke.assert_not_awaited()


def test_original_time_anchor_is_preserved_for_delayed_extraction(local_api):
    client, chain, auth = local_api
    response = client.post(
        "/local-memories/extract",
        headers=auth,
        json=body(occurred_at="2026-11-10T15:00:00Z", utc_offset_minutes=480),
    )
    assert response.status_code == 200
    messages = chain.ainvoke.await_args.args[0]
    assert '"occurred_at": "2026-11-10T15:00:00Z"' in messages[-1].content
    assert '"utc_offset_minutes": 480' in messages[-1].content
    response = client.post(
        "/local-memories/extract", headers=auth, json=body(occurred_at="2026-11-10T15:00:00")
    )
    assert response.status_code == 422


def test_context_budget_rejects_duplicates_and_extra_identity():
    from pydantic import ValidationError

    item = {"id": str(uuid4()), "category": "profile", "text": "内容"}
    for data in [
        {"memories": [item, item]},
        {"user_id": "bob"},
        {"memories": [{**item, "id": str(uuid4()), "text": "长" * 1000} for _ in range(4)]},
    ]:
        with pytest.raises(ValidationError):
            LocalMemoryContext.model_validate(data)
    with pytest.raises(ValidationError):
        LocalMemoryExtractionInput.model_validate(body(text="x" * 6001))
