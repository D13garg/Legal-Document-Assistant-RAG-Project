"""The use case of grounded generation (ADR-029)."""

from __future__ import annotations

from lda.modules.citations.api import (
    GenerationOutcome,
    GenerationRecorder,
    RecordGenerationCommand,
    TextEvidence,
)
from lda.modules.grounded_generation.application.answer_drafter import AnswerDrafter, Draft
from lda.modules.grounded_generation.application.attribution import attribution_of
from lda.modules.grounded_generation.application.dto import (
    AnswerStatement,
    GenerateGroundedAnswerCommand,
    GroundedAnswer,
)
from lda.modules.grounded_generation.application.evidence_assembler import EvidenceAssembler
from lda.modules.grounded_generation.application.policy import CONFIG_ID, GroundingPolicy
from lda.modules.grounded_generation.application.ports import GroundedUnitOfWorkFactory
from lda.modules.grounded_generation.application.verification import verify_statements
from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.domain.answer_contract import (
    OUTCOME_INSUFFICIENT,
    ContractViolation,
    parse_answer,
)
from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import AuthorizationError, ValidationError
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock, IdGenerator


@requires("documents.read_document")
@audited("grounded_generation.generation_recorded")
class GenerateGroundedAnswer:
    """Authorize -> verified text evidence -> prompt -> re-authorize -> gateway -> contract ->
    one transaction that records the generation and its audit fact -> a last authorization before
    statements are released.

    Order is a security property (ADR-029 section 2). Nothing is read before the caller's access to
    every cited document has been decided, nothing is sent before it has been decided again, and
    nothing is shown before the provider's reply has passed the answer contract, the record is
    committed and access has been decided a third time (a grant revoked during generation withholds
    the answer; the record of the generation stays). If the record cannot be written, the answer is
    withheld. A provider failure, a
    timeout or a rate limit raises; no answer and no record are made up for it. No text, quote,
    prompt or reply is logged, audited or recorded.
    """

    def __init__(
        self,
        assembler: EvidenceAssembler,
        drafter: AnswerDrafter,
        units_of_work: GroundedUnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
        policy: GroundingPolicy,
    ) -> None:
        self._assembler = assembler
        self._drafter = drafter
        self._units_of_work = units_of_work
        self._clock = clock
        self._ids = ids
        self._policy = policy

    def execute(
        self, ctx: ExecutionContext, command: GenerateGroundedAnswerCommand
    ) -> GroundedAnswer:
        if not isinstance(ctx, TenantContext):  # system work is never a user's question
            raise AuthorizationError(codes.REQUIRES_PRINCIPAL)
        self._check(command)
        if not command.claims:  # nothing to ground on: nothing authorized, read or sent
            return GroundedAnswer(GenerationOutcome.INSUFFICIENT_EVIDENCE, (), None)
        evidence = self._assembler.assemble(ctx, command.claims)
        draft = self._drafter.draft(ctx, command.question, evidence)
        outcome, statements, rejection = self._judge(draft, evidence)
        citations = evidence.evidence.resolve(
            [s.citation.marker for s in statements],
            max_markers=self._policy.max_markers,
            allow_empty=True,
        )
        who = attribution_of(ctx)
        with self._units_of_work.begin(ctx) as uow:
            recorded = GenerationRecorder(uow.records, self._clock, self._ids).record(
                ctx,
                RecordGenerationCommand(
                    request_id=ctx.request_id,
                    evidence=evidence.evidence,
                    citations=citations,
                    outcome=outcome,
                    config_id=command.config_id,
                    template_id=self._policy.template_id,
                    template_version=f"v{self._policy.template_version}",
                    model_id=draft.call.model,
                    request_sha256=draft.call.request_sha256,
                ),
            )
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="grounded_generation.generation_recorded",
                    resource_type="generation_record",
                    resource_id=str(recorded.record_id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=self._clock.now(),
                    attributes={
                        **who.attributes,
                        "generation_outcome": outcome.value,
                        "citation_count": str(len(citations)),
                        "template": f"{self._policy.template_id}:v{self._policy.template_version}",
                        "rejection": rejection or "none",
                    },
                )
            )
            uow.commit()
        if statements:  # quotes are chunk text: release them only to a caller who still has access
            self._drafter.confirm_access(ctx, citations)
        return GroundedAnswer(outcome, statements, recorded.record_id, rejection)

    def _check(self, command: GenerateGroundedAnswerCommand) -> None:
        if (
            not isinstance(command, GenerateGroundedAnswerCommand)
            or not isinstance(command.question, Sensitive)
            or not isinstance(command.claims, tuple)
            or not isinstance(command.config_id, str)
            or CONFIG_ID.fullmatch(command.config_id) is None
        ):
            raise ValidationError(codes.INVALID_INPUT, detail="command")

    def _judge(
        self, draft: Draft, evidence: TextEvidence
    ) -> tuple[GenerationOutcome, tuple[AnswerStatement, ...], str | None]:
        """The reply is untrusted: only a reply that passes every check is ever turned into an
        answer. Any contract violation yields `REJECTED` with its stable code and nothing else."""
        try:
            parsed = parse_answer(draft.reply.reveal(), self._policy.contract)
            if parsed.outcome == OUTCOME_INSUFFICIENT:
                return GenerationOutcome.INSUFFICIENT_EVIDENCE, (), None
            verified = verify_statements(parsed, evidence, self._policy.max_markers)
        except ContractViolation as violation:
            return GenerationOutcome.REJECTED, (), violation.code
        statements = tuple(
            AnswerStatement(Sensitive(v.text), Sensitive(v.quote), v.citation) for v in verified
        )
        return GenerationOutcome.ANSWERED, statements, None
