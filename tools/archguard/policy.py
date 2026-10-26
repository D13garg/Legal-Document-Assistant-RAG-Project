"""Single source of truth for architecture policy tables (ADR-025).

Editing this file is an architectural change and triggers the ADR gate.
"""

from __future__ import annotations

from dataclasses import dataclass

SRC_ROOT = "src/lda"
MODULES_ROOT = "src/lda/modules"
SCAN_ROOTS = ("src/lda", "apps", "tests")
INTERNAL_TOPS = frozenset({"lda", "apps", "tests"})

DAG_FILE = "docs/architecture/module-dag.toml"
ADR_DIR = "docs/architecture/ADR"
DECISIONS_LOG = "docs/architecture/decisions-log.md"
EXCEPTIONS_FILE = "architecture-exceptions.toml"
IMPORTLINTER_FILE = ".importlinter"

MODULE_LAYERS = ("domain", "application", "infrastructure", "presentation")
MODULE_SURFACES = ("api", "testing")
MODULE_ALLOWED_FILES = frozenset(
    {"module.toml", "README.md", "api.lock", "config.py", "__init__.py"}
)

# ---- intra-module layer direction: source layer -> layers it may import (same module) ----
SAME_MODULE_ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "application": frozenset({"domain", "application"}),
    "api": frozenset({"domain", "application", "api"}),
    "infrastructure": frozenset({"domain", "application", "infrastructure"}),
    "presentation": frozenset({"application", "presentation"}),
    "testing": frozenset({"domain", "application", "api", "testing"}),
    "config": frozenset({"config"}),
}  # layer "tests" (module-internal tests) may import anything in its own module.

PORTS_ALLOWED_FROM = frozenset({"application", "api", "infrastructure", "testing", "tests"})
PLATFORM_ALLOWED_FROM = frozenset({"infrastructure", "tests"})
CROSS_MODULE_API_FROM = frozenset({"application", "infrastructure", "testing", "tests"})
CROSS_MODULE_TESTING_FROM = frozenset({"testing", "tests"})
BOOTSTRAP_MODULE_LAYERS = frozenset({"api", "infrastructure", "presentation", "config"})
APPS_MODULE_LAYERS = frozenset({"api", "presentation"})

# ---- third-party purity: zones that may import ONLY stdlib + these third-party tops ----
EXTERNAL_ALLOWLIST: dict[str, frozenset[str]] = {
    "kernel": frozenset(),
    "ports": frozenset(),
    "domain": frozenset(),
    "application": frozenset({"pydantic", "typing_extensions"}),
    "api": frozenset({"pydantic", "typing_extensions"}),
    "testing": frozenset({"pydantic", "typing_extensions"}),
    "config": frozenset({"pydantic", "typing_extensions"}),
}


@dataclass(frozen=True)
class Restriction:
    rule: str
    patterns: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    why: str


RESTRICTIONS: tuple[Restriction, ...] = (
    Restriction(
        "AG031",
        ("sqlalchemy*", "alembic*", "psycopg*", "asyncpg*", "pgvector*"),
        ("src/lda/modules/*/infrastructure/persistence/*", "src/lda/platform/database/*"),
        "ORM/DB drivers live only in */infrastructure/persistence/** and platform/database/**",
    ),
    Restriction(
        "AG032",
        (
            "openai*",
            "anthropic*",
            "cohere*",
            "voyageai*",
            "mistralai*",
            "google.generativeai*",
            "google.genai*",
            "sentence_transformers*",
            "transformers*",
            "torch*",
        ),
        ("src/lda/modules/ai_gateway/infrastructure/adapters/*",),
        "provider SDKs live only in ai_gateway/infrastructure/adapters/**",
    ),
    Restriction(
        "AG033",
        ("boto3*", "botocore*", "aioboto3*"),
        ("src/lda/platform/storage/*",),
        "object-storage SDKs live only in platform/storage",
    ),
    Restriction(
        "AG033",
        ("redis*",),
        ("src/lda/platform/ratelimit/*",),
        "Redis is used only for rate limiting (platform/ratelimit)",
    ),
    Restriction(
        "AG033",
        ("celery*", "kombu*"),
        (),
        "Celery is not part of the architecture (frozen: Postgres-backed job queue)",
    ),
    Restriction(
        "AG033",
        ("fastapi*", "starlette*", "uvicorn*"),
        ("src/lda/modules/*/presentation/*", "apps/*", "src/lda/platform/*"),
        "web frameworks are delivery mechanisms (presentation/apps/platform only)",
    ),
    Restriction(
        "AG033",
        ("httpx*", "requests*", "aiohttp*", "urllib3*"),
        ("src/lda/modules/*/infrastructure/*", "src/lda/platform/*"),
        "HTTP clients live only in infrastructure/platform",
    ),
    Restriction(
        "AG033",
        ("langchain*", "llama_index*"),
        ("src/lda/modules/*/infrastructure/adapters/*",),
        "LangChain/LlamaIndex are allowed only inside infrastructure adapters",
    ),
    Restriction(
        "AG040",
        ("pydantic_settings*",),
        ("src/lda/platform/configuration/*",),
        "settings loading lives only in platform/configuration",
    ),
)

ENV_ATTRS = frozenset({"getenv", "environ", "environb", "putenv", "unsetenv"})
CONFIG_PATH_PREFIX = "src/lda/platform/configuration/"

# ---- constructors only specific code may call (type-enforced authorization, invariant 1) ----
RESTRICTED_CONSTRUCTORS: dict[str, tuple[str, ...]] = {
    "Authorized": ("src/lda/modules/access_control/",),
    "AuthorizedContext": ("src/lda/modules/access_control/",),
    "AuthorizationProof": ("src/lda/modules/access_control/",),
    "AccessScope": ("src/lda/modules/access_control/",),
    "ContextSegment": (
        "src/lda/modules/grounded_generation/",
        "src/lda/modules/answering/",
        "src/lda/modules/ai_gateway/testing/",
        "src/lda/modules/ai_gateway/tests/",
    ),
}

# ---- design quality ----
BANNED_PATH_NAMES = frozenset(
    {
        "utils",
        "util",
        "helpers",
        "helper",
        "common",
        "misc",
        "services",
        "service",
        "managers",
        "manager",
    }
)
BANNED_CLASS_SUFFIXES = (
    "Manager",
    "Helper",
    "Util",
    "Utils",
    "Service",
    "Orchestrator",
    "Coordinator",
)
MAX_CTOR_PARAMS = 7
MAX_CLASS_LOC = 400
MAX_PORT_METHODS = 12
MAX_INHERITANCE_DEPTH = 2
USE_CASE_PATH_PATTERNS = (
    "src/lda/modules/*/application/use_cases/*",
    "src/lda/modules/*/application/queries/*",
)
AUTHZ_DECORATORS = frozenset({"requires", "public"})
AUDIT_DECORATORS = frozenset({"audited", "not_audited"})
AUDIT_FACT_TYPE = "AuditFact"  # AG056 (ADR-019): what an audited use case hands to audit.record
# ADR-034: markers are class-level, imported from this module, with literal, shape-checked arguments.
# The shape limits mirror lda.shared.kernel.declarations (tests/architecture keeps them equal).
DECLARATIONS_MODULE = "lda.shared.kernel.declarations"
DECLARATIONS_PARENT = "lda.shared.kernel"
DECLARATION_IDENTIFIER_PATTERN = r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*"
DECLARATION_MAX_IDENTIFIER_LENGTH = 128
DECLARATION_MAX_REASON_LENGTH = 200
INHERITANCE_EXCLUDED_BASES = frozenset(
    {
        "Protocol",
        "Generic",
        "ABC",
        "Enum",
        "IntEnum",
        "StrEnum",
        "Flag",
        "NamedTuple",
        "TypedDict",
        "BaseModel",
        "object",
        "Exception",
        "BaseException",
    }
)

# ---- schema isolation / repository instantiation (ADR-027, ADR-013) ----
PERSISTENCE_PATH = "src/lda/modules/*/infrastructure/persistence/*"
REPOSITORY_INSTANTIATION_ALLOWED = (
    "src/lda/modules/*/infrastructure/*unit_of_work*",
    "src/lda/modules/*/testing/*",
)
SCHEMA_SQL_PATTERN = r"(?i)\b(?:from|join|into|update|references|table|view|index)\s+\"?([a-z_][a-z0-9_]*)\"?\.\"?[a-z_]"

NON_WAIVABLE = frozenset({"AG003", "AG090", "AG060", "AG061"})

# ---- ADR governance ----
ADR_REQUIRED_HEADINGS = (
    "Status",
    "Context",
    "Problem",
    "Decision",
    "Rationale",
    "Alternatives considered",
    "Consequences",
    "Security implications",
    "Operational implications",
    "Migration implications",
)
ADR_STATUS_PATTERN = r"(Proposed|Accepted|Deprecated|Superseded by ADR-\d{3})"
ADR_GOVERNED_GLOBS = (
    "src/lda/modules/*/module.toml",
    "src/lda/modules/*/api.lock",
    "src/lda/shared/kernel/*",
    "src/lda/shared/ports/*",
    "tools/archguard/policy.py",
    ".importlinter",
    "architecture-exceptions.toml",
    "docs/architecture/architecture.md",
    "docs/architecture/module-dag.toml",
)
