"""Run persisted BIG-bench categories sequentially for SQLite write safety."""
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
config = json.loads((root / 'backend/reports/p0-live-config.json').read_text())
reports = root / 'backend/reports'
for item in config['sets']:
    name = item['fixture']
    report = reports / f'p0-live-{Path(name).stem}.json'
    if report.exists():
        print('SKIP completed', name, flush=True)
        continue
    log = reports / f'p0-live-{Path(name).stem}.log'
    print('STARTED', name, flush=True)
    with log.open('w') as handle:
        process = subprocess.Popen([sys.executable, str(root / 'scripts/run_p0_live_category.py'), name], cwd=root, env={**os.environ, 'PYTHONPATH': str(root / 'backend')}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in process.stdout:
            handle.write(line)
            handle.flush()
            if line.startswith(('START ', 'PROGRESS ', 'DONE ')):
                print(line.rstrip(), flush=True)
        code = process.wait()
    print('EXIT', name, code, flush=True)
    if code:
        print('\n'.join(log.read_text(errors='replace').splitlines()[-15:]), flush=True)
        sys.exit(code)
