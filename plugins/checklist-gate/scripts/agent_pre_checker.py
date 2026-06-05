#!/usr/bin/env python3
"""PreToolUse hook (Agent): check agent launch parameters for procedural violations."""

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State

GATE_NAME = 'tenno_koe'

EVAL_PROMPT = """\
You are checking an agent launch request for critical procedural violations.

--- Project rules (MEMORY.md) ---
{memory}

--- Agent launch parameters ---
{agent_params}

---
If there is a clear critical procedural violation, output ONLY:
[天の声] <violation in one sentence>

If OK: OK

Rules: default to OK when uncertain. No general advice or speculation. \
Flag only clear, critical procedural mistakes based on MEMORY.md rules.\
"""


def _load_policy() -> dict:
    policy_path = os.path.join(os.environ.get('CLAUDE_PLUGIN_DATA', ''), 'policy.json')
    if not os.path.isfile(policy_path):
        return {}
    with open(policy_path, encoding='utf-8') as f:
        return json.load(f)


def _find_memory_path(cwd: str):
    project_key = cwd.replace('/', '-')
    base = Path.home() / '.claude_config' / 'projects' / project_key / 'memory' / 'MEMORY.md'
    if base.exists():
        return base
    alt = Path.home() / '.claude' / 'projects' / project_key / 'memory' / 'MEMORY.md'
    if alt.exists():
        return alt
    return None


def _evaluate(memory_content: str, agent_params_text: str, model: str, timeout: int):
    prompt = EVAL_PROMPT.format(memory=memory_content, agent_params=agent_params_text)
    env = os.environ.copy()
    env['TENNO_KOE_EVALUATING'] = '1'
    try:
        result = subprocess.run(
            ['claude', '-p', prompt, '--model', model, '--no-session-persistence'],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return None


def _deny_response(reason: str) -> None:
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        }
    }))
    sys.exit(0)


def main() -> None:
    if os.environ.get('TENNO_KOE_EVALUATING'):
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    if hook_input.get('tool_name') != 'Agent':
        sys.exit(0)

    try:
        policy = _load_policy()
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    tenno_koe_cfg = policy.get('tenno_koe', {})
    if not tenno_koe_cfg or not tenno_koe_cfg.get('enabled', True):
        sys.exit(0)

    session_id = hook_input.get('session_id', '')
    cwd = hook_input.get('cwd', '')
    tool_input = hook_input.get('tool_input', {})

    state = State()
    state.init_schema()
    gate_enabled = state.is_gate_enabled(session_id, GATE_NAME)
    state.close()
    if not gate_enabled:
        sys.exit(0)

    memory_path = _find_memory_path(cwd)
    if not memory_path:
        sys.exit(0)

    try:
        memory_content = memory_path.read_text(encoding='utf-8')
    except OSError:
        sys.exit(0)

    if not memory_content.strip():
        sys.exit(0)

    agent_params_text = json.dumps(tool_input, ensure_ascii=False, indent=2)
    model = tenno_koe_cfg.get('model', 'haiku')
    timeout = tenno_koe_cfg.get('timeout', 30)

    verdict = _evaluate(memory_content, agent_params_text, model, timeout)
    if verdict:
        cleaned = verdict.strip()
        if cleaned.startswith('[天の声]') and cleaned != '[天の声] OK':
            _deny_response(verdict)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[agent_pre_checker] Error: {e}', file=sys.stderr)
        sys.exit(0)
