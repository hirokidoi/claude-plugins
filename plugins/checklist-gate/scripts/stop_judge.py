#!/usr/bin/env python3
"""Stop hook: tenno_koe conversation monitor for checklist-gate.

Evaluates each turn against the project's MEMORY.md using an LLM.
When a violation is detected, records a gate deny and blocks the response.
Claude must run gate-ack tenno_koe_cleared before the session can stop.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State

GATE_NAME = 'tenno_koe'
ACK_ITEM = 'tenno_koe_cleared'

EVAL_PROMPT = """\
Evaluate the assistant's last turn. Evaluate assistant actions only — user messages are out of scope.

--- Project rules (MEMORY.md) ---
{memory}

--- Last turn (assistant actions) ---
{last_turn}

---
If there is a clear rule violation, output ONLY:
[天の声] <violation in one sentence>

If no violation: OK

Rules: default to OK when uncertain. No general advice or speculation. Flag only clear, fact-based violations.

Evaluation scope:
- IN SCOPE: the content or location of what was created/modified violates an explicit project
  prohibition stated in MEMORY.md (e.g. "do not write files to design/", "do not push without
  running tests"). Focus on WHAT was done and WHERE, not HOW the assistant conducted itself.
- OUT OF SCOPE — do NOT flag:
  - Procedural violations: whether the assistant asked for approval before acting, whether
    files were read before editing, communication style, etc. These cannot be corrected
    after the fact and are not actionable by this hook.
  - CLAUDE.md behavioral rules (approval workflow, read-before-edit, etc.): these govern
    how Claude operates, not whether the content of actions violates project-specific rules.
  - General best practices or style issues not explicitly stated as a project prohibition.

Important context about gate-ack:
- `gate-ack` is a pre-declaration required BEFORE any subsequent tool use is allowed.
- Corrective actions (deleting a file, reverting a change, etc.) can only be executed AFTER gate-ack.
- Therefore, a gate-ack reason that says "I will do X to fix this" is legitimate and expected — do NOT flag future corrective intent as a violation.
- Only flag gate-ack reasons that contain fabrication or unjustified inference (e.g., claiming user permission that was never given, asserting facts not in evidence).\
"""


def _load_policy() -> dict:
    policy_path = os.path.join(os.environ.get('CLAUDE_PLUGIN_DATA', ''), 'policy.json')
    if not os.path.isfile(policy_path):
        return {}
    with open(policy_path, encoding='utf-8') as f:
        return json.load(f)


def _read_transcript(transcript_path: str) -> list:
    messages = []
    try:
        with open(transcript_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return messages


def _get_msg_role(msg: dict) -> str:
    """Extract role from either flat or nested (message.role) transcript format."""
    return msg.get('role') or (msg.get('message') or {}).get('role', '')


def _get_msg_content(msg: dict) -> list:
    """Extract content from either flat or nested (message.content) transcript format."""
    content = msg.get('content')
    if content is None:
        content = (msg.get('message') or {}).get('content', [])
    return content if isinstance(content, list) else []


def _get_last_turn_info(messages: list) -> tuple:
    """Return (tools_used: set, last_turn_text: str) for the last assistant turn."""
    tools_used = set()
    lines = []

    last_user_idx = -1
    for i, msg in enumerate(messages):
        if _get_msg_role(msg) == 'user':
            content = _get_msg_content(msg)
            # Skip pure tool-result injections (role=user but not a conversational turn)
            if len(content) > 0 and all(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                continue
            last_user_idx = i

    for msg in messages[last_user_idx + 1:]:
        if _get_msg_role(msg) != 'assistant':
            continue
        content = _get_msg_content(msg)
        if isinstance(content, str):
            first_line = content.strip().split('\n')[0][:100]
            if first_line:
                lines.append(first_line)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get('type', '')
                if btype == 'text':
                    text = block.get('text', '').strip()
                    if text:
                        first_line = text.split('\n')[0][:100]
                        if first_line:
                            lines.append(first_line)
                elif btype == 'tool_use':
                    name = block.get('name', '')
                    tools_used.add(name)
                    inp = block.get('input', {})
                    detail = ''
                    if 'command' in inp:
                        detail = f"command={str(inp['command'])[:120]}"
                    elif 'file_path' in inp:
                        detail = f"file={inp['file_path']}"
                    elif 'path' in inp:
                        detail = f"path={inp['path']}"
                    lines.append(f'[Tool: {name}] {detail}'.strip())

    return tools_used, '\n'.join(lines)


def _find_memory_path(cwd: str):
    project_key = cwd.replace('/', '-')
    base = Path.home() / '.claude_config' / 'projects' / project_key / 'memory' / 'MEMORY.md'
    if base.exists():
        return base
    alt = Path.home() / '.claude' / 'projects' / project_key / 'memory' / 'MEMORY.md'
    if alt.exists():
        return alt
    return None


def _evaluate(memory_content: str, last_turn_text: str, model: str, timeout: int):
    prompt = EVAL_PROMPT.format(memory=memory_content, last_turn=last_turn_text)
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


def _block_response(reason: str) -> None:
    print(json.dumps({'decision': 'block', 'reason': reason}))
    sys.exit(0)


def main() -> None:
    if os.environ.get('TENNO_KOE_EVALUATING'):
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
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
    transcript_path = hook_input.get('transcript_path', '')

    state = State()
    state.init_schema()

    try:
        # Respect gate-toggle off tenno_koe
        if not state.is_gate_enabled(session_id, GATE_NAME):
            return

        # --- 1. Check for pending deny (waiting for ack) ---
        if state.has_deny(session_id, GATE_NAME):
            if state.has_unconsumed_ack(session_id, ACK_ITEM):
                # Acked: consume and clear, let through
                state.consume_oldest_unconsumed_ack(session_id, ACK_ITEM)
                state.clear_deny(session_id, GATE_NAME)
            else:
                # Not acked yet: keep blocking
                hint = tenno_koe_cfg.get('hint', '')
                _block_response(
                    f'[天の声] 前回の指摘への対応を宣言してください。\n'
                    f'gate-ack {ACK_ITEM} --reason "<対応内容>"\n'
                    f'hint: {hint}'
                )
            return

        # --- 2. Filter: only evaluate if mutation tools were used ---
        messages = _read_transcript(transcript_path)
        if not messages:
            return

        watch_tools = set(tenno_koe_cfg.get('watch_tools', []))
        tools_used, last_turn_text = _get_last_turn_info(messages)

        if not (tools_used & watch_tools):
            return

        if not last_turn_text.strip():
            return

        # --- 3. Load MEMORY.md ---
        memory_path = _find_memory_path(cwd)
        if not memory_path:
            return

        try:
            memory_content = memory_path.read_text(encoding='utf-8')
        except OSError:
            return

        if not memory_content.strip():
            return

        # --- 4. Evaluate with LLM ---
        model = tenno_koe_cfg.get('model', 'haiku')
        timeout = tenno_koe_cfg.get('timeout', 30)
        t0 = time.monotonic()
        verdict = _evaluate(memory_content, last_turn_text, model, timeout)
        duration_ms = int((time.monotonic() - t0) * 1000)

        eval_verdict = verdict.strip() if verdict else 'TIMEOUT'
        state.record_tenno_koe_eval(
            session_id, eval_verdict,
            turn_text=last_turn_text,
            duration_ms=duration_ms,
        )

        if verdict:
            cleaned = verdict.strip()
            if cleaned.startswith('[天の声]') and cleaned != '[天の声] OK':
                state.record_deny(session_id, GATE_NAME)
                hint = tenno_koe_cfg.get('hint', '')
                _block_response(
                    f'{verdict}\n\n'
                    f'gate-ack {ACK_ITEM} --reason "<対応内容>" を実行してください。\n'
                    f'hint: {hint}'
                )

    finally:
        state.close()


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[stop_judge] Error: {e}', file=sys.stderr)
        sys.exit(0)
