## ADDED Requirements

### Requirement: Infrastructure priority
New coevolution plans SHALL include a bounded infrastructure task while justified component
gaps remain, or document an evidenced blocking dependency, its task ID and return criterion.

#### Scenario: Repeated harness-only plan
- **WHEN** store infrastructure gaps remain and a plan proposes only harness repairs without a concrete blocker
- **THEN** the independent critic requests revision of the plan

### Requirement: Stateful acceptance
Selected infrastructure tasks SHALL define service boundaries, persistent state, role checks,
legal workflows and failure/retry/restart scenarios rather than unconditional mock success.

#### Scenario: Payment emulator
- **WHEN** a payment slice is proposed
- **THEN** its acceptance includes synthetic state transitions, idempotence and ambiguous timeout recovery without duplicate payment

### Requirement: Incremental and truthful readiness
Progress reports SHALL distinguish implemented infrastructure, in-process models, fixtures
and remaining work. Existing assessments SHALL retain their frozen methodology.

#### Scenario: New roadmap deployed
- **WHEN** version 2 is installed while an older revision is active
- **THEN** the next new assessment receives infrastructure priorities and the active revision keeps its approved scope
