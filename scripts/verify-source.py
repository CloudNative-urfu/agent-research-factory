"""Verify the byte-identical extraction recorded in the source manifest."""

import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'docs/source-manifest.json').read_text())
changes = json.loads((root / 'docs/source-changes.json').read_text())
failed, changed = [], 0
for relative, record in manifest.items():
    path = root / relative
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if digest == changes.get(relative, {}).get('sha256'):
        changed += 1
    elif digest != record['sha256']:
        failed.append(relative)
if failed:
    raise SystemExit('Extraction differs from source and docs/source-changes.json: ' + ', '.join(failed))
print(f'Verified {len(manifest)} original files: {len(manifest) - changed} byte for byte, {changed} by recorded change')
