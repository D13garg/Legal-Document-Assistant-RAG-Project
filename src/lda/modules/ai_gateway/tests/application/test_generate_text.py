"""The egress use case against fakes: order, fail-closed proofs, limits, retries, redaction."""

from __future__ import annotations

from dataclasses import astuple, replace
from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.ai_gateway.api import (
    INVALID_REQUEST,
    LIMIT_EXCEEDED,
    RATE_LIMITED,
    SCOPE_EMPTY,
    ContextSegment,
    FailureKind,
    GenerateTextCommand,
    GenerationParameters,
    ProviderFailure,
    RateLimited,
)
from lda.modules.ai_gateway.domain.canonical import request_sha256
from lda.modules.ai_gateway.tests.support import (
    ACTION,
    D1,
    D2,
    PRINCIPAL,
    SECRET_QUESTION,
    SECRET_TEXT,
    TENANT_A,
    TENANT_B,
    TOKEN,
    Rig,
    command,
    ctx,
    expectation,
    policy,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import (
    AuthorizationError,
    InfrastructureError,
    LdaError,
    ValidationError,
)
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource

D3 = ProofResource("document", "d3")
SECRETS = (SECRET_TEXT, SECRET_QUESTION, TOKEN)


@pytest.fixture
def rig() -> Rig:
    r = Rig()
    r.allow()
    return r


# ---- happy path and ordering ----------------------------------------------------------------


def test_a_valid_request_returns_a_sensitive_completion_and_content_free_evidence(
    rig: Rig,
) -> None:
    result = rig.use_case.execute(ctx(), command())
    assert isinstance(result.text, Sensitive)
    assert result.text.reveal().startswith("fake-completion:")
    evidence = result.evidence
    assert (evidence.provider_id, evidence.model, evidence.attempts) == ("fake", "fake-model-1", 1)
    assert evidence.parameters == GenerationParameters(256, 0.2)
    assert evidence.resources == (D1, D2)
    assert evidence.request_sha256 == request_sha256(
        provider_id="fake",
        model="fake-model-1",
        parameters=GenerationParameters(256, 0.2),
        instructions=SECRET_QUESTION,
        segments=[("document", "d1", f"{SECRET_TEXT}-d1"), ("document", "d2", f"{SECRET_TEXT}-d2")],
    )
    flat = repr(astuple(evidence))
    assert not any(secret in flat for secret in SECRETS)
    assert not any(secret in repr(result) for secret in SECRETS)


def test_the_proof_is_verified_before_the_provider_is_called(rig: Rig) -> None:
    rig.use_case.execute(ctx(), command())
    assert rig.events == ["verify", "provider"]


def test_the_provider_receives_context_in_order_with_the_policy_timeout(rig: Rig) -> None:
    rig.use_case.execute(ctx(), command())
    (sent,) = rig.provider.requests
    assert [c.reveal() for c in sent.context] == [f"{SECRET_TEXT}-d1", f"{SECRET_TEXT}-d2"]
    assert sent.timeout == timedelta(seconds=10) and sent.model == "fake-model-1"


def test_tenant_and_principal_come_from_the_context_never_the_payload(rig: Rig) -> None:
    rig.use_case.execute(ctx(), command())
    ((token, expected),) = rig.verifier.calls
    assert token == TOKEN
    assert (expected.tenant_id, expected.principal_id, expected.action) == (
        TENANT_A,
        PRINCIPAL,
        ACTION,
    )
    assert not {"tenant_id", "principal_id", "tenant", "principal"} & set(
        GenerateTextCommand.__dataclass_fields__
    )


def test_the_same_request_gives_identical_evidence(rig: Rig) -> None:
    first = rig.use_case.execute(ctx(), command()).evidence
    second = rig.use_case.execute(ctx(), command()).evidence
    assert first == second


def test_changed_content_changes_the_request_hash(rig: Rig) -> None:
    base = rig.use_case.execute(ctx(), command()).evidence.request_sha256
    changed = command(instructions=Sensitive(SECRET_QUESTION + "?"))
    assert rig.use_case.execute(ctx(), changed).evidence.request_sha256 != base


# ---- request shape and size limits (zero verifier and provider calls) --------------------------


def good_segment(text: str = "x") -> ContextSegment:
    return ContextSegment(D1, Sensitive(text))


@pytest.mark.parametrize(
    "overrides",
    [
        {"proof_token": "plain-string"},
        {"proof_token": Sensitive("")},
        {"proof_token": Sensitive(5)},
        {"action": "Not A Dotted Id"},
        {"action": None},
        {"model": ""},
        {"model": "bad model!"},
        {"model": "x" * 129},
        {"instructions": "plain"},
        {"instructions": Sensitive("   ")},
        {"parameters": {"max_output_tokens": 5}},
        {"segments": [good_segment()]},  # a list, not a tuple
        {"segments": (good_segment(), "not a segment")},
        {"segments": (ContextSegment(D1, "plain text"),)},  # type: ignore[arg-type]
        {"segments": (ContextSegment(D1, Sensitive("")),)},
        {"segments": (ContextSegment(None, Sensitive("x")),)},  # type: ignore[arg-type]
        {"segments": (ContextSegment(("document", "d1"), Sensitive("x")),)},  # type: ignore[arg-type]
        {"segments": (ContextSegment(ProofResource("", "d1"), Sensitive("x")),)},
        {"segments": (ContextSegment(ProofResource("Document", "d1"), Sensitive("x")),)},
        {"segments": (ContextSegment(ProofResource("document", ""), Sensitive("x")),)},
        {"segments": (ContextSegment(ProofResource("document", "a\nb"), Sensitive("x")),)},
    ],
)
def test_malformed_requests_are_rejected_before_anything_else(
    rig: Rig, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as raised:
        rig.use_case.execute(ctx(), command(**overrides))
    assert raised.value.code == INVALID_REQUEST
    assert rig.events == [] and rig.provider.call_count == 0


def test_a_non_command_and_a_system_context_are_rejected(rig: Rig) -> None:
    with pytest.raises(ValidationError):
        rig.use_case.execute(ctx(), "text")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        rig.use_case.execute(SystemContext(TENANT_A, "job"), command())  # type: ignore[arg-type]
    assert rig.events == []


def test_a_segment_cannot_be_built_without_a_source_resource() -> None:
    with pytest.raises(TypeError):
        ContextSegment(text=Sensitive("orphan"))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "overrides",
    [
        {"segments": tuple(good_segment() for _ in range(6))},  # max_segments is 5
        {"segments": (good_segment("x" * 101),)},  # max_segment_characters is 100
        {"segments": tuple(good_segment("x" * 100) for _ in range(4))},  # total 400 + question
        {"instructions": Sensitive("q" * 101)},
        {"parameters": GenerationParameters(max_output_tokens=4096)},  # ceiling is 2048
    ],
)
def test_size_limits_are_enforced(rig: Rig, overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        rig.use_case.execute(ctx(), command(**overrides))
    assert raised.value.code == LIMIT_EXCEEDED
    assert rig.events == []


def test_requests_exactly_at_the_limits_are_accepted() -> None:
    rig = Rig(
        egress_policy=policy(
            max_segments=2,
            max_segment_characters=100,
            max_request_characters=205,
            max_instruction_characters=5,
        )
    )
    segments = (
        ContextSegment(D1, Sensitive("x" * 100)),
        ContextSegment(D2, Sensitive("y" * 100)),
    )
    rig.verifier.allow(TOKEN, expectation(D1, D2))
    rig.use_case.execute(ctx(), command(segments=segments, instructions=Sensitive("q" * 5)))
    assert rig.provider.call_count == 1


# ---- resource-set derivation and tampering -------------------------------------------------------


def test_the_resource_set_is_derived_from_the_segments_deterministically(rig: Rig) -> None:
    a = command(D1, D2)
    b = command(D2, D1, D1)  # other order and a duplicate: same set
    rig.use_case.execute(ctx(), a)
    rig.use_case.execute(ctx(), b)
    assert rig.verifier.calls[0][1] == rig.verifier.calls[1][1] == expectation(D1, D2)


def test_evidence_lists_the_resources_sorted_regardless_of_segment_order(rig: Rig) -> None:
    assert rig.use_case.execute(ctx(), command(D2, D1)).evidence.resources == (D1, D2)


@pytest.mark.parametrize("segments", [(D1, D3), (D1,), (D1, D2, D3)])
def test_a_changed_resource_set_is_rejected_and_nothing_is_sent(
    rig: Rig, segments: tuple[ProofResource, ...]
) -> None:
    with pytest.raises(AuthorizationError) as raised:
        rig.use_case.execute(ctx(), command(*segments))  # the proof was issued for D1 + D2
    assert raised.value.code == "proof_mismatch"
    assert rig.provider.call_count == 0


def test_a_different_action_or_tenant_does_not_verify(rig: Rig) -> None:
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), command(action="other.action"))
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(TENANT_B), command())  # proof was for tenant A
    assert rig.provider.call_count == 0


# ---- proof failures fail closed ---------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "proof_malformed",
        "proof_unknown_key",
        "proof_signature_invalid",
        "proof_mismatch",
        "proof_expired",
        "proof_stale_epoch",
        "proof_principal_inactive",
    ],
)
def test_every_proof_rejection_makes_zero_provider_calls_and_keeps_its_code(
    rig: Rig, code: str
) -> None:
    rig.verifier.reject_next(code)
    with pytest.raises(AuthorizationError) as raised:
        rig.use_case.execute(ctx(), command())
    assert raised.value.code == code
    assert rig.provider.call_count == 0 and rig.events == ["verify"]


def test_a_rejected_proof_does_not_consume_rate_budget(rig: Rig) -> None:
    for _ in range(10):
        rig.verifier.reject_next("proof_expired")
        with pytest.raises(AuthorizationError):
            rig.use_case.execute(ctx(), command())
    for _ in range(5):  # the full burst is still available
        rig.use_case.execute(ctx(), command())


def test_an_infrastructure_failure_while_verifying_means_no_egress(rig: Rig) -> None:
    class Broken:
        def verify(self, token: str, expected: object) -> None:
            raise InfrastructureError("epoch_read_failed")

    rig.use_case._verifier = Broken()  # noqa: SLF001
    with pytest.raises(InfrastructureError):
        rig.use_case.execute(ctx(), command())
    assert rig.provider.call_count == 0


def test_an_empty_scope_fails_closed_without_asking_the_verifier(rig: Rig) -> None:
    with pytest.raises(AuthorizationError) as raised:
        rig.use_case.execute(ctx(), command(segments=()))
    assert raised.value.code == SCOPE_EMPTY
    assert rig.events == [] and rig.provider.call_count == 0


# ---- rate limiting -----------------------------------------------------------------------------


def test_the_budget_is_per_tenant_and_exhaustion_is_distinguishable(rig: Rig) -> None:
    rig.verifier.allow(TOKEN, expectation(D1, D2, tenant=TENANT_B))
    for _ in range(5):
        rig.use_case.execute(ctx(), command())
    with pytest.raises(RateLimited) as raised:
        rig.use_case.execute(ctx(), command())
    assert raised.value.code == RATE_LIMITED and raised.value.retry_after_seconds > 0
    assert rig.provider.call_count == 5  # the sixth never reached the provider
    rig.use_case.execute(ctx(TENANT_B), command())  # another tenant has its own bucket
    assert rig.provider.call_count == 6


def test_the_budget_refills_with_time(rig: Rig) -> None:
    for _ in range(5):
        rig.use_case.execute(ctx(), command())
    rig.clock.advance(timedelta(seconds=2))
    rig.use_case.execute(ctx(), command())
    rig.use_case.execute(ctx(), command())
    with pytest.raises(RateLimited):
        rig.use_case.execute(ctx(), command())


# ---- timeouts, retries, failure mapping ----------------------------------------------------------


def test_a_provider_timeout_is_mapped_and_not_retried(rig: Rig) -> None:
    rig.provider.latency = timedelta(seconds=60)
    with pytest.raises(ProviderFailure) as raised:
        rig.use_case.execute(ctx(), command())
    assert raised.value.kind is FailureKind.TIMEOUT and raised.value.code == "provider_timeout"
    assert rig.provider.call_count == 1 and rig.sleeps == []


@pytest.mark.parametrize("kind", [FailureKind.REJECTED, FailureKind.TIMEOUT, FailureKind.AMBIGUOUS])
def test_only_pre_response_transport_failures_are_retried(rig: Rig, kind: FailureKind) -> None:
    rig.provider.fail_next(kind, kind, kind)
    with pytest.raises(ProviderFailure) as raised:
        rig.use_case.execute(ctx(), command())
    assert raised.value.kind is kind
    assert rig.provider.call_count == 1 and rig.sleeps == []


def test_an_unreachable_provider_is_retried_with_backoff_then_succeeds(rig: Rig) -> None:
    rig.provider.fail_next(FailureKind.UNREACHABLE, FailureKind.UNREACHABLE)
    result = rig.use_case.execute(ctx(), command())
    assert result.evidence.attempts == 3 and rig.provider.call_count == 3
    assert rig.sleeps == [timedelta(milliseconds=100), timedelta(milliseconds=200)]
    assert rig.events == ["verify", "provider"] * 3  # re-verified before every provider call


def test_retries_are_bounded(rig: Rig) -> None:
    rig.provider.fail_next(*[FailureKind.UNREACHABLE] * 10)
    with pytest.raises(ProviderFailure) as raised:
        rig.use_case.execute(ctx(), command())
    assert raised.value.kind is FailureKind.UNREACHABLE
    assert rig.provider.call_count == 3 and len(rig.sleeps) == 2


def test_a_single_attempt_policy_never_retries() -> None:
    rig = Rig(egress_policy=policy(max_attempts=1))
    rig.allow()
    rig.provider.fail_next(FailureKind.UNREACHABLE)
    with pytest.raises(ProviderFailure):
        rig.use_case.execute(ctx(), command())
    assert rig.provider.call_count == 1 and rig.sleeps == []


def test_a_proof_revoked_during_the_backoff_stops_the_retry(rig: Rig) -> None:
    rig.provider.fail_next(FailureKind.UNREACHABLE)
    rig.verifier.reject_next(None, "proof_stale_epoch")  # first verify passes, the retry's fails
    with pytest.raises(AuthorizationError) as raised:
        rig.use_case.execute(ctx(), command())
    assert raised.value.code == "proof_stale_epoch" and rig.provider.call_count == 1


def test_every_retry_spends_rate_budget() -> None:
    rig = Rig(egress_policy=policy(rate=policy().rate.__class__(capacity=2, refill_per_second=1.0)))
    rig.allow()
    rig.provider.fail_next(FailureKind.UNREACHABLE, FailureKind.UNREACHABLE)
    with pytest.raises(RateLimited):
        rig.use_case.execute(ctx(), command())
    assert rig.provider.call_count == 2


def test_an_unexpected_adapter_error_is_not_swallowed_or_retried(rig: Rig) -> None:
    def explode(request: object) -> None:
        raise RuntimeError("programming error")

    rig.provider.generate = explode  # type: ignore[method-assign,assignment]
    with pytest.raises(RuntimeError):
        rig.use_case.execute(ctx(), command())


# ---- redaction ----------------------------------------------------------------


def failures(rig: Rig) -> list[LdaError]:
    caught: list[LdaError] = []
    cases: list[tuple[object, GenerateTextCommand]] = [
        (None, command(model="")),
        (None, command(segments=())),
        ("proof_expired", command()),
        (FailureKind.REJECTED, command()),
        (FailureKind.TIMEOUT, command()),
        (FailureKind.UNREACHABLE, command()),
    ]
    for script, cmd in cases:
        if isinstance(script, str):
            rig.verifier.reject_next(script)
        elif isinstance(script, FailureKind):
            rig.provider.fail_next(*([script] * 5))
        try:
            rig.use_case.execute(ctx(), cmd)
        except LdaError as error:
            caught.append(error)
        rig.provider._failures.clear()  # noqa: SLF001
        rig.clock.advance(timedelta(seconds=60))
    return caught


def test_no_error_repr_detail_or_cause_contains_content_or_credentials(rig: Rig) -> None:
    caught = failures(rig)
    assert len(caught) == 6
    for error in caught:
        rendered = " ".join(
            [str(error), repr(error), error.detail, repr(error.args), repr(error.__cause__)]
        )
        assert not any(secret in rendered for secret in SECRETS)
    provider_errors = [e for e in caught if isinstance(e, ProviderFailure)]
    assert provider_errors and all(e.__cause__ is None for e in provider_errors)


def test_commands_requests_and_results_do_not_show_content_in_their_repr(rig: Rig) -> None:
    cmd = command()
    result = rig.use_case.execute(ctx(), cmd)
    (sent,) = rig.provider.requests
    for obj in (cmd, sent, result):
        assert not any(secret in repr(obj) + str(obj) for secret in SECRETS)
    assert replace(cmd, action=ACTION) == cmd  # still a plain frozen value
    assert UUID(int=1)  # (keeps UUID import honest for fixtures above)
