"""`open-brane` command for pip, pipx and uvx installs.

It runs the same brane.py commands as a git checkout, with two differences:
the current Python is used (no extra virtual environment), and data lives in
~/.open-brane unless --config or OPEN_BRANE_HOME says otherwise. The first
`serve`, `run` or `mcp-config` initializes that folder if it is empty.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))  # brane.py imports scripts.*
from . import brane  # noqa: E402


def main():
    args = sys.argv[1:]
    home = Path(os.environ.get('OPEN_BRANE_HOME', Path.home() / '.open-brane')).expanduser()
    config = home / 'starter.json'
    if '--config' not in args:
        args += ['--config', str(config)]
    if '--skip-install' not in args:
        args.append('--skip-install')
    if args and args[0] in {'serve', 'run', 'mcp-config', 'doctor', 'search', 'reindex', 'tasks'} \
            and '--config' not in sys.argv[1:] and not config.exists():
        sys.argv = [sys.argv[0], 'setup', '--config', str(config), '--skip-install']
        # Keep stdout clean: `run` speaks MCP over stdout right after this.
        saved = os.dup(1)
        sys.stdout.flush()
        os.dup2(2, 1)
        try:
            code = _run()
        finally:
            sys.stdout.flush()
            os.dup2(saved, 1)
            os.close(saved)
        if code:
            return code
    sys.argv = [sys.argv[0], *args]
    return _run()


def _run():
    try:
        return brane.main()
    except (OSError, ValueError) as exc:
        print(f'Open Brane: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
