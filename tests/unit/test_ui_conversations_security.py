"""Conversation visibility uses committed scope, never role-wide access."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import TYPE_CHECKING

import pytest
from memory_server.tools.schemas import MEMORY_TOOLS_SCHEMA_LIST
from src.adapters.schemas import OpenAIChatRequest
from src.middleware.layers.tool import ToolLayer
from src.models.context import PipelineContext
from src.models.identity import AgentIdentity
from src.models.session_persistence import IncognitoStreamAdmission, PersistedStreamAdmission
from src.services import dispatcher
from src.services.tool_executor import ToolExecutor
from src.tools.builtin import memory as gateway
from src.tools.contracts import validate_native_arguments
from src.utils import async_db
from src.utils.session_scope import channel_session_id

from tests.support.conversation_search import conversation_db as conversation_db
from tests.support.conversation_search import denied, search, success, tree, unknown_count
from tests.support.session_scope import DEBUG, EMPTY, HUMAN, A, B, raw_row, seed, ui_save
from tests.support.session_scope import scope_db as scope_db

if TYPE_CHECKING:
    from tests.support.conversation_search import ConversationDB


@pytest.mark.parametrize("actor", [A, AgentIdentity.for_role(A.agent_id, "admin"), DEBUG])
async def test_exact_binding_precedes_role_owner_model_and_paging(
    conversation_db: ConversationDB, actor: AgentIdentity
) -> None:
    await conversation_db.ui("mine", "allowed", identity=actor)
    await conversation_db.ui("foreign", "forbidden", identity=B, timestamp=None)
    first = await search(actor, source="ui", page_size=1, model="@ScopeB")
    past = await search(actor, source="ui", page_size=1, offset=1)
    dated = await search(actor, source="ui", date_start="2026-05-13")
    success(first, 1)
    success(past, 1)
    success(dated, 1)
    unknown_count(dated, 0)
    assert "allowed" in first and "forbidden" not in first + past + dated
    assert "historical_data" not in past and "下一頁" not in first + past


@pytest.mark.parametrize("owner", ["scope-human", "other-human", "scope-admin", None])
async def test_same_soul_is_visible_regardless_of_human_creator(
    conversation_db: ConversationDB, owner: str | None
) -> None:
    await conversation_db.ui("mine", "allowed", owner=owner)
    await conversation_db.ui("other", "forbidden", identity=B, owner=owner)
    result = await search(source="ui")
    success(result, 1)
    assert "allowed" in result and "forbidden" not in result


@pytest.mark.parametrize(
    "actor",
    [
        HUMAN,
        AgentIdentity.for_role(A.agent_id, "admin", is_human=True),
        AgentIdentity.for_role("plain-model", "Base"),
        AgentIdentity.for_role("ToukaGroup", "Owner"),
        AgentIdentity.for_role("DispatchAgent", "Owner"),
        AgentIdentity.for_role("child", "Subagent"),
        AgentIdentity.for_role("unknown", "Debugger"),
        AgentIdentity.for_role("ToukaSoulDebugAgent", "Debugger"),
    ],
)
async def test_non_soul_actor_cannot_expand_owned_agents(
    conversation_db: ConversationDB, actor: AgentIdentity
) -> None:
    await conversation_db.ui("mine", "allowed")
    denied(await search(actor, source="ui"))
    allowed = await search(source="ui")
    success(allowed, 1)
    assert "allowed" in allowed


@pytest.mark.parametrize("alias_side", ["row", "identity"])
async def test_canonical_alias_on_either_side_preserves_own_scope(
    conversation_db: ConversationDB, alias_side: str
) -> None:
    alias = AgentIdentity.for_role(f"ToukaSoul{A.agent_id}", "Owner")
    await conversation_db.ui("mine", "allowed", identity=alias if alias_side == "row" else A)
    await conversation_db.ui("other", "forbidden", identity=B)
    result = await search(alias if alias_side == "identity" else A, source="ui")
    success(result, 1)
    assert "allowed" in result and "forbidden" not in result


@pytest.mark.parametrize("marker", [0, 2, -1, "not-a-version", 1.5])
async def test_only_exact_marker_one_is_visible(
    conversation_db: ConversationDB, marker: object
) -> None:
    await conversation_db.ui("valid", "allowed")
    await conversation_db.ui("old", "forbidden", marker=marker, timestamp=None)
    result = await search(source="ui", page_size=1)
    dated = await search(source="ui", date_start="2026-05-13")
    success(result, 1)
    success(dated, 1)
    unknown_count(dated, 0)
    assert "allowed" in result and "forbidden" not in result + dated
    assert "下一頁" not in result


@pytest.mark.parametrize("initial", ["nonempty", "side-branch", "summary", "old-empty", "empty"])
async def test_rebinding_never_restores_lost_provenance(
    conversation_db: ConversationDB, initial: str
) -> None:
    messages = tree("forbidden") if initial in {"nonempty", "side-branch"} else copy.deepcopy(EMPTY)
    if initial == "side-branch":
        messages["currentId"] = None
    await seed(tree=messages, context_summary="summary" if initial == "summary" else None)
    if initial == "old-empty":
        await conversation_db.execute(
            "UPDATE user_sessions SET session_scope_version = 0 WHERE id = ?", ("chat",)
        )
    assert (await ui_save(agent_id=B)).persistence.status == "saved"
    # 換回 A 與寫入內容分兩次保存：同一次保存帶入內容的換綁依 ADR-0276 一律降標記。
    assert (await ui_save(agent_id=A)).persistence.status == "saved"
    assert (
        await ui_save(messages=tree("candidate"), context_summary=None)
    ).persistence.status == "saved"
    await conversation_db.ui("control", "allowed")
    result = await search(source="ui")
    expected = 2 if initial == "empty" else 1
    success(result, expected)
    assert "allowed" in result
    assert ("candidate" in result) == (initial == "empty")
    assert (await raw_row())["session_scope_version"] == (1 if initial == "empty" else 0)


@pytest.mark.parametrize("retired", ["pulse_channel_default", "sns_shared", "sns_"])
async def test_retired_channels_hidden_while_new_scoped_channels_are_visible(
    conversation_db: ConversationDB, retired: str
) -> None:
    await conversation_db.ui(retired, "forbidden")
    for kind in ("heartbeat", "sns"):
        await conversation_db.ui(channel_session_id(kind, A.agent_id), f"allowed-{kind}")
        await conversation_db.ui(channel_session_id(kind, B.agent_id), "foreign", identity=B)
    await conversation_db.ui("independent", "allowed-independent")
    result = await search(source="ui")
    success(result, 3)
    assert all(f"allowed-{kind}" in result for kind in ("heartbeat", "sns", "independent"))
    assert "forbidden" not in result and "foreign" not in result


@pytest.mark.parametrize("bad_home", ["marker-zero", "foreign-binding", "retired", "alias"])
async def test_home_candidates_are_checked_before_visibility_filters(
    conversation_db: ConversationDB, bad_home: str
) -> None:
    await conversation_db.ui("home", "allowed", home=A.agent_id, is_home=True)
    good = await search(source="ui")
    await conversation_db.ui(
        "sns_retired" if bad_home == "retired" else "conflicting-home",
        "forbidden",
        identity=B if bad_home == "foreign-binding" else A,
        marker=0 if bad_home == "marker-zero" else 1,
        home=f"ToukaSoul{A.agent_id}" if bad_home == "alias" else A.agent_id,
        is_home=True,
    )
    bad = await search(source="ui")
    denied(bad)
    assert all(
        value not in bad for value in ("allowed", "forbidden", B.agent_id, "conflicting-home")
    )
    success(good, 1)
    assert "allowed" in good


@pytest.mark.parametrize("is_home", [False, True])
async def test_own_row_with_foreign_home_is_not_an_ordinary_chat(
    conversation_db: ConversationDB, is_home: bool
) -> None:
    await conversation_db.ui("control", "allowed")
    good = await search(source="ui")
    await conversation_db.ui("bad", "forbidden", home=B.agent_id, is_home=is_home)
    denied(await search(source="ui"))
    success(good, 1)


async def test_old_valid_home_is_hidden_without_hiding_ordinary_chats(
    conversation_db: ConversationDB,
) -> None:
    await conversation_db.ui("old-home", "forbidden", marker=0, home=A.agent_id, is_home=True)
    await conversation_db.ui("ordinary", "allowed")
    result = await search(source="ui")
    success(result, 1)
    assert "allowed" in result and "forbidden" not in result


@pytest.mark.parametrize("damage", ["foreign-binding", "retired", "debug-alias"])
async def test_single_invalid_old_home_is_rejected_before_marker_filter(
    conversation_db: ConversationDB, damage: str
) -> None:
    actor = DEBUG if damage == "debug-alias" else A
    await conversation_db.ui("control", "allowed", identity=actor)
    success(await search(actor, source="ui"), 1)
    await conversation_db.ui(
        "sns_retired" if damage == "retired" else "invalid-home",
        "forbidden",
        identity=B if damage == "foreign-binding" else actor,
        home=f"ToukaSoul{actor.agent_id}" if damage == "debug-alias" else actor.agent_id,
        is_home=True,
        marker=0,
    )
    denied(await search(actor, source="ui"))


async def test_foreign_corrupt_json_is_not_parsed(conversation_db: ConversationDB) -> None:
    await conversation_db.ui("ordinary", "allowed")
    await conversation_db.ui("foreign", "forbidden", identity=B)
    await conversation_db.execute(
        "UPDATE user_sessions SET messages = ? WHERE id = ?", ("{", "foreign")
    )
    result = await search(source="ui")
    success(result, 1)
    assert "allowed" in result and "forbidden" not in result


@pytest.mark.parametrize("source", ["legacy", "ui", "all", None])
async def test_gateway_incognito_refusal_is_paired_with_normal_search(
    conversation_db: ConversationDB, source: str | None
) -> None:
    await conversation_db.ui("ordinary", "allowed-ui")
    conversation_db.legacy("allowed-legacy")
    normal = await search(source=source)
    conversation_db.statements.clear()
    conversation_db.legacy_reads.clear()
    result = await search(source=source, _ctx=PipelineContext(identity=A, incognito=True))
    denied(result)
    assert json.loads(result)["reason"] == "incognito"
    assert conversation_db.statements == []
    assert conversation_db.legacy_reads == []
    success(normal, 2 if source in (None, "all") else 1)


@pytest.mark.parametrize("source", ["legacy", "ui", "all", None])
async def test_real_tool_layer_and_executor_refuse_incognito(
    conversation_db: ConversationDB, source: str | None
) -> None:
    await conversation_db.ui("ordinary", "allowed-ui")
    conversation_db.legacy("allowed-legacy")
    executor = ToolExecutor(
        None, {"memory_tools": gateway.memory_tools}, {}, {}, enforce_advertised=True
    )
    results = []
    for incognito in (False, True):
        ctx = PipelineContext(identity=A, agent_id=A.agent_id, incognito=incognito)
        ctx["memory_tools_schema"] = MEMORY_TOOLS_SCHEMA_LIST
        request = OpenAIChatRequest(model=A.agent_id, messages=[])
        await ToolLayer().process(request, ctx)
        conversation_db.statements.clear()
        conversation_db.legacy_reads.clear()
        result = await executor.execute_to_queue(
            "memory_tools",
            {"action": "conversations", "source": source},
            "request",
            ctx,
            asyncio.Queue(),
            call_id="call",
        )
        results.append(result)
        if incognito:
            denied(result)
            assert json.loads(result)["reason"] == "not_advertised"
            assert conversation_db.statements == []
            assert conversation_db.legacy_reads == []
    success(results[0], 2 if source in (None, "all") else 1)


@pytest.mark.parametrize(
    "field", ["target", "agent_id", "identity", "reader", "conversation_reader", "_ctx"]
)
async def test_public_scope_injection_cannot_change_execution_identity(
    conversation_db: ConversationDB, field: str
) -> None:
    await conversation_db.ui("ordinary", "allowed")
    await conversation_db.ui("foreign", "forbidden", identity=B)
    arguments, error = validate_native_arguments(
        "memory_tools", {"action": "conversations", "source": "ui", field: B.agent_id}
    )
    if error is not None:
        denied(error, "validation_error")
    else:
        assert field not in arguments
        result = await gateway.memory_tools(identity=A, **arguments)
        success(result, 1)
        assert "forbidden" not in result
    allowed = await search(source="ui")
    success(allowed, 1)
    assert "allowed" in allowed and "forbidden" not in allowed


async def test_confirmed_delete_hides_ui_but_keeps_archived_copy(
    conversation_db: ConversationDB,
) -> None:
    await conversation_db.ui("deleted", "archived-copy")
    await conversation_db.ui("control", "allowed")
    conversation_db.legacy("archived-copy")
    snapshot = await async_db.session_service.read_persistence_snapshot("deleted", HUMAN)
    failed = await async_db.session_service.conditional_delete_session("deleted", "stale", HUMAN)
    assert failed.status != "saved"
    before = await search(source="ui")
    saved = await async_db.session_service.conditional_delete_session(
        "deleted", snapshot.revision, HUMAN
    )
    assert saved.status == "saved"
    after = await search(source="ui")
    merged = await search(source="all")
    success(before, 2)
    success(after, 1)
    success(merged, 2)
    assert "archived-copy" not in after and "allowed" in after
    assert "archived-copy" in merged and "legacy" in merged


async def test_incognito_stream_content_is_not_searchable(conversation_db: ConversationDB) -> None:
    await conversation_db.ui("ordinary", "allowed")
    admission = IncognitoStreamAdmission(
        mode="incognito", user_parent_id="u", assistant_node_id="a"
    )
    entry = await dispatcher.stream_buffer_service.create(
        "ephemeral", identity=HUMAN, admission=admission
    )

    async def model_output():
        yield 'data: {"choices":[{"delta":{"content":"incognito-secret"}}]}\n\n'

    await dispatcher._pump_ui_stream(
        model_output(), "ephemeral", entry.stream_id, A.agent_id, HUMAN, admission, A
    )
    assert entry.terminal.persistence.reason == "incognito"
    assert await raw_row("ephemeral") is None
    result = await search(source="ui")
    success(result, 1)
    assert "allowed" in result and "incognito-secret" not in result


@pytest.mark.parametrize("outcome", ["error", "cancelled", "no_text"])
async def test_failed_generation_does_not_restore_rebound_visibility(
    conversation_db: ConversationDB, outcome: str
) -> None:
    await seed(tree=tree("old-scope-content"))
    snapshot = await async_db.session_service.read_persistence_snapshot("chat", HUMAN)
    admission = PersistedStreamAdmission(
        mode="persistent",
        revision=snapshot.revision,
        user_parent_id="m0",
        assistant_node_id="reply",
    )
    admitted = await async_db.session_service.admit_ui_stream("chat", admission, HUMAN, B)
    admission = admission.model_copy(update={"revision": admitted.revision})
    entry = await dispatcher.stream_buffer_service.create(
        "chat", identity=HUMAN, admission=admission
    )

    async def model_output():
        if outcome == "error":
            raise OSError
        if outcome == "cancelled":
            raise asyncio.CancelledError
        yield 'data: {"choices":[{"delta":{"content":""}}]}\n\n'

    operation = dispatcher._pump_ui_stream(
        model_output(), "chat", entry.stream_id, B.agent_id, HUMAN, admission, B
    )
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation
    assert (await raw_row())["session_scope_version"] == 0
    assert (await ui_save(agent_id=A)).persistence.status == "saved"
    await conversation_db.ui("control", "allowed")
    result = await search(source="ui")
    success(result, 1)
    assert "allowed" in result and "old-scope-content" not in result
