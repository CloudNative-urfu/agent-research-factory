## Why

Intermittent OpenRouter 502 responses and incomplete streams block otherwise valid campaigns
permanently. Repeated manual restarts rerun the full preflight without durable retry state.
The streaming probe also ignores choice.finish_reason=error.

## What Changes

- Diagnose completion, content size and finish reason without logging raw model content.
- Persist retry deadlines for explicitly transient preflight failures, with bounded frequency.
- Keep invalid capabilities, authentication and unknown failures blocking.
- Recover the legacy preflight pause only when saved diagnostics establish a transient cause.

## Capabilities

### New Capabilities
- `transient-preflight-recovery`: durable retries and honest streaming diagnostics.

### Modified Capabilities

None.

## Impact

Proxy probe, controller preflight, autopilot recovery, CLI status and tests. No provider
switch, fabricated successful stream or skipping required capabilities.
