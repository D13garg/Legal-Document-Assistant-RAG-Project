"""A consumer can use conversations through api/ + testing/ alone; nothing internal leaks."""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path
from uuid import uuid4

import lda.modules.conversations.api as api
from lda.modules.conversations.api import (
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationReference,
    MessageRole,
    MessageView,
    StartConversationCommand,
)
from lda.modules.conversations.testing import InMemoryConversationsUnitOfWorkFactory
from lda.modules.conversations.tests.support import build_kit

MODULE = Path(api.__file__).resolve().parents[1]

EXPECTED_SURFACE = {
    "DELETION_SCOPE",
    "LIMIT_REACHED",
    "MESSAGE_CONFLICT",
    "MESSAGE_INVALID",
    "NOT_FOUND",
    "RECORD_LINKED",
    "RECORD_UNAVAILABLE",
    "AppendAssistantTurn",
    "AppendAssistantTurnCommand",
    "AppendUserMessage",
    "AppendUserMessageCommand",
    "AppendedMessageView",
    "ClientMessageId",
    "ConversationHistoryView",
    "ConversationId",
    "ConversationLimits",
    "ConversationListView",
    "ConversationReference",
    "ConversationView",
    "ConversationsUnitOfWork",
    "ConversationsUnitOfWorkFactory",
    "DeleteConversation",
    "DeletionView",
    "ListConversations",
    "ListConversationsQuery",
    "MessageRole",
    "MessageView",
    "ReadConversation",
    "StartConversation",
    "StartConversationCommand",
}


def test_surface_is_exactly_the_approved_contract() -> None:
    assert set(api.__all__) == EXPECTED_SURFACE
    assert all(hasattr(api, name) for name in api.__all__)


def test_no_repository_adapter_or_table_is_exported() -> None:
    for name in api.__all__:
        assert not re.search(r"Sql|Repository|Table|Engine|Session", name), name


def test_a_consumer_can_run_a_conversation_using_only_public_names() -> None:
    kit = build_kit(InMemoryConversationsUnitOfWorkFactory())
    cid = (
        api.StartConversation(kit.factory, kit.clock, kit.ids)
        .execute(kit.ctx, StartConversationCommand())
        .conversation_id
    )
    api.AppendUserMessage(kit.factory, kit.clock, api.ConversationLimits(5, 100, 5)).execute(
        kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "hello")
    )
    history = api.ReadConversation(kit.factory).execute(kit.ctx, ConversationReference(cid))
    assert history.messages[0].role is MessageRole.USER


def test_the_assistant_view_cannot_imply_a_stored_answer() -> None:
    fields = {f.name for f in dataclasses.fields(MessageView)}
    assert "answer" not in fields and "citations" not in fields and "excerpt" not in fields
    assert MessageView.__doc__ and "NOT stored" in MessageView.__doc__
    assert "cannot be reconstructed" in MessageView.__doc__
    readme = (MODULE / "README.md").read_text(encoding="utf-8")
    assert "No answer text is stored" in readme and "cannot be reconstructed" in readme


def test_views_and_commands_hide_text_in_repr() -> None:
    for cls, name in ((AppendUserMessageCommand, "text"), (MessageView, "text")):
        field = next(f for f in dataclasses.fields(cls) if f.name == name)
        assert field.repr is False, cls.__name__


def test_the_api_exports_no_document_or_version_identifier() -> None:
    """ADR-039 C3: provenance is represented only through linked generation records."""
    for cls in (api.MessageView, api.ConversationView, api.AppendUserMessageCommand):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not {n for n in names if "document" in n or "version" in n}


def test_a_lock_file_exists_and_lists_every_exported_name() -> None:
    lock = (MODULE / "api.lock").read_text(encoding="utf-8")
    for name in api.__all__:
        if name.isupper():
            continue  # constants are not class entries
        assert f"## {name}" in lock, name


def test_production_code_never_imports_a_sibling_modules_internals() -> None:
    for path in MODULE.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                if name.startswith("lda.modules.") and not name.startswith(
                    "lda.modules.conversations"
                ):
                    assert name == "lda.modules.citations.api", (path.name, name)
