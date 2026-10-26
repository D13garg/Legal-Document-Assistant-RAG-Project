"""`SearchIndex`: authorized, ranked, provenance-carrying results (ADR-008). No text, no prompt."""

from __future__ import annotations

from uuid import UUID

from lda.modules.access_control.api import MAX_SCOPE_RESOURCES
from lda.modules.documents.api import DOCUMENT_KIND, READ_DOCUMENT
from lda.modules.retrieval.application.dto import SearchHit, SearchQuery, SearchResult
from lda.modules.retrieval.application.limits import SearchLimits
from lda.modules.retrieval.application.ports import (
    Embedder,
    RetrievalUnitOfWorkFactory,
    VersionStates,
)
from lda.modules.retrieval.domain.embedding import Vector, checked_vector
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.domain.selection import (
    Candidate,
    VersionFact,
    current_version,
    keep_current,
)
from lda.shared.kernel.context import ExecutionContext, SystemContext, TenantContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import AuthorizationError, NotFoundError, ValidationError
from lda.shared.ports.authorization import ProofExpectation, ProofResource, ProofVerifier

SEARCH_QUERY_INVALID = "search_query_invalid"
SEARCH_LIMIT_INVALID = "search_limit_invalid"
SEARCH_SCOPE_INVALID = "search_scope_invalid"
SEARCH_SCOPE_EMPTY = "search_scope_empty"
SEARCH_SCOPE_MISMATCH = "search_scope_mismatch"
SEARCH_REQUIRES_PRINCIPAL = "search_requires_principal"
PURPOSE = "retrieval:search"


@public(
    reason="Authorized by the Access Control proof in the query, verified here against the scope."
)
@not_audited(reason="Read-only query; reads are not audited (ADR-019 D2). Content-free by design.")
class SearchIndex:
    """Order, each step before the next is attempted (fail closed at every step):
    1. validate the request shape and bind it to the caller (tenant, principal);
    2. verify the proof for exactly `documents.read_document` over exactly the scope's resources
       through the established verifier, which also checks expiry and the live policy epoch;
    3. only then embed the query text;
    4. read nearest rows of exactly the scoped documents, page by page, and keep only rows of each
       document's current version, decided live by Documents (`ready`, highest number);
    5. verify the proof once more, so a revocation during the search returns nothing.
    The scope bounds the SQL filter; it is never widened and never cached. Stored version numbers
    or statuses do not exist here: Documents answers them on every search (ADR-008).
    """

    def __init__(
        self,
        units_of_work: RetrievalUnitOfWorkFactory,
        embedder: Embedder,
        verifier: ProofVerifier,
        versions: VersionStates,
        limits: SearchLimits,
    ) -> None:
        self._units_of_work = units_of_work
        self._embedder = embedder
        self._verifier = verifier
        self._versions = versions
        self._limits = limits

    def execute(self, ctx: ExecutionContext, query: SearchQuery) -> SearchResult:
        if not isinstance(ctx, TenantContext):
            raise AuthorizationError(SEARCH_REQUIRES_PRINCIPAL)
        documents = self._validated(ctx, query)
        expected = ProofExpectation(
            tenant_id=query.scope.tenant_id,
            principal_id=query.scope.principal_id,
            action=READ_DOCUMENT,
            resources=frozenset(ProofResource(r.kind, r.id) for r in query.scope.resources),
        )
        self._verifier.verify(query.proof_token, expected)  # before the query reaches anything
        try:
            vector = checked_vector(self._embedder.embed_query(query.text))
        except ValidationError:
            raise ValidationError(SEARCH_QUERY_INVALID) from None
        hits = self._collect(ctx, documents, vector, query.limit)
        self._verifier.verify(query.proof_token, expected)  # still authorized when returning
        return SearchResult(self._embedder.identity.index_id, hits)

    def _validated(self, ctx: TenantContext, query: SearchQuery) -> tuple[DocumentId, ...]:
        scope = query.scope
        if scope.tenant_id != ctx.tenant_id or scope.principal_id != ctx.principal_id:
            raise AuthorizationError(SEARCH_SCOPE_MISMATCH)
        text, limit = query.text, query.limit
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > self._limits.max_query_characters
            or "\x00" in text
            or not isinstance(query.proof_token, str)
            or not query.proof_token
        ):
            raise ValidationError(SEARCH_QUERY_INVALID)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError(SEARCH_LIMIT_INVALID)
        if not 1 <= limit <= self._limits.max_results:
            raise ValidationError(SEARCH_LIMIT_INVALID)
        resources = scope.resources
        if not resources:
            raise ValidationError(SEARCH_SCOPE_EMPTY)
        if len(resources) > MAX_SCOPE_RESOURCES:
            raise ValidationError(SEARCH_SCOPE_INVALID)
        try:
            ids = [UUID(r.id) for r in resources if r.kind == DOCUMENT_KIND]
        except ValueError:
            raise ValidationError(SEARCH_SCOPE_INVALID) from None
        if len(ids) != len(resources):  # a resource of another kind: nothing here can serve it
            raise ValidationError(SEARCH_SCOPE_INVALID)
        return tuple(sorted((DocumentId(i) for i in ids), key=str))

    def _collect(
        self,
        ctx: TenantContext,
        documents: tuple[DocumentId, ...],
        vector: Vector,
        limit: int,
    ) -> tuple[SearchHit, ...]:
        system = SystemContext(ctx.tenant_id, PURPOSE)
        allowed = frozenset(documents)
        index_id = self._embedder.identity.index_id
        decided: dict[DocumentId, VersionFact | None] = {}
        kept: list[Candidate] = []
        offset = 0
        while len(kept) < limit and offset < self._limits.max_candidates:
            size = min(self._limits.candidate_pool, self._limits.max_candidates - offset)
            with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
                page = uow.index.nearest(index_id, documents, vector, size, offset)
                fresh = tuple(
                    {c.document_id for c in page if c.document_id in allowed} - decided.keys()
                )
                known = uow.index.versions_of(sorted(fresh, key=str)) if fresh else {}
            if not page:
                break
            offset += len(page)
            for document_id in fresh:
                decided[document_id] = self._current(
                    system, document_id, known.get(document_id, ())
                )
            kept.extend(keep_current((c for c in page if c.document_id in allowed), decided))
        return tuple(
            SearchHit(
                rank=position,
                document_id=c.document_id,
                version_id=c.version_id,
                version_number=decided[c.document_id].number,  # type: ignore[union-attr]
                chunk_id=c.chunk_id,
                ordinal=c.ordinal,
                score=max(-1.0, min(1.0, c.similarity)),
                text_hash=c.text_hash,
            )
            for position, c in enumerate(kept[:limit], start=1)
        )

    def _current(
        self, system: SystemContext, document_id: DocumentId, versions: tuple[VersionId, ...]
    ) -> VersionFact | None:
        facts: list[VersionFact] = []
        for version_id in versions:
            try:
                fact = self._versions.read(system, document_id, version_id)
            except NotFoundError:
                continue  # tombstoned document or unknown version: never eligible
            if fact is not None:
                facts.append(fact)
        return current_version(facts)
