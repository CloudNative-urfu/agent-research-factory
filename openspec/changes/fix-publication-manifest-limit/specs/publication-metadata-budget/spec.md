## ADDED Requirements

### Requirement: Preserve large provenance manifests
Publication SHALL preserve source-manifest.json byte for byte when it exceeds the ordinary
text-file limit but the complete export fits within the aggregate budget.

#### Scenario: Manifest larger than 2 MB
- **WHEN** a completed revision has a source manifest larger than 2 MB and the export fits
- **THEN** publication exports that manifest unchanged with its original hash

### Requirement: Bound all exported bytes
The aggregate export limit SHALL include source-manifest.json, prototype files, reports and
export-manifest.json. Ordinary prototype files SHALL retain their existing per-file limit.

#### Scenario: Metadata pushes export over budget
- **WHEN** the export exceeds the aggregate budget only after export-manifest.json is added
- **THEN** publication fails before the destination is installed or Git is committed

### Requirement: Safe retry
Repeating a successful export SHALL verify and reuse it without rewriting source evidence.

#### Scenario: Retry after export installation
- **WHEN** export is retried for the same completed revision
- **THEN** the existing manifest hash is returned after integrity and size verification

### Requirement: Narrow legacy pause recovery
The upgraded controller SHALL recover the exact legacy source-manifest text-limit pause
only when autopilot and publication are enabled, preserving previous state before retry.

#### Scenario: Legacy limit failure
- **WHEN** the paused reason is exactly Source manifest exceeds text export limit
- **THEN** enabled publication is retried and previous state is saved in recovery history

#### Scenario: Unrelated pause or disabled publication
- **WHEN** the reason differs or publication is disabled
- **THEN** the controller leaves the pause unchanged
