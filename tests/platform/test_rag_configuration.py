"""ADR-041 D1, D2: the identity is computed from the real settings and bound at start-up; a
declaration can never override it; the gate fails closed over a real PostgreSQL store (embedded
`pgserver` unless LDA_TEST_POSTGRES_URL is set; not a CI service container)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.answering import build_answering, load_answering_settings
from lda.bootstrap.chunking import load_chunking_settings
from lda.bootstrap.citations import load_evidence_settings
from lda.bootstrap.conversations import load_conversations_settings
from lda.bootstrap.evaluation import load_evaluation_settings
from lda.bootstrap.grounded_generation import load_grounded_generation_settings
from lda.bootstrap.rag_configuration import (
    CONFIG_ID_MISMATCH,
    CONFIGURATION_INVALID,
    RUNTIME_CONFIGURATION_MISMATCH,
    RuntimeConfiguration,
    bind_runtime_configuration,
    effective_configuration,
)
from lda.bootstrap.release_gating import (
    RELEASE_NOT_ELIGIBLE,
    assert_release_eligible,
    build_release_gating,
)
from lda.bootstrap.retrieval import load_index_settings
from lda.modules.evaluation.api import CONFIG_ID
from lda.modules.evaluation.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.evaluation.infrastructure.persistence.store import SqlEvaluationStore
from lda.modules.evaluation.testing import PerfectTarget
from lda.modules.prompts.api import PromptCatalog, PromptTemplate, production_catalog
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConfigurationError
from platform_support import Database, provisioned_database

APPROVED_PROMPT_SHA256 = "2e3a3ce3b2b0a36bf212a4ed7ec3d07ec2f4ebee4f3882694071e48cd64cb94a"
BASE = {
    "LDA_DATABASE__URL": "postgresql+psycopg://user:pw-one@localhost/db",
    "LDA_OBJECT_STORAGE__BUCKET": "bucket",
    "LDA_GROUNDED_GENERATION__MODEL": "fake-model:1",
}


def runtime_for(env: dict[str, str]) -> RuntimeConfiguration:
    config = ConfigurationLoader(env)
    answering = load_answering_settings(config)
    effective = effective_configuration(
        grounded=load_grounded_generation_settings(config),
        index=load_index_settings(config),
        evidence=load_evidence_settings(config),
        chunking=load_chunking_settings(config),
        answering=answering,
    )
    return bind_runtime_configuration(effective, answering.config_id)


def config_id(**env: str) -> str:
    return runtime_for({**BASE, **env}).config_id


def test_the_identity_is_computed_and_has_the_documented_shape() -> None:
    assert CONFIG_ID.fullmatch(config_id())
    assert config_id() == config_id()


def test_the_approved_prompt_hash_is_part_of_the_identity() -> None:
    effective = runtime_for(BASE).effective
    assert (effective.prompt.template_id, effective.prompt.version) == ("grounded_answer", 1)
    assert effective.prompt.content_sha256 == APPROVED_PROMPT_SHA256


def test_the_environment_order_does_not_matter() -> None:
    forward = dict(BASE)
    backward = dict(reversed(list(BASE.items())))
    assert runtime_for(forward).config_id == runtime_for(backward).config_id


@pytest.mark.parametrize(
    "change",
    [
        {"LDA_GROUNDED_GENERATION__MODEL": "other-model:2"},
        {"LDA_GROUNDED_GENERATION__TEMPERATURE": "0.2"},
        {"LDA_GROUNDED_GENERATION__MAX_OUTPUT_TOKENS": "512"},
        {"LDA_GROUNDED_GENERATION__MAX_STATEMENTS": "10"},
        {"LDA_GROUNDED_GENERATION__MIN_QUOTE_CHARACTERS": "20"},
        {"LDA_RETRIEVAL__MAX_RESULTS": "10"},
        {"LDA_RETRIEVAL__CANDIDATE_POOL": "100"},
        {"LDA_CHUNKING__MAX_CHARACTERS": "1500"},
        {"LDA_CHUNKING__MIN_CHARACTERS": "100"},
        {"LDA_CITATIONS__MAX_MARKERS": "100"},
        {"LDA_ANSWERING__MAX_SOURCES": "5"},
        {"LDA_ANSWERING__MAX_CANDIDATES": "50"},
    ],
)
def test_every_behaviour_affecting_setting_changes_the_identity(change: dict[str, str]) -> None:
    assert config_id(**change) != config_id()


@pytest.mark.parametrize(
    "change",
    [
        {"LDA_DATABASE__URL": "postgresql+psycopg://other:pw-two@elsewhere/db2"},
        {"LDA_OBJECT_STORAGE__BUCKET": "another-bucket"},
        {"LDA_AI_GATEWAY__PROVIDER_TIMEOUT_SECONDS": "5"},
        {"LDA_AI_GATEWAY__MAX_ATTEMPTS": "1"},
        {"LDA_RETRIEVAL__EMBEDDING_BATCH_SIZE": "64"},
        {"LDA_EVALUATION__MAX_CASES": "10"},
        {"LDA_PROMPTS__MAX_PARTS": "100"},
    ],
)
def test_operational_deployment_and_secret_settings_do_not_change_the_identity(
    change: dict[str, str],
) -> None:
    assert config_id(**change) == config_id()


def test_credentials_never_appear_in_the_identity_inputs() -> None:
    effective = runtime_for(BASE).effective
    blob = repr(effective.components())
    assert "pw-one" not in blob and "localhost" not in blob and "bucket" not in blob


def test_a_matching_declared_id_starts_and_the_computed_one_is_used() -> None:
    computed = config_id()
    bound = runtime_for({**BASE, "LDA_ANSWERING__CONFIG_ID": computed})
    assert bound.config_id == computed


@pytest.mark.parametrize(
    "declared",
    ["rag-" + "d" * 64, "rag-" + "0" * 64],
)
def test_a_well_formed_but_wrong_declared_id_stops_start_up(declared: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        runtime_for({**BASE, "LDA_ANSWERING__CONFIG_ID": declared})
    assert error.value.args[0] == CONFIG_ID_MISMATCH
    assert declared not in repr(error.value) and config_id() not in repr(error.value)


def test_a_declaration_for_another_configuration_stops_start_up() -> None:
    other = config_id(LDA_RETRIEVAL__MAX_RESULTS="10")
    with pytest.raises(ConfigurationError):
        runtime_for({**BASE, "LDA_ANSWERING__CONFIG_ID": other})


def test_a_runtime_configuration_cannot_carry_a_forged_id() -> None:
    effective = runtime_for(BASE).effective
    with pytest.raises(ConfigurationError) as error:
        RuntimeConfiguration(effective, "rag-" + "a" * 64)
    assert error.value.args[0] == CONFIG_ID_MISMATCH


def test_an_unknown_template_is_a_start_up_error() -> None:
    env = {**BASE, "LDA_GROUNDED_GENERATION__TEMPLATE_VERSION": "9"}
    with pytest.raises(ConfigurationError) as error:
        runtime_for(env)
    assert error.value.args[0] == CONFIGURATION_INVALID


def test_a_changed_template_wording_changes_the_identity() -> None:
    config = ConfigurationLoader(BASE)
    approved = production_catalog().get("grounded_answer", 1)
    edited = PromptCatalog((PromptTemplate("grounded_answer", 1, approved.instructions + " ."),))
    kwargs: dict[str, Any] = {
        "grounded": load_grounded_generation_settings(config),
        "index": load_index_settings(config),
        "evidence": load_evidence_settings(config),
        "chunking": load_chunking_settings(config),
        "answering": load_answering_settings(config),
    }
    assert (
        effective_configuration(**kwargs, catalog=edited).config_id
        != effective_configuration(**kwargs).config_id
    )


def test_an_out_of_range_chunking_limit_fails_start_up_without_its_value() -> None:
    env = {**BASE, "LDA_CHUNKING__MAX_CHARACTERS": "99999999", "LDA_CHUNKING__MIN_CHARACTERS": "5"}
    with pytest.raises(ConfigurationError) as error:
        runtime_for(env)
    assert error.value.args[0] == CONFIGURATION_INVALID and "99999999" not in repr(error.value)


def test_answering_refuses_a_runtime_configuration_computed_from_other_settings() -> None:
    env = {**BASE}
    config = ConfigurationLoader(env)
    other = runtime_for({**BASE, "LDA_ANSWERING__MAX_SOURCES": "5"})
    dummy: Any = object()
    with pytest.raises(ConfigurationError) as error:
        build_answering(
            load_answering_settings(config),
            dummy, dummy, dummy, dummy, dummy,
            index=load_index_settings(config),
            grounded=load_grounded_generation_settings(config),
            evidence=load_evidence_settings(config),
            conversations=load_conversations_settings(config),
            runtime=other,
        )  # fmt: skip
    assert error.value.args[0] == RUNTIME_CONFIGURATION_MISMATCH
    assert "answering" in str(error.value.detail)


# ---- the gate over real settings and a real PostgreSQL store ----------------------------------

START = datetime(2026, 1, 1, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_release", "lda_app_release", [MIGRATIONS_VERSIONS]) as db:
        yield db


@pytest.fixture
def engine(database: Database) -> Iterator[Engine]:
    admin = create_engine(database.admin_url)
    with admin.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM evaluation.release_eligibility"))
        conn.execute(text("DELETE FROM evaluation.evaluation_runs"))
    admin.dispose()
    app = create_engine(database.app_url)
    yield app
    app.dispose()


def test_release_eligibility_is_bound_to_the_exact_running_configuration(engine: Engine) -> None:
    clock = FakeClock(START)
    settings = load_evaluation_settings(ConfigurationLoader(BASE))
    gating = build_release_gating(
        settings, PerfectTarget(), SqlEvaluationStore(engine), clock, SequentialIdGenerator()
    )
    runtime = runtime_for(BASE)

    with pytest.raises(ConfigurationError) as error:  # nothing evaluated yet: closed
        assert_release_eligible(gating, runtime)
    assert error.value.args[0] == RELEASE_NOT_ELIGIBLE

    assert gating.evaluate.execute(runtime.effective).passed
    assert gating.record_eligibility.execute(runtime.effective).decision.eligible
    assert_release_eligible(gating, runtime)  # eligible: no raise

    changed = runtime_for({**BASE, "LDA_GROUNDED_GENERATION__TEMPERATURE": "0.2"})
    assert changed.config_id != runtime.config_id
    with pytest.raises(ConfigurationError):  # prior evidence cannot cover the changed config
        assert_release_eligible(gating, changed)

    clock.advance(timedelta(seconds=settings.max_evidence_age_seconds + 1))
    with pytest.raises(ConfigurationError):  # and evidence expires
        assert_release_eligible(gating, runtime)


def test_an_unreachable_database_is_a_refusal_not_a_pass(database: Database) -> None:
    dead = create_engine(database.app_url.replace("/lda_test_release", "/missing_db"))
    gating = build_release_gating(
        load_evaluation_settings(ConfigurationLoader(BASE)),
        PerfectTarget(),
        SqlEvaluationStore(dead),
        FakeClock(START),
        SequentialIdGenerator(),
    )
    with pytest.raises(ConfigurationError) as error:
        assert_release_eligible(gating, runtime_for(BASE))
    assert error.value.args[0] == RELEASE_NOT_ELIGIBLE and error.value.detail == "check_failed"


def test_the_evaluation_age_setting_is_bounded() -> None:
    for bad in ("59", "2592001", "0"):
        with pytest.raises(ConfigurationError):
            load_evaluation_settings(
                ConfigurationLoader({**BASE, "LDA_EVALUATION__MAX_EVIDENCE_AGE_SECONDS": bad})
            )
    assert load_evaluation_settings(ConfigurationLoader(BASE)).max_evidence_age_seconds == 604_800
