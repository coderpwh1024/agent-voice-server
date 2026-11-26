from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.services.memory.mem0_service import AGENT_ID, Mem0Service, MemoryNotFoundError

MEMORY_ID = "00000000-0000-0000-0000-000000000001"
SECOND_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def management():
    memory = MagicMock()
    memory.get.return_value = {
        "id": MEMORY_ID,
        "memory": "tea",
        "user_id": "alice",
        "agent_id": AGENT_ID,
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield Mem0Service(memory, executor), memory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner",
    [None, {"user_id": "bob", "agent_id": AGENT_ID}, {"user_id": "alice", "agent_id": "other"}],
)
@pytest.mark.parametrize("operation", ["get", "update", "delete", "history"])
async def test_single_operations_deny_foreign_and_missing_memories(management, owner, operation):
    service, memory = management
    memory.get.return_value = owner
    memory.db.history_owner.return_value = owner
    with pytest.raises(MemoryNotFoundError):
        match operation:
            case "get":
                await service.get_memory(MEMORY_ID, "alice")
            case "update":
                await service.update_memory(MEMORY_ID, "alice", "coffee")
            case "delete":
                await service.delete_memory(MEMORY_ID, "alice")
            case "history":
                await service.memory_history(MEMORY_ID, "alice")
    memory.update.assert_not_called()
    memory.delete.assert_not_called()
    memory.db.bind_history_owner.assert_not_called()
    memory.db.history_page.assert_not_called()
    memory.db.clear_context.assert_not_called()


@pytest.mark.asyncio
async def test_get_treats_null_metadata_as_empty(management):
    service, memory = management
    memory.get.return_value["metadata"] = None

    record = await service.get_memory(MEMORY_ID, "alice")

    assert record.metadata == {}


@pytest.mark.asyncio
async def test_update_returns_new_text_and_clears_extraction_context(management):
    service, memory = management
    old = memory.get.return_value
    memory.get.side_effect = [old, dict(old, memory="coffee")]
    record = await service.update_memory(MEMORY_ID, "alice", "coffee")
    assert record.memory == "coffee"
    memory.update.assert_called_once_with(memory_id=MEMORY_ID, text="coffee")
    memory.db.bind_history_owner.assert_called_once_with(MEMORY_ID, "alice", AGENT_ID)
    memory.db.clear_context.assert_called_once_with("alice", AGENT_ID)


@pytest.mark.asyncio
async def test_noop_update_does_not_add_history_or_clear_context(management):
    service, memory = management
    assert (await service.update_memory(MEMORY_ID, "alice", "tea")).memory == "tea"
    memory.update.assert_not_called()
    memory.db.bind_history_owner.assert_not_called()
    memory.db.clear_context.assert_not_called()


@pytest.mark.asyncio
async def test_delete_keeps_history_owner_and_clears_context(management):
    service, memory = management
    assert (await service.delete_memory(MEMORY_ID, "alice")).deleted_count == 1
    memory.delete.assert_called_once_with(memory_id=MEMORY_ID)
    memory.db.bind_history_owner.assert_called_once_with(MEMORY_ID, "alice", AGENT_ID)
    memory.db.clear_context.assert_called_once_with("alice", AGENT_ID)


@pytest.mark.asyncio
async def test_bulk_delete_continues_across_batches_and_clears_empty_users(management):
    service, memory = management
    memory.db.list_memories.side_effect = [[{"id": MEMORY_ID}], [{"id": SECOND_ID}], []]
    assert (await service.delete_all_memories("alice")).deleted_count == 2
    assert [call.kwargs["memory_id"] for call in memory.delete.call_args_list] == [
        MEMORY_ID,
        SECOND_ID,
    ]
    assert all(
        call.args == ("alice", AGENT_ID, 100) for call in memory.db.list_memories.call_args_list
    )
    memory.db.list_memories.side_effect = None
    memory.db.list_memories.return_value = []
    assert (await service.delete_all_memories("alice")).deleted_count == 0
    assert memory.db.clear_context.call_count == 2


@pytest.mark.asyncio
async def test_bulk_failure_is_not_reported_as_success(management):
    service, memory = management
    memory.db.list_memories.return_value = [{"id": MEMORY_ID}, {"id": SECOND_ID}]
    memory.delete.side_effect = [None, ConnectionError("unavailable")]
    with pytest.raises(ConnectionError):
        await service.delete_all_memories("alice")
    assert memory.delete.call_count == 2
    assert memory.db.list_memories.call_count == 1


@pytest.mark.asyncio
async def test_listing_returns_ids_metadata_and_cursor(management):
    service, memory = management
    memory.db.list_memories.return_value = [
        {
            "id": memory_id,
            "data": "tea",
            "user_id": "alice",
            "agent_id": AGENT_ID,
            "source_thread_id": "thread",
            "hash": "internal",
        }
        for memory_id in [MEMORY_ID, SECOND_ID]
    ]
    result = await service.list_memories("alice", 1)
    assert [str(record.id) for record in result.memories] == [MEMORY_ID]
    assert result.next_cursor == UUID(MEMORY_ID)
    assert result.memories[0].metadata == {"source_thread_id": "thread"}
    memory.db.list_memories.assert_called_once_with("alice", AGENT_ID, 2, None)


@pytest.mark.asyncio
async def test_deleted_history_is_owned_and_paginated(management):
    service, memory = management
    memory.get.return_value = None
    memory.db.history_owner.return_value = {"user_id": "alice", "agent_id": AGENT_ID}
    memory.db.history_page.return_value = [
        {"id": 1, "memory_id": MEMORY_ID, "event": "ADD", "new_memory": "tea"},
        {"id": 2, "memory_id": MEMORY_ID, "event": "DELETE", "old_memory": "tea"},
    ]
    result = await service.memory_history(MEMORY_ID, "alice", 1)
    assert result.history[0].new_memory == "tea"
    assert result.next_cursor == 1
    memory.db.history_page.assert_called_once_with(MEMORY_ID, 2, None)


@pytest.mark.asyncio
async def test_legacy_deleted_history_without_owner_fails_closed(management):
    service, memory = management
    memory.get.return_value = None
    memory.db.history_owner.return_value = None
    with pytest.raises(MemoryNotFoundError):
        await service.memory_history(MEMORY_ID, "alice")
    memory.db.history_page.assert_not_called()
