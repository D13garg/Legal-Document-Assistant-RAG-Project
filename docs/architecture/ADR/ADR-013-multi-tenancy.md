# ADR-013: Multi-tenancy and RLS scope

## Status
Accepted

## Context
Legal content demands defence in depth against cross-tenant leakage.

## Problem
What each isolation layer protects against and what it must not be used for.

## Decision
Shared schema + tenant_id on every tenant table + FORCE ROW LEVEL SECURITY with a tenant policy `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` (fails closed when unset). Tenant is fixed when a TenantTransaction is constructed and applied with transaction-local `set_config` before any statement. RLS covers the tenant boundary only; document ACLs stay in access_control/AccessScope. One sanctioned cross-tenant path exists: a second policy on `platform.jobs` only, enabled by the transaction-local `app.system_scope = 'job_queue'`, used solely by queue maintenance (claim/heartbeat/reclaim/metrics).

## Rationale
RLS catches a forgotten predicate in ORM/raw SQL; it does not stop a wrong-tenant context (confused deputy) or ACL errors, which other layers handle.

## Alternatives considered
App-side filtering only (one missed predicate leaks); database-per-tenant (premium option later); ACLs inside RLS (rejected: duplicated logic, planner cost); a BYPASSRLS worker role (a broader hole than a scoped policy).

## Consequences
Tests prove: unfiltered queries see only the tenant's rows, unset/empty scope sees none, cross-tenant writes are rejected, the runtime role is neither superuser nor BYPASSRLS, and every tenant table has FORCE RLS, a policy and a tenant-leading index. The system scope can be set by any SQL running as the runtime role, so it protects against accidents, not malicious code.

## Security implications
Defence in depth, not the only control.

## Operational implications
Migration role differs from runtime role; workers set tenant per job.

## Migration implications
New tenant tables must pass the introspection test.
