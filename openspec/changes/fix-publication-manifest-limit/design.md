## Context

The nested copy helper enforces the same size limit on prototype files and mandatory metadata.
Its current aggregate check runs before export-manifest.json exists.

## Goals / Non-Goals

Allow larger mandatory source manifests while retaining exact hashes and bounded total output.
Do not raise ordinary file limits, split or compact the source manifest, change remotes or
automatically dismiss arbitrary pauses.

## Decisions

Use explicit ordinary-file and aggregate constants. Pass an override only for source-manifest.json,
and verify the copied bytes against the recorded identity hash. Create export-manifest.json before
the final check and include its hash in that check. Reuse the aggregate limit on existing exports.
Atomic staging remains: any failure leaves no destination and no Git commit.

## Risks / Trade-offs

Larger manifests increase Git size, but the aggregate ceiling remains 64 MiB. Previously
published exports remain compatible unless they actually exceed that documented ceiling.
The JSON representation is not rewritten; its existing hash remains verifiable.

## Migration Plan

Run regressions and an isolated export of revision 8. Publish/build the corrected factory.
Owner activates the system; the controller resumes only the exact legacy manifest-size pause
when both autopilot and publication are enabled, preserving previous state in history before
changing it. Other pauses remain unchanged. Existing push recovery reuses any completed commit.
