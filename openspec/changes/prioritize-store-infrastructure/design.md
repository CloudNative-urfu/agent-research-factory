## Context

The opt-in workflow already freezes the roadmap, sends it to both assessment roles and
copies it into the successor brief. Existing coevolution-progress artifacts persist between
revisions. Use these paths rather than create a second scheduler or another JSON contract.

## Goals / Non-Goals

Goals: explicit infrastructure priority, bounded acceptance tests, meaningful fault emulation,
honest component readiness and continued attack/defence isolation.
Non-goals: implement the prototype in the factory, mandate PostgreSQL/microservices, require
real payments or change active campaigns.

## Decisions

Add track I independently of A/B/C so service work is not indefinitely postponed until
perfect isolation. Require an infrastructure slice or a concrete evidenced dependency when
gaps remain. Critic judges substance; textual presence cannot prove useful progress.
Preserve existing database and tools where possible. State contracts, transitions, role checks,
failures and independent outcome evidence determine fidelity, not the brand of a database.
Keep payment and logistics local and synthetic; model only lifecycle transitions required by
the selected policy. Extend one existing progress report instead of duplicating state.

## Risks / Trade-offs

Semantic review can miss weak excuses; repeated deferrals must be reviewed against prior
progress. Avoid forced feature accumulation after meaningful gaps are closed. New service
surfaces may require versioned agent contracts; do not silently weaken original requirements.

## Migration Plan

Validate OpenSpec and build/check the factory. Publish the factory and update nix-dots input
after permission. Owner activates; the next newly created assessment freezes version 2.
Previously approved revisions retain their original scope.
