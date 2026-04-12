#!/usr/bin/env python3
"""UserPromptSubmit hook: Record user prompts for checklist-gate plugin.

Stores each user prompt in the user_prompts table and outputs the
prompt_id via additionalContext so the agent can reference it in
gate-ack --prompt-id.
"""

import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = hook_input.get('session_id', '')
    prompt = hook_input.get('prompt', '')

    if not session_id or not prompt:
        sys.exit(0)

    # --- 1. Record user prompt ---
    state = State()
    state.init_schema()
    try:
        prompt_id = state.add_user_prompt(session_id, prompt)
    finally:
        state.close()

    # --- 2. Output additionalContext with prompt_id ---
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'UserPromptSubmit',
            'additionalContext': f'[checklist-gate: prompt_id={prompt_id}]',
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
        print(f'[prompt_store] Error: {e}', file=sys.stderr)
        sys.exit(0)
