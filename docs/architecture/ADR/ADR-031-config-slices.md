# ADR-031: Config slices with a single loader

## Status
Accepted

## Context
A central Settings class would couple every module.

## Problem
Where configuration is defined and read.

## Decision
Each module defines its own typed slice (`<module>/config.py`, plain pydantic models); platform defines its own in `platform/configuration/settings.py`. One loader reads `LDA_<SECTION>__<FIELD>` variables and validates a slice at a time. Secrets are SecretStr. Errors name fields, never values. Only platform/configuration may touch the environment (AG040).

## Rationale
Cohesion per module and a single audited env reader.

## Alternatives considered
pydantic-settings BaseSettings (extra dependency, not needed); one global Settings (god object).

## Consequences
Map/list values are JSON.

## Security implications
Secrets never echoed.

## Operational implications
Fail fast at startup in bootstrap.

## Migration implications
Add slices as modules appear.

## Amendment (Step 1 hardening)
`ConfigurationError` is raised outside the `except` block so no pydantic error (which holds input values) is reachable through `__context__`. `build_platform` loads and validates every slice before creating any engine or client. Unknown *fields* in a section are rejected (`extra="forbid"`); an unknown *section* (typo in the variable prefix) is still ignored, so an absent optional slice silently takes its defaults. Detecting unused sections needs the final composition (all slices loaded) and is deferred to the first app entrypoint.

## Amendment (Step 3)
`access_control` is the first module with its own slice (`access_control/config.py`). `config` code may import only `pydantic` and itself (AG020/AG030), so it cannot subclass the platform's `SettingsSlice`. `ConfigurationLoader.load` is therefore typed on pydantic `BaseModel` instead of `SettingsSlice`; module slices declare `model_config = ConfigDict(frozen=True, extra="forbid")` themselves. Loading behaviour, error chaining and the no-echo rule are unchanged.
