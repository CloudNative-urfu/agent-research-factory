## Context

Live doctor checks generation, JSON, tools, streaming and both Claude roles. A failure
currently blocks report even when the next independent probe succeeds.

## Goals / Non-Goals

Preserve genuine failures and retry transient availability errors across reboot. Do not
silently bypass probes or change provider/model. Do not retry arbitrary invalid outputs.

## Decisions

Classify each failed check; retry only when all failures have an explicit transient signature.
Wait 60, 300, then 1800 seconds between full preflights, persisted before sleeping. Total
attempts can continue while the campaign is enabled/running, but never in a busy loop.
Claude quota-specific waiting retains priority. SSE errors and length truncation cannot
pass simply because DONE exists; record finish reason, completion and content length.
Legacy recovery requires the exact preflight error and saved transient-only doctor report;
preserve prior autopilot state before resuming. Other pauses retain existing behavior.

## Risks / Trade-offs

Persistent provider failures can lead to ongoing billable probes at the capped cadence.
Diagnostics expose counts/status, not response bodies or credentials. Unknown transport
errors fail closed. A structurally invalid capability is not repaired by retrying forever.

## Migration Plan

Validate unit/VM checks, publish corrected package and activate. A saved transient preflight
pause resumes automatically, rechecks all capabilities and only then invokes the worker.
