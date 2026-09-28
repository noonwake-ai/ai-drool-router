#!/usr/bin/env python3
"""Build the static online demo published to GitHub Pages.

The demo is a real build of the dashboard plus a fixed dataset written by
``dev_preview``. It has no backend: ``api/state`` and ``api/runs/<id>`` are
plain files, every control is disabled, and a banner tells the visitor that the
data is synthetic. Nothing here reads a credential or touches a gateway.

    python3 scripts/build_demo.py
    python3 scripts/build_demo.py --check   # rebuild and fail if it changed

Layout produced (served from the repository `Pages` root):
    demo/index.html            built dashboard
    demo/assets/...            hashed JS/CSS
    demo/api/state             dashboard state (extensionless, like the API)
    demo/api/runs/<id>         one record per run
    demo/artifacts/<id>.html   the generated animations
    demo/.nojekyll             publish as-is, no Jekyll processing
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import dev_preview  # noqa: E402

DEMO = ROOT / 'demo'
WEB = ROOT / 'web'

# Injected into the built index.html so the client can show the demo banner and
# refuse every control path.
DEMO_META = '<meta name="drool-demo" content="static-snapshot">'


def build_web():
    """Build the dashboard with relative paths so it works from a subdirectory."""
    if not (WEB / 'node_modules').exists():
        subprocess.run(['npm', 'ci', '--no-audit', '--no-fund'], cwd=WEB, check=True)
    env = {**os.environ, 'DROOL_BASE': './'}
    subprocess.run(['npm', 'run', 'build'], cwd=WEB, env=env, check=True)
    return WEB / 'dist'


def stage(dist):
    """Assemble demo/ from the built dashboard plus a synthetic dataset."""
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)

    for item in dist.iterdir():
        target = DEMO / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    index = DEMO / 'index.html'
    html = index.read_text()
    if DEMO_META not in html:
        html = html.replace('<head>', '<head>\n    ' + DEMO_META, 1)
    index.write_text(html)

    # Controls are off: there is no backend behind the static files.
    snapshot, written = dev_preview.write_dataset(DEMO, controls_enabled=False)
    public = DEMO / 'public'
    api_runs = DEMO / 'api' / 'runs'
    artifacts = DEMO / 'artifacts'
    api_runs.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    (DEMO / 'api' / 'state').write_text((public / 'state.json').read_text())
    for run in public.glob('*.json'):
        (api_runs / run.stem).write_text(run.read_text())
    for art in public.glob('*.html'):
        (artifacts / art.name).write_text(art.read_text())
    shutil.rmtree(public)
    shutil.rmtree(DEMO / 'controls', ignore_errors=True)

    # GitHub Pages must serve `api/state` verbatim rather than running Jekyll.
    (DEMO / '.nojekyll').write_text('')
    return snapshot, written


def digest_tree(root):
    """Stable hash of everything the demo publishes, for the --check mode."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description='Build the static online demo.')
    parser.add_argument('--check', action='store_true',
                        help='rebuild and exit non-zero if the committed demo is stale')
    args = parser.parse_args()

    before = digest_tree(DEMO) if args.check and DEMO.exists() else None
    dist = build_web()
    snapshot, written = stage(dist)
    after = digest_tree(DEMO)

    accounts = len(snapshot['accounts'])
    runs = len(list((DEMO / 'api' / 'runs').iterdir()))
    artifacts = len(list((DEMO / 'artifacts').iterdir()))
    print('demo built: %d accounts, %d runs, %d runs with artwork (%d records staged)'
          % (accounts, runs, artifacts, written), flush=True)

    if args.check:
        if before == after:
            print('committed demo is up to date')
            return 0
        print('committed demo is STALE: re-run python3 scripts/build_demo.py and commit demo/')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
