"""Download the model files listed in a manifest (models/manifests/*.json) with size + sha256 checks.

Usage:
    python3 -I tools/download_models.py <manifest.json> <log_dir> [models_base]

- Only entries with tier 추천/선택 and minimax_license_hold=false are fetched (the manifests in
  models/manifests/ contain only those).
- Files go to <models_base>/<folder>/<filename>. models_base defaults to the manifest's
  "comfy_models_base". Any ComfyUI models folder works: ComfyUI finds a model by its category folder
  (diffusion_models, text_encoders, vae, loras, ...) and its file name, not by the full path.
- Safe to re-run: a file that already has the right size and sha256 is skipped; a partial download
  (<name>.part) is resumed with curl -C -.
- Needs python3 and curl. No Hugging Face token is needed for these files.
- The Ollama stage is disabled (user decision 2026-10-21: the Ollama store is on the root disk).
"""
import concurrent.futures as cf
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if len(sys.argv) not in (3, 4):
    sys.exit(__doc__)
manifest = json.load(open(sys.argv[1], encoding='utf-8'))
logdir = Path(sys.argv[2]); logdir.mkdir(parents=True, exist_ok=True)
log = open(logdir / 'download.log', 'a', buffering=1, encoding='utf-8')
base = Path(sys.argv[3] if len(sys.argv) == 4 else manifest['comfy_models_base'])


def say(*a):
    line = time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a)
    print(line, flush=True); log.write(line + '\n')


files = [f for f in manifest['files'] if f['tier'] in ('추천', '선택') and not f.get('minimax_license_hold')]
seen, todo = set(), []
for f in files:
    key = (f['folder'], f['filename'])
    if key not in seen:
        seen.add(key); todo.append(f)
for d in manifest.get('mkdir_before_download', []):
    (base / d).mkdir(parents=True, exist_ok=True)
say(f'models base: {base}')
say(f'files to fetch: {len(todo)}, total {sum(f["size_bytes"] for f in todo)/1e9:.1f} GB')


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch(f):
    dest = base / f['folder'] / f['filename']
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == f['size_bytes']:
        if sha256(dest) == f['sha256']:
            return f'SKIP (already ok) {f["filename"]}'
    part = dest.with_name(dest.name + '.part')
    for attempt in range(1, 6):
        rc = subprocess.run(['curl', '-L', '--fail', '--silent', '--show-error', '--retry', '5', '--retry-delay', '5',
                             '-C', '-', '-o', str(part), f['url']]).returncode
        size = part.stat().st_size if part.exists() else 0
        if rc == 0 and size == f['size_bytes']:
            break
        if size > f['size_bytes']:
            part.unlink()
        say(f'retry {attempt} {f["filename"]} rc={rc} size={size}/{f["size_bytes"]}')
        time.sleep(10)
    else:
        return f'FAIL download {f["filename"]}'
    got = sha256(part)
    if got != f['sha256']:
        part.unlink()
        return f'FAIL sha256 {f["filename"]} got {got[:12]} want {f["sha256"][:12]}'
    os.replace(part, dest)
    return f'OK {f["filename"]} ({f["size_bytes"]/1e9:.2f} GB) -> {f["folder"]}'


results = []
with cf.ThreadPoolExecutor(max_workers=2) as ex:
    futs = {ex.submit(fetch, f): f for f in sorted(todo, key=lambda x: x['size_bytes'])}
    for fu in cf.as_completed(futs):
        try:
            r = fu.result()
        except Exception as e:  # noqa: BLE001
            r = f'FAIL exception {futs[fu]["filename"]}: {e}'
        results.append(r); say(f'[{len(results)}/{len(todo)}]', r)

tags = []  # user decision 2026-10-21: do NOT pull Ollama models (root disk). Pull by hand if ever wanted (MODELS_KO.md 4).
say(f'ollama tags to pull: {len(tags)}')
for t in tags:
    rc = subprocess.run(['ollama', 'pull', t], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    results.append(f'{"OK" if rc.returncode == 0 else "FAIL"} ollama {t}' + ('' if rc.returncode == 0 else f' {rc.stderr.strip()[-200:]}'))
    say(results[-1])

fails = [r for r in results if r.startswith('FAIL')]
say(f'DONE ok={len(results)-len(fails)} fail={len(fails)}')
for r in fails:
    say('  ', r)
sys.exit(1 if fails else 0)
