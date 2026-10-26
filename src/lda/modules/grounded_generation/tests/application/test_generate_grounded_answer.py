"""GenerateGroundedAnswer: ordering, trust boundaries, answer contract, failure paths (ADR-029).

The rig's shared event log orders authorization, proof verification, chunk reads and the provider
call, so these tests prove WHEN things happened. The provider is scripted and records exactly
what it was sent.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import replace
from uuid import uuid4

import pytest

from lda.modules.ai_gateway.api import FailureKind, ProviderFailure
from lda.modules.citations.api import SOURCE_UNAVAILABLE, GenerationOutcome
from lda.modules.grounded_generation import api
from lda.modules.grounded_generation.api import (
    NOT_AUTHORIZED,
    OUTPUT_CITATION_INVALID,
    OUTPUT_MALFORMED,
    OUTPUT_QUOTE_UNSUPPORTED,
    OUTPUT_SCHEMA,
    OUTPUT_TOO_LARGE,
    OUTPUT_UNSAFE_TEXT,
    REQUIRES_PRINCIPAL,
    TEMPLATE_UNAVAILABLE,
    ContractLimits,
    GenerateGroundedAnswerCommand,
)
from lda.modules.grounded_generation.tests.support import (
    CONFIG_ID,
    INJECTION,
    PASSAGES,
    QUESTION,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx,
    reply,
)
from lda.modules.prompts.api import PromptCatalog, PromptRenderer, production_catalog
from lda.modules.prompts.testing import default_limits
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import (
    AuthorizationError,
    ConfigurationError,
    InfrastructureError,
    LdaError,
    NotFoundError,
    ValidationError,
)
from lda.shared.kernel.sensitive import Sensitive

QUOTE_1 = "shall give ninety days written notice"
QUOTE_2 = "payable within thirty days of the invoice date"


def good_reply() -> str:
    return reply(
        ("The notice period is ninety days.", "S1", QUOTE_1),
        ("Fees fall due within thirty days.", "S2", QUOTE_2),
        ("Notice is also covered here.", "S1", "ninety days written notice"),
    )


def committed_records(rig: Rig) -> list[object]:
    return list(rig.units.committed_records())


def assert_nothing_happened(rig: Rig) -> None:
    assert rig.provider.requests == []
    assert committed_records(rig) == [] and rig.units.audit == []


# ---- the answered path ----------------------------------------------------------------------


def test_a_grounded_answer_is_validated_recorded_and_audited() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.ANSWERED and answer.rejection is None
    assert [s.citation.marker for s in answer.statements] == ["S1", "S2", "S1"]
    assert answer.statements[0].text.reveal() == "The notice period is ninety days."
    assert answer.statements[0].citation.source.chunk_id == rig.docs[0][2].chunk_id
    (record,) = committed_records(rig)
    assert record.record_id == answer.record_id  # type: ignore[attr-defined]
    assert record.outcome is GenerationOutcome.ANSWERED  # type: ignore[attr-defined]
    assert [c.marker for c in record.citations] == ["S1", "S2"]  # type: ignore[attr-defined]
    assert record.template_id == "grounded_answer" and record.template_version == "v1"  # type: ignore[attr-defined]
    assert record.config_id == CONFIG_ID and record.model_id == "fake-model:1"  # type: ignore[attr-defined]
    (fact,) = rig.units.audit
    assert fact.action == "grounded_generation.generation_recorded"
    assert (fact.tenant_id, fact.actor) == (TENANT_A, ctx().principal_id)
    assert fact.resource_id == str(answer.record_id)
    assert dict(fact.attributes) == {
        "generation_outcome": "answered",
        "citation_count": "2",
        "template": "grounded_answer:v1",
        "rejection": "none",
    }


def test_authorization_precedes_every_read_and_the_provider_is_authorized_again() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    rig.use_case.execute(ctx(), rig.command())
    events = rig.events
    assert len(rig.access.calls) == 3  # evidence, egress, release
    assert events[0] == "authorize"
    assert events.index("read_chunks") > events.index("verify") > events.index("authorize")
    second = [i for i, e in enumerate(events) if e == "authorize"][1]
    assert second > events.index("read_chunks")  # a fresh decision right before egress
    assert events[second:][:3] == [
        "authorize",
        "verify",
        "provider",
    ]  # gateway verifies, then sends
    assert events[-1] == "authorize"  # and one more before any statement is released
    assert rig.access.calls[0] == rig.access.calls[1] == {d.value for d, _, _ in rig.docs}


def test_the_provider_receives_only_authorized_evidence_in_untrusted_blocks() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    rig.use_case.execute(ctx(), rig.command())
    (request,) = rig.provider.requests
    context = [c.reveal() for c in request.context]
    assert len(context) == 3
    assert all(PASSAGES[i] in context[i] for i in range(3))
    instructions = request.instructions.reveal()
    # the question is inside its own untrusted block, after the trusted instructions
    assert instructions.index("[[LDA:BEGIN block=user_question") > instructions.index("Trust rules")
    assert "block=user_question trust=untrusted" in instructions
    assert "part=1/3" in context[0] and "trust=untrusted" in context[0]


# ---- authorization --------------------------------------------------------------------------


def test_a_caller_without_access_reads_and_sends_nothing() -> None:
    rig = Rig()
    rig.access.allowed = set()
    with pytest.raises(AuthorizationError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.code == NOT_AUTHORIZED
    assert rig.store.reads == 0
    assert "read_chunks" not in rig.events and "provider" not in rig.events
    assert_nothing_happened(rig)


def test_one_unauthorized_document_denies_the_whole_request() -> None:
    rig = Rig()
    rig.access.allowed = {rig.docs[0][0].value, rig.docs[1][0].value}  # the third is forbidden
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert rig.store.reads == 0
    assert_nothing_happened(rig)


def test_unauthorized_text_never_reaches_the_provider() -> None:
    rig = Rig()
    rig.access.allowed = {rig.docs[0][0].value}
    rig.provider.replies.append(good_reply())
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert rig.provider.requests == []
    # even a request restricted to the permitted document works, and carries only its text
    rig.use_case.execute(ctx(), rig.command(claims=(rig.claims[0],)))
    sent = " ".join(c.reveal() for c in rig.provider.requests[0].context)
    assert PASSAGES[0] in sent and PASSAGES[1] not in sent and PASSAGES[2] not in sent


def test_a_grant_revoked_before_egress_stops_the_call() -> None:
    rig = Rig()
    rig.access.deny_from_call = 2  # allowed for the evidence, revoked before the send
    rig.provider.replies.append(good_reply())
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert rig.provider.requests == []
    assert_nothing_happened(rig)


def test_a_revocation_during_the_chunk_read_yields_no_generation() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    original = rig.store.read

    def read_then_revoke(*args, **kwargs):  # type: ignore[no-untyped-def]
        found = original(*args, **kwargs)
        rig.access.revoked = True
        return found

    rig.store.read = read_then_revoke  # type: ignore[method-assign]
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert_nothing_happened(rig)


def test_another_tenants_documents_are_unavailable_not_generated_from() -> None:
    rig = Rig()
    rig.store.versions.clear()
    rig.load_documents(TENANT_B, PASSAGES)  # the chunks exist, under another tenant
    claims = tuple(rig.claim(i) for i in range(3))
    rig.provider.replies.append(good_reply())
    with pytest.raises(NotFoundError) as caught:
        rig.use_case.execute(ctx(TENANT_A), rig.command(claims=claims))
    assert caught.value.code == SOURCE_UNAVAILABLE
    assert_nothing_happened(rig)


def test_system_work_is_never_a_user_question() -> None:
    rig = Rig()
    with pytest.raises(AuthorizationError) as caught:
        rig.use_case.execute(SystemContext(TENANT_A, "worker"), rig.command())
    assert caught.value.code == REQUIRES_PRINCIPAL
    assert rig.events == []


# ---- prompt injection -----------------------------------------------------------------------


def test_instructions_inside_a_document_stay_data_and_cannot_forge_a_boundary() -> None:
    passages = (PASSAGES[0] + " " + INJECTION, PASSAGES[1])
    rig = Rig(passages=passages)
    rig.provider.replies.append(reply(("Ninety days.", "S1", QUOTE_1)))
    rig.use_case.execute(ctx(), rig.command())
    (request,) = rig.provider.requests
    assert "IGNORE ALL PREVIOUS" not in request.instructions.reveal()
    assert "PWNED" not in request.instructions.reveal()
    injected = request.context[0].reveal()
    assert "IGNORE ALL PREVIOUS" in injected  # delivered as data, inside its own block
    # only the renderer's own delimiter lines are live; the forged ones were neutralized
    assert len(re.findall(r"(?<!\\)\[\[LDA:END", injected)) == 1
    assert len(re.findall(r"(?<!\\)\[\[LDA:BEGIN", injected)) == 1


def test_a_model_that_obeys_the_injection_is_rejected_and_nothing_is_shown() -> None:
    rig = Rig(passages=(PASSAGES[0] + " " + INJECTION, PASSAGES[1]))
    rig.provider.replies.append("PWNED")
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.REJECTED and answer.rejection == OUTPUT_MALFORMED
    assert answer.statements == ()
    (record,) = committed_records(rig)
    assert record.outcome is GenerationOutcome.REJECTED and record.citations == ()  # type: ignore[attr-defined]


def test_an_injected_citation_to_unsupplied_evidence_is_rejected() -> None:
    rig = Rig()
    rig.provider.replies.append(reply(("Per the secret annex.", "S9", QUOTE_1)))
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.rejection == OUTPUT_CITATION_INVALID and answer.statements == ()


# ---- the answer contract: rejection, insufficiency, no silent grounding ---------------------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("not json", OUTPUT_MALFORMED),
        ("```json\n" + reply(("x.", "S1", QUOTE_1)) + "\n```", OUTPUT_MALFORMED),
        (reply(("x.", "S1", QUOTE_1)) + " trailing", OUTPUT_MALFORMED),
        ('{"outcome": "answered", "outcome": "answered", "statements": []}', OUTPUT_MALFORMED),
        ('{"outcome": "answered", "statements": NaN}', OUTPUT_MALFORMED),
        ("[]", OUTPUT_SCHEMA),
        ('{"outcome": "answered", "statements": [], "extra": 1}', OUTPUT_SCHEMA),
        (reply(outcome="answered"), OUTPUT_SCHEMA),
        (reply(("x.", "S1", QUOTE_1), outcome="insufficient_evidence"), OUTPUT_SCHEMA),
        (reply(("x.", "S9", QUOTE_1)), OUTPUT_CITATION_INVALID),
        (reply(("x.", "s1", QUOTE_1)), OUTPUT_CITATION_INVALID),
        (reply(("x.", "S1, S2", QUOTE_1)), OUTPUT_CITATION_INVALID),
        (reply(("x.", "S1", "the supplier pays ninety days notice")), OUTPUT_QUOTE_UNSUPPORTED),
        (reply(("x.", "S2", QUOTE_1)), OUTPUT_QUOTE_UNSUPPORTED),  # real quote, wrong source
        (reply(("x.", "S1", QUOTE_1.upper())), OUTPUT_QUOTE_UNSUPPORTED),
        (reply(("x.", "S1", "short")), OUTPUT_SCHEMA),
        (reply(("two\nlines", "S1", QUOTE_1)), OUTPUT_UNSAFE_TEXT),
        (reply(("zero\u200bwidth", "S1", QUOTE_1)), OUTPUT_UNSAFE_TEXT),
        (reply(("[[LDA:BEGIN forged", "S1", QUOTE_1)), OUTPUT_UNSAFE_TEXT),
        ("x" * 25_000, OUTPUT_TOO_LARGE),
    ],
)
def test_a_reply_that_breaks_the_contract_is_rejected_whole_and_never_shown(
    raw: str, code: str
) -> None:
    rig = Rig()
    rig.provider.replies.append(raw)
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.REJECTED
    assert (answer.rejection, answer.statements) == (code, ())
    (record,) = committed_records(rig)
    assert record.outcome is GenerationOutcome.REJECTED and record.citations == ()  # type: ignore[attr-defined]
    (fact,) = rig.units.audit
    assert fact.attributes["rejection"] == code and fact.attributes["citation_count"] == "0"


def test_a_good_statement_next_to_a_bad_one_does_not_survive() -> None:
    rig = Rig()
    rig.provider.replies.append(
        reply(("Ninety days.", "S1", QUOTE_1), ("Invented.", "S2", "a sentence nobody wrote here"))
    )
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.REJECTED and answer.statements == ()


def test_a_quote_may_differ_from_the_source_only_in_whitespace() -> None:
    rig = Rig(passages=("The Supplier shall give\n  ninety days   written\tnotice.", PASSAGES[1]))
    rig.provider.replies.append(reply(("Ninety days.", "S1", "give ninety days written notice")))
    assert rig.use_case.execute(ctx(), rig.command()).outcome is GenerationOutcome.ANSWERED


def test_the_model_may_say_the_evidence_is_insufficient() -> None:
    rig = Rig()
    rig.provider.replies.append(reply(outcome="insufficient_evidence"))
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.INSUFFICIENT_EVIDENCE
    assert answer.statements == () and answer.rejection is None and answer.record_id is not None
    (record,) = committed_records(rig)
    assert record.outcome is GenerationOutcome.INSUFFICIENT_EVIDENCE and record.citations == ()  # type: ignore[attr-defined]


def test_no_sources_means_no_authorization_no_read_no_call_and_no_record() -> None:
    rig = Rig()
    answer = rig.use_case.execute(ctx(), rig.command(claims=()))
    assert answer.outcome is GenerationOutcome.INSUFFICIENT_EVIDENCE and answer.record_id is None
    assert rig.events == [] and rig.store.reads == 0
    assert_nothing_happened(rig)


# ---- provider failures never become answers -------------------------------------------------


@pytest.mark.parametrize("kind", list(FailureKind))
def test_a_provider_failure_raises_and_leaves_no_answer_and_no_record(kind: FailureKind) -> None:
    rig = Rig()
    rig.provider.failures.extend([kind] * 3)
    with pytest.raises(ProviderFailure) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.kind is kind
    assert committed_records(rig) == [] and rig.units.audit == [] and rig.units.begun == 0


def test_a_timeout_is_not_retried_and_an_unreachable_provider_is_bounded() -> None:
    rig = Rig()
    rig.provider.failures.append(FailureKind.TIMEOUT)
    with pytest.raises(ProviderFailure):
        rig.use_case.execute(ctx(), rig.command())
    assert len(rig.provider.requests) == 1
    rig2 = Rig()
    rig2.provider.failures.extend([FailureKind.UNREACHABLE] * 3)
    with pytest.raises(ProviderFailure):
        rig2.use_case.execute(ctx(), rig2.command())
    assert len(rig2.provider.requests) == 3


# ---- recording is part of the contract ------------------------------------------------------


def test_if_the_record_cannot_be_committed_no_answer_is_returned() -> None:
    rig = Rig()
    rig.units.fail_commit = True
    rig.provider.replies.append(good_reply())
    with pytest.raises(InfrastructureError):
        rig.use_case.execute(ctx(), rig.command())
    assert committed_records(rig) == [] and rig.units.audit == []


def test_the_record_and_its_audit_fact_commit_together_in_one_transaction() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    rig.use_case.execute(ctx(), rig.command())
    assert rig.units.begun == 1 and len(rig.units.audit) == 1 and len(committed_records(rig)) == 1


# ---- configuration and input errors ---------------------------------------------------------


def test_with_no_approved_template_a_real_render_fails_closed_before_any_send() -> None:
    rig = Rig()
    rig.build(PromptRenderer(PromptCatalog((), production=True), default_limits()))
    rig.provider.replies.append(good_reply())
    with pytest.raises(ConfigurationError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.code == TEMPLATE_UNAVAILABLE
    assert rig.provider.requests == []
    assert rig.events.count("authorize") == 1  # no second decision was even needed
    assert_nothing_happened(rig)


def test_the_real_production_catalog_serves_the_approved_template_end_to_end() -> None:
    """ADR-029 D8: the exact approved wording is what the provider is sent, and the record names
    the template and version that produced the answer."""
    rig = Rig()
    rig.build(PromptRenderer(production_catalog(), default_limits()))
    rig.provider.replies.append(good_reply())
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.ANSWERED and answer.rejection is None
    approved = production_catalog().get("grounded_answer", 1)
    assert approved.test_only is False
    (request,) = rig.provider.requests
    assert request.instructions.reveal().startswith(approved.instructions + "\n\n")
    (record,) = committed_records(rig)
    assert (record.template_id, record.template_version) == ("grounded_answer", "v1")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("template_id", "template_version"),
    [
        ("grounded_answer", 2),  # a version nobody approved
        ("grounded_answer", 1_000_000),
        ("grounded_answer_v2", 1),  # a different template nobody approved
        ("test_only_question_context", 1),  # the test-only template is never production
    ],
)
def test_with_the_real_production_catalog_any_unapproved_selection_fails_closed(
    template_id: str, template_version: int
) -> None:
    rig = Rig()
    rig.policy = replace(rig.policy, template_id=template_id, template_version=template_version)
    rig.build(PromptRenderer(production_catalog(), default_limits()))
    rig.provider.replies.append(good_reply())
    with pytest.raises(ConfigurationError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.code == TEMPLATE_UNAVAILABLE
    assert_nothing_happened(rig)  # no provider call, no record, no audit
    if template_id != "grounded_answer":
        assert template_id not in repr(caught.value)  # the refusal never echoes the selection


@pytest.mark.parametrize(
    "bad",
    [
        GenerateGroundedAnswerCommand("question", (), CONFIG_ID),  # type: ignore[arg-type]
        GenerateGroundedAnswerCommand(Sensitive("q"), (), "rag-short"),
        GenerateGroundedAnswerCommand(Sensitive("q"), [], CONFIG_ID),  # type: ignore[arg-type]
    ],
)
def test_malformed_commands_are_refused_before_anything_else(
    bad: GenerateGroundedAnswerCommand,
) -> None:
    rig = Rig()
    with pytest.raises(ValidationError):
        rig.use_case.execute(ctx(), bad)
    assert rig.events == []


def test_too_many_sources_are_refused_before_authorization() -> None:
    rig = Rig()
    many = rig.claims * 5  # 15 > max_sources (10)
    with pytest.raises(ValidationError):
        rig.use_case.execute(ctx(), rig.command(claims=many))
    assert rig.events == []


def test_an_oversized_passage_fails_the_request_instead_of_being_truncated() -> None:
    rig = Rig(passages=("A" * 60_000, PASSAGES[1]))
    with pytest.raises(ValidationError):
        rig.use_case.execute(ctx(), rig.command())
    assert rig.provider.requests == []


# ---- nothing sensitive leaks ----------------------------------------------------------------


def test_no_content_reaches_logs_audit_records_or_reprs(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    with caplog.at_level(logging.DEBUG):
        answer = rig.use_case.execute(ctx(), rig.command())
    secrets = [QUESTION, *PASSAGES, "ninety days", "The notice period"]
    (fact,) = rig.units.audit
    haystacks = [
        caplog.text,
        repr(answer),
        repr(fact),
        repr(committed_records(rig)[0]),
        json.dumps(dict(fact.attributes)),
    ]
    for haystack in haystacks:
        assert not any(s in haystack for s in secrets)


def test_a_rejected_reply_is_not_recoverable_from_the_result_or_the_audit_fact() -> None:
    rig = Rig()
    rig.provider.replies.append("LEAKY-MODEL-OUTPUT-9917")
    answer = rig.use_case.execute(ctx(), rig.command())
    (fact,) = rig.units.audit
    assert "LEAKY-MODEL-OUTPUT-9917" not in repr(answer) + repr(fact) + repr(committed_records(rig))


def test_every_exported_name_exists() -> None:
    assert all(hasattr(api, n) for n in api.__all__)
    assert uuid4() != uuid4()
    assert isinstance(ContractLimits(), ContractLimits)
    assert isinstance(LdaError("grounded_x"), Exception)


# ---- review additions: release-time authorization, adapter bugs, leak sweep ----------------------


def test_a_grant_revoked_while_the_provider_runs_withholds_the_answer() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())

    def revoke() -> None:
        rig.access.deny_from_call = 3  # the decision taken after generation

    rig.provider.on_generate = revoke
    with pytest.raises(AuthorizationError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.code == NOT_AUTHORIZED
    # the generation did happen and stays provable; only its release is refused
    (record,) = committed_records(rig)
    assert record.outcome is GenerationOutcome.ANSWERED  # type: ignore[attr-defined]
    assert len(rig.units.audit) == 1


def test_a_non_answer_is_not_blocked_by_the_release_decision() -> None:
    for raw in (reply(outcome="insufficient_evidence"), "not json"):
        rig = Rig()
        rig.access.deny_from_call = 3  # would deny a release; nothing is released here
        rig.provider.replies.append(raw)
        answer = rig.use_case.execute(ctx(), rig.command())
        assert answer.statements == () and len(rig.access.calls) == 2


def test_an_unexpected_provider_exception_fails_closed_without_a_record() -> None:
    """The gateway only translates `ProviderFailure`; adapters must raise nothing else. If one does,
    the request still fails: no answer, no record, no audit fact."""
    rig = Rig()
    rig.provider.crash = RuntimeError("adapter bug")
    with pytest.raises(RuntimeError):
        rig.use_case.execute(ctx(), rig.command())
    assert rig.units.begun == 0 and committed_records(rig) == [] and rig.units.audit == []


@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_an_empty_reply_is_rejected_not_treated_as_an_answer(raw: str) -> None:
    rig = Rig()
    rig.provider.replies.append(raw)
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.REJECTED and answer.rejection == OUTPUT_MALFORMED
    assert answer.statements == ()


def test_the_same_request_run_twice_is_two_generations_with_two_records() -> None:
    """There is no idempotency key: each execution is its own generation and own record."""
    rig = Rig()
    rig.provider.replies.extend([good_reply(), good_reply()])
    first = rig.use_case.execute(ctx(), rig.command())
    second = rig.use_case.execute(ctx(), rig.command())
    assert first.record_id != second.record_id
    assert len(committed_records(rig)) == 2 and len(rig.units.audit) == 2


def _timeout(rig: Rig) -> None:
    rig.provider.failures.append(FailureKind.TIMEOUT)


def _deny(rig: Rig) -> None:
    rig.access.allowed = set()


def _purged(rig: Rig) -> None:
    rig.store.versions.clear()


def _oversized(rig: Rig) -> None:
    rig.load_documents(TENANT_A, ("LEAK-" + "A" * 60_000, PASSAGES[1], PASSAGES[2]))
    rig.claims = tuple(rig.claim(i) for i in range(3))


def _no_template(rig: Rig) -> None:
    rig.build(PromptRenderer(PromptCatalog((), production=True), default_limits()))


def _commit_fails(rig: Rig) -> None:
    rig.units.fail_commit = True
    rig.provider.replies.append(good_reply())


def _released_after_revocation(rig: Rig) -> None:
    rig.provider.replies.append(good_reply())
    rig.provider.on_generate = lambda: setattr(rig.access, "deny_from_call", 3)


def _revoked_during_read(rig: Rig) -> None:
    rig.provider.replies.append(good_reply())
    original = rig.store.read

    def read_then_revoke(*args, **kwargs):  # type: ignore[no-untyped-def]
        found = original(*args, **kwargs)
        rig.access.revoked = True
        return found

    rig.store.read = read_then_revoke  # type: ignore[method-assign]


@pytest.mark.parametrize(
    "arrange",
    [
        _timeout,
        _deny,
        _purged,
        _oversized,
        _no_template,
        _commit_fails,
        _released_after_revocation,
        _revoked_during_read,
    ],
)
def test_no_failure_leaks_content_through_messages_or_exception_chains(arrange) -> None:  # type: ignore[no-untyped-def]
    """The whole formatted traceback (message, `__cause__`, `__context__`) is searched."""
    rig = Rig()
    arrange(rig)
    with pytest.raises(LdaError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    rendered = "".join(traceback.format_exception(caught.value))
    for secret in (QUESTION, "LEAK-", *PASSAGES, "ninety days", "SECRET-QUESTION"):
        assert secret not in rendered, secret
    assert caught.value.__cause__ is None
