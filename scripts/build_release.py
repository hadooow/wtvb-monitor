"""Build and smoke-test the Windows x64 portable release on Windows."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.diagnostics import VERSION


def smoke_test(bundle: Path) -> None:
    # Use a copy so the shipping folder contains no test data or settings.
    with tempfile.TemporaryDirectory(prefix='wtvb-smoke-') as temporary:
        test = Path(temporary) / 'WTVB-Monitor'
        shutil.copytree(bundle, test)
        settings_path = test / 'config' / 'settings.json'
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        settings.update(gateway_driver='simulator', host='127.0.0.1', port=port, persist_interval_seconds=1)
        settings_path.write_text(json.dumps(settings), encoding='utf-8')
        process = subprocess.Popen([str(test / 'WTVB-Monitor.exe')], cwd=test,
                                   env={**os.environ, 'WTVB_NO_BROWSER': '1'})
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'Executable exited with {process.returncode}')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/dashboard', timeout=2) as response:
                        dashboard = json.load(response)
                    cbf1 = next(d for d in dashboard['devices'] if d['mac'] == 'C2372102DEEF')
                    if any(d['runtime'].get('collecting') for d in dashboard['devices']):
                        assert cbf1['name'] == 'CBF1-W31'
                        defaults = {d['mac']: d for d in dashboard['devices']}
                        assert defaults['FE6DF407B3E4']['name'] == 'WTVB01-BT50'
                        assert defaults['E8C5C0B8917E']['name'] == 'CBF0-W31'
                        assert 'F8C5C0B8917E' not in defaults
                        for path in ('/', '/static/app.js', '/api/diagnostics/download'):
                            with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
                                data = response.read()
                                assert data
                                if path.endswith('/download'):
                                    assert data.startswith(b'PK')
                        print('Packaged executable smoke test passed')
                        return
                except (OSError, StopIteration):
                    pass
                time.sleep(0.25)
            raise RuntimeError('Executable did not serve simulated samples within 45 seconds')
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log = test / 'logs' / 'monitor.log'
            if log.exists():
                print(log.read_text(encoding='utf-8')[-5000:])


def main() -> None:
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise SystemExit('Build this release using 64-bit Python on Windows.')
    os.chdir(ROOT)
    subprocess.run([sys.executable, '-m', 'pytest', '-q'], check=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
        '--name', 'WTVB-Monitor', '--add-data', f'{ROOT / "app" / "static"};app/static',
        '--collect-submodules', 'uvicorn', '--collect-submodules', 'websockets',
        '--distpath', 'release', '--workpath', 'work/build', '--specpath', 'work/spec',
        'launcher.py',
    ], check=True)
    bundle = ROOT / 'release' / 'WTVB-Monitor'
    (bundle / 'config').mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'config' / 'settings.json', bundle / 'config' / 'settings.json')
    for name in ('README.md', '现场测试说明.md', 'RELEASE_NOTES.md'):
        shutil.copy2(ROOT / name, bundle / name)
    smoke_test(bundle)
    name = f'WTVB-Monitor-v{VERSION}-windows-x64'
    archive = Path(shutil.make_archive(str(ROOT / 'release' / name), 'zip', bundle.parent, bundle.name))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    print(f'Release ready: {archive}')


if __name__ == '__main__':
    main()
