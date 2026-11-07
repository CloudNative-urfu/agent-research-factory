## ADDED Requirements

### Requirement: Frozen opt-in workflow
The factory SHALL freeze the configured development workflow into each new assessment and
SHALL preserve the original workflow when resuming an existing assessment.

#### Scenario: Upgrade during an existing assessment
- **WHEN** configuration enables OpenSpec after assessment inputs were frozen
- **THEN** that assessment retains its original contract and the next new one opts in

### Requirement: Validated approval artifacts
OpenSpec assessments SHALL require proposal, design, delta specs and unchecked tasks with
IDs matching the improvement plan. The factory SHALL run OpenSpec validation and SHALL
reject proceed unless every specified task is independently approved.

#### Scenario: Missing or malformed specification
- **WHEN** an author omits a delta spec or uses unknown task IDs
- **THEN** the assessment cannot advance to an approved successor

### Requirement: Immutable scope and evidence
Successors SHALL retain approved documents unchanged except task completion checkboxes.
Reports and independent critiques SHALL classify every task with existing local evidence.
The factory SHALL reject missing evidence or falsely completed checkboxes for partial tasks.

#### Scenario: Partial implementation
- **WHEN** a task is partial
- **THEN** its checkbox remains unchecked and both report and critique expose its status

#### Scenario: Scope rewritten during implementation
- **WHEN** an approved requirement or task text changes
- **THEN** report validation fails instead of accepting a weakened contract

### Requirement: Backward compatibility and history
Legacy revisions SHALL retain existing validation. OpenSpec histories SHALL be available
to subsequent assessment roles and SHALL not be overwritten by a new change name collision.

#### Scenario: Prior change retained
- **WHEN** a successor starts a new named change
- **THEN** prior change artifacts and residual tasks remain available for planning
