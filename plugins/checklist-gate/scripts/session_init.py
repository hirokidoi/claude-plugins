#!/usr/bin/env python3
"""SessionStart hook: Initialize session for checklist-gate plugin.

- Creates/opens SQLite DB (WAL mode)
- Creates session row
- Cleans up old data (housekeeping)
- Outputs additionalContext with plugin usage instructions
"""

import json
import os
import sqlite3
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State

TEMPLATE_PATH = os.path.join(PLUGIN_DIR, 'templates', 'session-context.txt')
DATA_DIR = os.environ['CLAUDE_PLUGIN_DATA']


def _init_data_files() -> None:
    """Copy default config files to $CLAUDE_PLUGIN_DATA on first run."""
    source_default = os.path.join(PLUGIN_DIR, 'config', 'policy-source.md')
    source_dest = os.path.join(DATA_DIR, 'policy-source.md')
    if not os.path.isfile(source_dest) and os.path.isfile(source_default):
        import shutil
        shutil.copy2(source_default, source_dest)
        print(
            f'[session_init] Copied default policy-source.md to {source_dest}',
            file=sys.stderr,
        )


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = hook_input.get('session_id', '')
    cwd = hook_input.get('cwd', '')

    # --- 1. Initialize data files ---
    _init_data_files()

    # --- 2. DB initialization ---
    state = State()
    state.init_schema()

    # --- 3. Create session row (skip if already exists on resume) ---
    try:
        state.start_session(session_id, cwd)
    except sqlite3.IntegrityError:
        pass  # Session already exists (resume)

    # --- 4. Housekeeping: remove old data ---
    # Child tables first (7-day retention)
    deleted_gt = state.cleanup_old_gate_toggles(keep_days=7)
    deleted_up = state.cleanup_old_user_prompts(keep_days=7)
    deleted_gd = state.cleanup_old_gate_denies(keep_days=7)
    # Remaining child tables (30-day retention)
    deleted_sc = state.cleanup_old_session_checks(keep_days=30)
    deleted_ak = state.cleanup_old_acks(keep_days=30)
    # Parent table last (30-day retention)
    deleted_ss = state.cleanup_old_sessions(keep_days=30)
    deleted_total = deleted_gt + deleted_up + deleted_gd + deleted_sc + deleted_ak + deleted_ss
    if deleted_total:
        print(
            f'[session_init] Housekeeping: deleted '
            f'{deleted_gt} gate_toggles, {deleted_up} user_prompts, '
            f'{deleted_gd} gate_denies, {deleted_sc} session_checks, '
            f'{deleted_ak} acks, {deleted_ss} sessions',
            file=sys.stderr,
        )

    state.close()

    # --- 5. Output additionalContext ---
    try:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            context_text = f.read().strip()
    except FileNotFoundError:
        context_text = '[checklist-gate plugin enabled]'

    # Replace template placeholders
    context_text = context_text.replace('{plugin_data_dir}', DATA_DIR)

    response = {
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': context_text,
        }
    }
    json.dump(response, sys.stdout)
    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[session_init] Error: {e}', file=sys.stderr)
        sys.exit(0)
