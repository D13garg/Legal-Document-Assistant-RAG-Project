"""Public fakes and builders for consumers' tests."""

from lda.modules.access_control.testing.builders import (
    a_document_kind,
    a_resource_registry,
    a_signing_keyring,
    an_access_scope,
)
from lda.modules.access_control.testing.in_memory import (
    InMemoryAccessControlStore,
    InMemoryAccessControlUnitOfWorkFactory,
)

__all__ = [
    "InMemoryAccessControlStore",
    "InMemoryAccessControlUnitOfWorkFactory",
    "a_document_kind",
    "a_resource_registry",
    "a_signing_keyring",
    "an_access_scope",
]
