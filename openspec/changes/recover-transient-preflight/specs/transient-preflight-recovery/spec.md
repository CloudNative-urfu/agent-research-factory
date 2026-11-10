## ADDED Requirements

### Requirement: Persist bounded retry cadence
The controller SHALL persist a deadline before waiting after transient-only preflight failure
and SHALL respect that deadline after restart without consuming worker attempts.

#### Scenario: Temporary upstream error
- **WHEN** all failed probes report transient errors such as HTTP 502
- **THEN** the campaign waits and reruns the full preflight at a bounded frequency

### Requirement: Preserve blocking failures
Authentication, invalid tool contracts, malformed responses and missing capabilities SHALL
remain blocking rather than be silently accepted.

#### Scenario: Wrong tool call
- **WHEN** a required probe returns the wrong tool call
- **THEN** the controller blocks without scheduling an availability retry

### Requirement: Honest streaming diagnostics
The stream probe SHALL reject error or length finish reasons and SHALL distinguish DONE,
empty content and finish reason in its diagnostics without storing raw content.

#### Scenario: Error with DONE
- **WHEN** the provider emits content and DONE but a choice ends with error
- **THEN** the stream check fails

### Requirement: Narrow legacy recovery
The autopilot SHALL resume a legacy preflight block only with saved transient-only diagnostics
and SHALL preserve previous state. Arbitrary pauses SHALL remain unchanged.

#### Scenario: Operator pause
- **WHEN** the paused reason is unrelated to preflight availability
- **THEN** the autopilot leaves it paused
