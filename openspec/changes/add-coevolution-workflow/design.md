## Context

The autopilot already freezes assessment inputs, reviews 1–5 tasks and atomically creates
a successor. We extend that contract only for new opt-in assessments. The existing final
critic reports findings rather than guaranteeing all requirements are implemented.

## Goals / Non-Goals

Goals: real OpenSpec CLI validation; immutable approval scope; local task evidence; truthful
partial outcomes; persisted roadmap; offline regression tests; declarative deployment.
Non-goals: train weights, provision a third agent, change active experiments or claim strong
attacker isolation before the prototype implements and tests it.

## Decisions

1. Freeze `development.json` and the versioned roadmap with assessment inputs. Existing
   assessment markers win on resume, so a config upgrade cannot reinterpret an old approval.
2. Author creates `artifacts/openspec/changes/<change>/` plus `artifacts/openspec-plan.json`.
   Task IDs equal plan IDs; all must be approved for proceed. OpenSpec validates delta syntax;
   factory validates task mapping and file evidence. Neither proves semantic correctness.
3. Copy the approved change into successor `openspec/changes/`; refuse collisions. Keep
   `openspec-contract.json` outside editable work and verify approved document hashes.
   Only checkbox completion in tasks.md may change. Carry prior openspec/ into assessments.
4. Report requires task rows (implemented/partial/blocked), evidence and exact checkbox
   agreement. Partial work remains explicit; it is not silently marked complete. Critic must
   independently classify every task and supply evidence. New plans must inspect residuals.
5. Separate coevolution roadmap from concrete revision scope: foundations/isolation first,
   then persistent experience, then league/evaluation. Arbitrary round counts or mandatory
   successful attacks are forbidden. No experimental budget increase is implied.

## Risks / Trade-offs

OpenSpec structure and hashes constrain artifacts, not truth. Independent critique still
checks claims. CLI is a runtime dependency and must be in packaged PATH. Existing prototypes
with no OpenSpec contract retain old behavior. Persistent experience is a future tested
prototype capability, not shared access to historical internal logs.

## Migration Plan

Add opt-in configuration and run checks/build; owner activates system. New assessments use
the workflow. Existing active revisions finish unchanged. Disable config for future assessments
to stop opting in; already approved revisions keep their frozen contracts.

## Open Questions

Exact process isolation technology and public outcome API are selected by the researcher
under the roadmap's acceptance constraints, then independently reviewed before implementation.
