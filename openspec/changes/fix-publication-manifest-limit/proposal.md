## Why

DARP revision 8 completed but publication paused: its 3,077,288-byte source manifest
exceeded the ordinary 2 MB text-file limit. The selected prototype is only about 5.5 MB;
the manifest is required provenance and belongs under the aggregate 64 MiB export budget.

## What Changes

- Preserve source-manifest.json byte for byte up to the aggregate export budget.
- Keep the 2 MB limit for ordinary files.
- Include export-manifest.json itself in the final aggregate size/integrity check.
- Test retry/idempotence and rejected oversized exports; verify revision 8 in a temporary directory.
- Recover only the exact legacy manifest-limit pause after upgrade, with a saved state history.

## Capabilities

### New Capabilities
- `publication-metadata-budget`: bounded provenance export independent of ordinary file limits.

### Modified Capabilities

None.

## Impact

publishing.py, publication regression tests and operator documentation. No source snapshots,
remotes or existing export history are rewritten. Deployment and resuming a paused controller
use a narrowly scoped migration for this legacy error; all other pauses remain unchanged.
