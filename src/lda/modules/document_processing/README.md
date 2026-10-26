# document_processing

> TODO

## Responsibility
TODO: one paragraph. What this module owns and decides.

## Invariants
TODO: the rules this module guarantees (link to architecture.md invariants).

## Public API (`api/`)
TODO: use cases, DTOs, events other modules may use. Snapshot: `api.lock`.

## Dependencies
Declared in `module.toml`; must be a subset of `docs/architecture/module-dag.toml`.

## Owned data
Schema `document_processing` (no cross-schema reads or FKs).

## Events in / out
TODO

## Must NOT know about
TODO

## Testing in isolation
Unit and application tests use only this module's code plus `<dependency>.testing` fakes.
