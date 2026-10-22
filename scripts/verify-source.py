"""Verify the byte-identical extraction recorded in the source manifest."""

import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'docs/source-manifest.json').read_text())
failed = []
for relative, record in manifest.items():
    path = root / relative
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
        failed.append(relative)
if failed:
    raise SystemExit('Extraction differs from source: ' + ', '.join(failed))
print(f'Verified {len(manifest)} original files, byte for byte')
