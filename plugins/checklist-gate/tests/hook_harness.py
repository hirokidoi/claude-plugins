"""Shared helpers: run the real hook scripts against a temporary data directory."""
import copy
import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import List, Optional

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'scripts'))

from state import State, _RETENTION_DAYS

SCRIPTS_DIR = os.path.join(PLUGIN_DIR, 'scripts')
GATE_CHECK = os.path.join(SCRIPTS_DIR, 'gate_check.py')
CONFIRM_RUN = os.path.join(SCRIPTS_DIR, 'confirm_run.py')
PROMPT_STORE = os.path.join(SCRIPTS_DIR, 'prompt_store.py')
SESSION_INIT = os.path.join(SCRIPTS_DIR, 'session_init.py')
VALIDATE_POLICY = os.path.join(SCRIPTS_DIR, 'validate_policy.py')
POLICY_EXAMPLE = os.path.join(PLUGIN_DIR, 'config', 'policy.json.example')
USER_POLICY = os.path.expanduser(
    '~/.claude_config/plugins/data/checklist-gate-doi/policy.json'
)
HOOK_TIMEOUT_SECONDS = 30
SESSION = 'sess-1'
DB_NAME = 'checklist-gate.sqlite'


class HookResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.output = json.loads(stdout) if stdout.strip() else None

    @property
    def hook(self) -> dict:
        return (self.output or {}).get('hookSpecificOutput', {})

    @property
    def denied(self) -> bool:
        return self.hook.get('permissionDecision') == 'deny'

    @property
    def reason(self) -> str:
        return self.hook.get('permissionDecisionReason', '')

    @property
    def context(self) -> str:
        return self.hook.get('additionalContext', '')

    def __repr__(self) -> str:
        return f'HookResult(rc={self.returncode}, stdout={self.stdout!r}, stderr={self.stderr!r})'


def _env(data_dir: str) -> dict:
    return {**os.environ, 'CLAUDE_PLUGIN_DATA': data_dir}


def run_hook(script: str, hook_input, data_dir: str) -> HookResult:
    """Run a hook script with hook_input (dict, or str for raw stdin)."""
    stdin = hook_input if isinstance(hook_input, str) else json.dumps(hook_input)
    proc = subprocess.run(
        [sys.executable, script], input=stdin, capture_output=True, text=True,
        env=_env(data_dir), timeout=HOOK_TIMEOUT_SECONDS,
    )
    return HookResult(proc.returncode, proc.stdout, proc.stderr)


def run_hooks_parallel(script: str, hook_inputs: list, data_dir: str) -> List[HookResult]:
    """Start one process per input, hand them all their input at about the same time."""
    procs = [
        subprocess.Popen(
            [sys.executable, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=_env(data_dir),
        )
        for _ in hook_inputs
    ]
    for proc, hook_input in zip(procs, hook_inputs):
        proc.stdin.write(json.dumps(hook_input))
    for proc in procs:
        proc.stdin.close()
        proc.stdin = None
    results = []
    try:
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=HOOK_TIMEOUT_SECONDS)
            results.append(HookResult(proc.returncode, stdout, stderr))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    return results


_tool_use_ids = itertools.count(1)


def tool_call(tool_name: str, tool_input: dict, session_id: str = SESSION) -> dict:
    """A PreToolUse input with a tool_use_id of its own, as Claude Code sends."""
    return {
        'session_id': session_id, 'tool_name': tool_name, 'tool_input': tool_input,
        'tool_use_id': f'toolu_test_{next(_tool_use_ids)}',
    }


def post_call(call: dict) -> dict:
    """The PostToolUse input Claude Code sends after the call ran without error."""
    return {**call, 'hook_event_name': 'PostToolUse', 'tool_response': {}}


def bash_call(command: str, session_id: str = SESSION) -> dict:
    return tool_call('Bash', {'command': command}, session_id)


def assert_denied(result: HookResult, *substrings: str) -> None:
    assert result.returncode == 0, result
    assert result.denied, f'expected deny: {result}'
    for s in substrings:
        assert s in result.reason, f'{s!r} not in deny reason: {result.reason}'


def assert_passed_silently(result: HookResult) -> None:
    """Exit 0 with no output at all (a crash that happens to exit 0 would leave stderr)."""
    assert result.returncode == 0, result
    assert result.stdout == '' and result.stderr == '', f'expected silence: {result}'


def assert_context(result: HookResult, substring: str) -> None:
    assert result.returncode == 0 and not result.denied, f'expected a pass with context: {result}'
    assert substring in result.context, f'{substring!r} not in context: {result.context}'


def example_policy() -> dict:
    with open(POLICY_EXAMPLE, encoding='utf-8') as f:
        return json.load(f)


def gate_of(policy: dict, name: str) -> dict:
    return next(g for g in policy['gates'] if g['name'] == name)


def write_policy(data_dir: str, policy) -> str:
    """Write policy (dict, or str for raw text) as policy.json."""
    path = os.path.join(data_dir, 'policy.json')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(policy if isinstance(policy, str) else json.dumps(policy))
    return path


class HookTestCase(unittest.TestCase):
    """A data directory with the bundled example policy, a DB and one session."""

    policy: Optional[dict] = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(self._tmp.name)
        self.policy = self.make_policy()
        write_policy(self.data_dir, self.policy)
        self.state = State(os.path.join(self.data_dir, DB_NAME))
        self.state.init_schema()
        self.state.touch_session(SESSION, '/repo')

    def tearDown(self) -> None:
        self.state.close()
        self._tmp.cleanup()

    def make_policy(self) -> dict:
        return copy.deepcopy(example_policy())

    def gate(self, command_or_call) -> HookResult:
        if isinstance(command_or_call, dict):
            return run_hook(GATE_CHECK, command_or_call, self.data_dir)
        return run_hook(GATE_CHECK, bash_call(command_or_call), self.data_dir)

    def gate_tool(self, tool_name: str, tool_input: dict) -> HookResult:
        return run_hook(GATE_CHECK, tool_call(tool_name, tool_input), self.data_dir)

    def add_prompt(self, text: str) -> int:
        return self.state.add_user_prompt(SESSION, text)

    def confirm(self, call: dict) -> HookResult:
        """PostToolUse for a call that ran without error."""
        return run_hook(CONFIRM_RUN, post_call(call), self.data_dir)

    def run_call(self, command_or_call) -> HookResult:
        """A call that the hook let through and that then ran: PreToolUse, then PostToolUse."""
        call = command_or_call if isinstance(command_or_call, dict) else bash_call(command_or_call)
        result = self.gate(call)
        if not result.denied:
            confirmed = self.confirm(call)
            assert confirmed.returncode == 0 and confirmed.stderr == '', confirmed
        return result

    def shell_command(self, command: str) -> str:
        """The command after the hook rewrote a gate-ack / gate-toggle call."""
        result = self.gate(command)
        assert result.returncode == 0 and result.stderr == '', result
        return result.hook['updatedInput']['command']

    def gate_command(self, command: str, confirm: bool = True) -> str:
        """Run a gate-ack / gate-toggle command as Claude Code does: hook rewrite, bash, then
        PostToolUse when bash exited 0 (confirm=False: the call was stopped after the hook)."""
        call = bash_call(command)
        result = self.gate(call)
        assert result.returncode == 0 and result.stderr == '', result
        proc = subprocess.run(
            ['bash', '-c', result.hook['updatedInput']['command']], capture_output=True,
            text=True, cwd=self.data_dir, timeout=HOOK_TIMEOUT_SECONDS,
        )
        output = proc.stdout + proc.stderr
        if confirm and proc.returncode == 0:
            confirmed = self.confirm(call)
            assert confirmed.returncode == 0 and confirmed.stderr == '', confirmed
            output += confirmed.context
        return output

    def deny_then_ack(self, gate_name: str, command: str, item: str, **ack_args) -> str:
        """Provoke a deny (deny-first), then ack the item."""
        assert_denied(self.gate(command), gate_name)
        args = ''.join(f' --{k.replace("_", "-")} {v}' for k, v in ack_args.items())
        return self.gate_command(f'gate-ack {item}{args} --reason xxxxxxxxxxxxxxxxxxxxxxxx')

    def unconsumed(self, item: str) -> int:
        row = self.state._get_conn().execute(
            'SELECT COUNT(*) FROM acks WHERE session_id = ? AND item = ? AND consumed_at IS NULL',
            (SESSION, item),
        ).fetchone()
        return row[0]


# --- DB rows for housekeeping tests ---

AGED_TIME = '2000-01-01T00:00:00+00:00'
TABLES = tuple(sorted(_RETENTION_DAYS))


def seed_session(state: State, session_id: str, started_at: str, last_active_at: Optional[str] = None) -> None:
    state._get_conn().execute(
        'INSERT INTO sessions (session_id, started_at, cwd, last_active_at) VALUES (?, ?, ?, ?)',
        (session_id, started_at, '/repo', last_active_at),
    )
    state._get_conn().commit()


def seed_row(state: State, table: str, session_id: str, timestamp: str) -> None:
    """One row in table for the session; tables without a time column ignore the timestamp."""
    sql = {
        'user_prompts': ('(session_id, prompt, created_at)', (session_id, 'p', timestamp)),
        'gate_toggles': ('(session_id, gate_name, enabled, updated_at)', (session_id, 'g', 1, timestamp)),
        'gate_denies': ('(session_id, gate_name, created_at)', (session_id, 'g', timestamp)),
        'session_checks': ('(session_id, item, reason, checked_at)', (session_id, 'i', 'r', timestamp)),
        'acks': ('(session_id, item, reason, created_at)', (session_id, 'i', 'r', timestamp)),
        'nudge_firings': ('(session_id, nudge_name, prompt_id)', (session_id, 'n', 1)),
        'gate_passes': ('(session_id, gate_name, prompt_id)', (session_id, 'g', 1)),
        'pending_effects': (
            '(session_id, tool_use_id, kind, name, payload, created_at)',
            (session_id, 't', 'k', 'n', '{}', timestamp),
        ),
    }[table]
    columns, values = sql
    marks = ', '.join('?' for _ in values)
    state._get_conn().execute(f'INSERT INTO {table} {columns} VALUES ({marks})', values)
    state._get_conn().commit()


def seed_all_rows(state: State, session_id: str, timestamp: str) -> None:
    for table in TABLES:
        seed_row(state, table, session_id, timestamp)


def row_counts(state: State, session_id: str) -> dict:
    conn = state._get_conn()
    counts = {
        table: conn.execute(
            f'SELECT COUNT(*) FROM {table} WHERE session_id = ?', (session_id,)
        ).fetchone()[0]
        for table in TABLES
    }
    counts['sessions'] = conn.execute(
        'SELECT COUNT(*) FROM sessions WHERE session_id = ?', (session_id,)
    ).fetchone()[0]
    return counts
