# Development operator bootstrap (ADR-043) — DEVELOPMENT ONLY

**Not authentication.** It exists so a local, headless run can obtain a tenant, a principal and a
first `documents.read_document` grant through the real `RegisterPrincipal` / `GrantAccess` use
cases, audited as system work (`actor` empty, `purpose = dev_operator_bootstrap`).

## Keys (read only through `ConfigurationLoader`, `LDA_<SECTION>__<FIELD>`)

| Variable | Required | Meaning |
|---|---|---|
| `LDA_DEV_OPERATOR__ENVIRONMENT` | yes, no default | must be exactly `development`; anything else refuses |
| `LDA_DEV_OPERATOR__TENANT_ID` | yes | tenant UUID you choose (you are the trust root) |
| `LDA_DEV_OPERATOR__PRINCIPAL_ID` | no | an already-registered principal of that tenant |
| `LDA_DEV_OPERATOR__DISPLAY_NAME` | no | name used when registering (never printed or audited) |
| `LDA_DATABASE__URL` | yes | **runtime role** URL; host must be loopback or a Unix socket |
| `LDA_ACCESS_CONTROL__PROOF_KEY_ID`, `LDA_ACCESS_CONTROL__PROOF_KEY` | yes | existing signing slice |
| `LDA_OBJECT_STORAGE__BUCKET`, `LDA_JOBS__QUEUES` | yes | required by `build_platform` |

The admin database URL is never read by this command.

## Run (after the schema is migrated by the admin role)

    python -m apps.dev_bootstrap

First run (no `PRINCIPAL_ID`): registers a principal and prints its id; put it in
`LDA_DEV_OPERATOR__PRINCIPAL_ID`. Later runs verify it and write nothing. Exit codes: `0` ok,
`1` failed after start-up, `2` refused at start-up (guard, configuration). A run without a
recorded id registers another (grant-less) principal; Identity has no lookup to avoid that.

## Limits
Not production authentication; replace when an authenticated API layer defines tenant identity.
The per-document grant is issued by `DevOperator.grant_first_read(resource_id)` from the
development slice driver (`python -m apps.dev_slice`, ADR-045) after the upload.
