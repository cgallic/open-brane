#!/usr/bin/env python3
"""Set up and operate Open Brane without activating a virtual environment."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def python_path():
    return ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def initialize(path, business):
    """Create only missing configuration and credentials; never reset an installation."""
    from scripts.runtime_config import load_config
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {
        'version': 1, 'business_id': business, 'owner_id': business,
        'database': 'events.db', 'storage': {'root': '.'},
        'vectors': {'enabled': False}, 'crm': {'provider': 'event', 'options': {}},
    }
    if not path.exists():
        # Validate before creating an unusable installation.
        with path.open('x', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
            f.write('\n')
        try:
            load_config(path)
        except ValueError:
            path.unlink()
            raise
    else:
        load_config(path)
    token = path.parent / 'http-token'
    if not token.exists():
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(32) + '\n')
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('setup', 'serve', 'run', 'doctor', 'mcp-config'))
    parser.add_argument('--config', type=Path, default=ROOT / 'data/starter.json')
    parser.add_argument('--business', default='personal', help='deployment identifier for first setup')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7778)
    parser.add_argument('--skip-install', action='store_true', help='use current Python (containers or managed environments)')
    args = parser.parse_args()
    config = args.config.resolve()
    python = Path(sys.executable) if args.skip_install else python_path()
    if args.command == 'setup':
        if sys.version_info < (3, 11):
            parser.error('Python 3.11 or newer is required')
        if not args.skip_install:
            if not python.exists():
                venv.create(ROOT / '.venv', with_pip=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-core.txt')], check=True)
        token = initialize(config, args.business)
        subprocess.run([str(python), str(ROOT / 'scripts/starter.py'), 'init', '--config', str(config)], check=True)
        print(f'Ready. Start: python3 brane.py serve --config "{config}"', file=sys.stderr)
        print(f'Browser: http://127.0.0.1:7778\nAccess token file: {token}', file=sys.stderr)
        return 0
    if not config.exists():
        parser.error('run python3 brane.py setup first, or supply --config')
    if not python.exists():
        parser.error('Python environment missing; run python3 brane.py setup')
    if args.command == 'mcp-config':
        print(json.dumps({'mcpServers': {'open-brane': {'command': str(python), 'args': [str(ROOT / 'scripts/starter.py'), 'run', '--config', str(config)]}}}, indent=2))
        return 0
    command = [str(python), str(ROOT / 'scripts/starter.py'), args.command, '--config', str(config)]
    if args.command == 'serve':
        command.extend(['--host', args.host, '--port', str(args.port)])
    return subprocess.call(command)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Open Brane: {exc}', file=sys.stderr)
        sys.exit(1)
