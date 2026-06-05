#!/usr/bin/env python3
"""PostToolUse hook (Agent): check agent result for procedural rule violations."""

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
You are checking whether a subagent followed procedural rules from MEMORY.md.

--- Project rules (MEMORY.md) ---
{memory}

--- Agent task ---
{agent_prompt}

--- Agent result ---
{agent_result}

---
Check ONLY whether the agent followed procedural rules (MEMORY.md).
Do NOT evaluate the quality or correctness of the agent's actual output.

If there is a procedural rule violation, output ONLY:
[天の声] <violation in one sentence>

If no violation: OK

Rules: default to OK when uncertain. No general advice or speculation. \
Focus only on HOW the agent worked, not WHAT it produced.\
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


def _evaluate(memory_content: str, agent_prompt: str, agent_result: str, model: str, timeout: int):
    prompt = EVAL_PROMPT.format(
        memory=memory_content,
        agent_prompt=agent_prompt[:2000],
        agent_result=agent_result[:3000],
    )
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


def _inject_context(message: str) -> None:
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PostToolUse',
            'additionalContext': message,
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
    tool_response = hook_input.get('tool_response', {})

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

    agent_prompt = tool_input.get('prompt', '')

    agent_result = ''
    if isinstance(tool_response, dict):
        content = tool_response.get('content', '')
        if isinstance(content, str):
            agent_result = content
        elif isinstance(content, list):
            agent_result = ' '.join(
                b.get('text', '') for b in content if isinstance(b, dict)
            )

    if not agent_prompt and not agent_result:
        sys.exit(0)

    model = tenno_koe_cfg.get('model', 'haiku')
    timeout = tenno_koe_cfg.get('timeout', 30)

    verdict = _evaluate(memory_content, agent_prompt, agent_result, model, timeout)
    if verdict:
        _inject_context(verdict)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[agent_watcher] Error: {e}', file=sys.stderr)
        sys.exit(0)
