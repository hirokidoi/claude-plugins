"""Hook-level tests: gate_check.py is run with the same stdin JSON as the PreToolUse hook."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State

GATE_CHECK = os.path.join(PLUGIN_DIR, 'scripts', 'gate_check.py')
SESSION = 'sess-1'

_POLICY = {
    'affirm_keywords': ['OK', 'はい'],
    'ack_items': {
        'docs_checked': {'type': 'session', 'min_reason_length': 10, 'hint': 'run `touch HINT_MARKER`'},
        'review_done': {'type': 'consumable', 'min_reason_length': 10},
        'user_authorized_push': {
            'type': 'user-prompt-match',
            'min_reason_length': 10,
            'match_keywords': ['push'],
            'except_keywords': ['dry-run'],
            'max_prompt_distance': 3,
            'auto_authorize_after_gate': 'git_commit_gate',
        },
    },
    'gates': [
        {
            'name': 'task_start',
            'trigger': {
                'patterns': ['Bash(grep *)', 'Bash(cat *)'],
                'except_patterns': ['Bash(*README*)'],
            },
            'require': ['docs_checked'],
        },
        {
            'name': 'bash_nudge',
            'trigger': {'patterns': ['Bash(git *)'], 'except_patterns': []},
            'require': [],
            'message': 'NUDGE',
        },
        {
            'name': 'git_commit_gate',
            'trigger': {
                'patterns': ['Bash(git commit *)'],
                'except_patterns': ['Bash(git commit --dry-run *)'],
            },
            'require': ['review_done'],
        },
        {
            'name': 'git_push_gate',
            'trigger': {
                'patterns': ['Bash(git push *)'],
                'except_patterns': ['Bash(git push --dry-run *)'],
            },
            'require': ['user_authorized_push'],
        },
    ],
}


class _HookTestBase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        with open(os.path.join(self.data_dir, 'policy.json'), 'w', encoding='utf-8') as f:
            json.dump(_POLICY, f)
        self.state = State(os.path.join(self.data_dir, 'checklist-gate.sqlite'))
        self.state.init_schema()
        self.state.start_session(SESSION, '/repo')

    def tearDown(self) -> None:
        self.state.close()
        self._tmp.cleanup()

    def _env(self) -> dict:
        return {**os.environ, 'CLAUDE_PLUGIN_DATA': self.data_dir}

    def _input(self, command: str) -> str:
        return json.dumps({
            'session_id': SESSION, 'tool_name': 'Bash', 'tool_input': {'command': command},
        })

    def _run(self, command: str) -> dict:
        result = subprocess.run(
            [sys.executable, GATE_CHECK], input=self._input(command),
            capture_output=True, text=True, env=self._env(), check=True,
        )
        self.assertEqual(result.stderr, '')
        output = json.loads(result.stdout) if result.stdout.strip() else {}
        return output.get('hookSpecificOutput', {})

    def assertDenied(self, command: str) -> None:
        self.assertEqual(self._run(command).get('permissionDecision'), 'deny', command)

    def assertAllowed(self, command: str) -> dict:
        out = self._run(command)
        self.assertNotIn('permissionDecision', out, command)
        return out

    def _rewritten(self, command: str) -> str:
        return self._run(command)['updatedInput']['command']

    def _ack(self, command: str) -> str:
        """Run a gate command as Claude Code would: hook rewrite, then bash."""
        result = subprocess.run(
            ['bash', '-c', self._rewritten(command)], capture_output=True, text=True,
            cwd=self.data_dir,
        )
        return result.stdout + result.stderr


class TestRewrittenCommandIsInert(_HookTestBase):

    def test_agent_input_is_not_expanded(self):
        for command in (
            "gate-ack '$(touch MARKER)' --reason xxxxxxxxxxxx",
            "gate-ack user_authorized_push --prompt-id '`touch MARKER`' --reason xxxxxxxxxxxx",
            "gate-toggle off '$(touch MARKER)'",
            "gate-toggle '$(touch MARKER)'",
        ):
            with self.subTest(command=command):
                self.state.record_deny(SESSION, 'git_push_gate')
                output = self._ack(command)
                self.assertFalse(os.path.exists(os.path.join(self.data_dir, 'MARKER')))
                self.assertIn('touch MARKER', output)

    def test_policy_text_is_not_expanded(self):
        output = self._ack('gate-ack --help-gates')
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, 'HINT_MARKER')))
        self.assertIn('`touch HINT_MARKER`', output)
        self.assertIn('\n=== Gates ===', output)


class TestGateToggleOff(_HookTestBase):

    def test_off_requires_user_instruction(self):
        self.state.add_user_prompt(SESSION, 'push して')
        self.assertIn('Rejected', self._ack('gate-toggle off git_push_gate'))
        self.assertDenied('git push')

    def test_off_with_unrelated_prompt_is_rejected(self):
        for text in (
            'テストを直して', 'オフィスで作業中', '無効な値のエラーを直して', 'git_push_gate の状態を教えて',
        ):
            with self.subTest(text=text):
                pid = self.state.add_user_prompt(SESSION, text)
                self.assertIn(
                    'Rejected', self._ack(f'gate-toggle off git_push_gate --prompt-id {pid}')
                )

    def test_off_with_gate_word_and_off_word(self):
        for text in (
            'push のゲートを OFF にして', '/checklist-gate:gate-off git_push_gate', 'gate-off して',
        ):
            with self.subTest(text=text):
                self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=True)
                pid = self.state.add_user_prompt(SESSION, text)
                self.assertIn(
                    'disabled', self._ack(f'gate-toggle off git_push_gate --prompt-id {pid}')
                )

    def test_off_with_negated_prompt_is_rejected(self):
        for text in ('ゲートは off にしないで', 'git_push_gate は外さないで'):
            with self.subTest(text=text):
                pid = self.state.add_user_prompt(SESSION, text)
                self.assertIn(
                    'Rejected', self._ack(f'gate-toggle off git_push_gate --prompt-id {pid}')
                )

    def test_off_with_instruction(self):
        pid = self.state.add_user_prompt(SESSION, 'git_push_gate を一旦外して')
        self.assertIn('disabled', self._ack(f'gate-toggle off git_push_gate --prompt-id {pid}'))
        self.assertAllowed('git push')

    def test_on_needs_no_instruction(self):
        self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=False)
        self.assertIn('enabled', self._ack('gate-toggle on git_push_gate'))
        self.assertDenied('git push')


class TestExceptPerSubcommand(_HookTestBase):

    def test_excepted_part_does_not_exempt_the_rest(self):
        self.assertAllowed('git push --dry-run origin')
        self.assertDenied('git push --dry-run origin; git push origin master')
        self.assertDenied('git push --dry-run && git push')
        self.assertAllowed('cat README.md')
        self.assertDenied('cat README.md && grep -rn secret app')


class TestAckStacking(_HookTestBase):

    def test_one_deny_one_consumable_ack(self):
        self.assertDenied('git commit -m x')
        self.assertIn('acknowledged', self._ack('gate-ack review_done --reason xxxxxxxxxxxx'))
        self.assertIn('already acknowledged', self._ack('gate-ack review_done --reason xxxxxxxxxxxx'))
        self.assertAllowed('git commit -m x')
        self.assertDenied('git commit -m y')

    def test_stale_prompt_ack_does_not_block_new_ack(self):
        old = self.state.add_user_prompt(SESSION, 'push して')
        self.state.add_ack(SESSION, 'user_authorized_push', 'old', prompt_id=old)
        for _ in range(3):
            self.state.add_user_prompt(SESSION, 'OK')
        new = self.state.add_user_prompt(SESSION, 'push して')
        self.assertDenied('git push')
        self.assertIn('acknowledged', self._ack(
            f'gate-ack user_authorized_push --prompt-id {new} --reason xxxxxxxxxxxx'
        ))
        self.assertAllowed('git push')


class TestCommitAndPushFlow(_HookTestBase):

    def test_dry_run_commit_does_not_enable_auto_push(self):
        self.state.add_user_prompt(SESSION, 'push の方法を教えて')
        self.assertAllowed('git commit --dry-run')
        self.assertDenied('git push origin master')

    def test_chained_commit_and_push_passes_together(self):
        self.state.add_user_prompt(SESSION, 'commit して push まで')
        self.state.add_ack(SESSION, 'review_done', 'review passed')
        out = self.assertAllowed('git commit -m x && git push')
        self.assertIn('auto-authorized', out.get('additionalContext', ''))

    def test_denied_chain_consumes_nothing(self):
        self.state.add_user_prompt(SESSION, 'commit して')
        self.state.add_ack(SESSION, 'review_done', 'review passed')
        self.assertDenied('git commit -m x && git push')
        self.assertTrue(self.state.has_unconsumed_ack(SESSION, 'review_done'))

    def test_parallel_calls_use_one_ack_once(self):
        for _ in range(5):
            with self.subTest():
                self.state.add_ack(SESSION, 'review_done', 'review passed')
                procs = [
                    subprocess.Popen(
                        [sys.executable, GATE_CHECK], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env(),
                    )
                    for _ in range(2)
                ]
                for p in procs:
                    p.stdin.write(self._input('git commit -m x'))
                for p in procs:
                    p.stdin.close()
                outputs = [p.stdout.read() for p in procs]
                for p in procs:
                    p.wait()
                    p.stdout.close()
                    p.stderr.close()
                allowed = [o for o in outputs if '"deny"' not in o]
                self.assertEqual(len(allowed), 1, outputs)


class TestParallelGateAck(_HookTestBase):

    def _parallel(self, command: str) -> list:
        procs = [
            subprocess.Popen(
                [sys.executable, GATE_CHECK], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env(),
            )
            for _ in range(2)
        ]
        for p in procs:
            p.stdin.write(self._input(command))
        for p in procs:
            p.stdin.close()
        outputs = [p.stdout.read() for p in procs]
        for p in procs:
            p.wait()
            p.stdout.close()
            p.stderr.close()
        return outputs

    def _unconsumed(self, item: str) -> int:
        conn = self.state._get_conn()
        return conn.execute(
            'SELECT COUNT(*) FROM acks WHERE session_id = ? AND item = ? AND consumed_at IS NULL',
            (SESSION, item),
        ).fetchone()[0]

    def test_parallel_acks_record_one(self):
        for _ in range(20):
            with self.subTest():
                pid = self.state.add_user_prompt(SESSION, 'push して')
                self.state.record_deny(SESSION, 'git_commit_gate')
                self.state.record_deny(SESSION, 'git_push_gate')
                self._parallel('gate-ack review_done --reason xxxxxxxxxxxx')
                self._parallel(
                    f'gate-ack user_authorized_push --prompt-id {pid} --reason xxxxxxxxxxxx'
                )
                self.assertEqual(self._unconsumed('review_done'), 1)
                self.assertEqual(self._unconsumed('user_authorized_push'), 1)
                self.state.consume_acks(SESSION, ['review_done', 'user_authorized_push'])


class TestNudgeOnDeny(_HookTestBase):

    def test_nudge_is_not_used_up_by_a_denied_call(self):
        self.state.add_user_prompt(SESSION, '作業して')
        self.assertDenied('git push')
        out = self.assertAllowed('git status')
        self.assertEqual(out.get('additionalContext'), 'NUDGE')


if __name__ == '__main__':
    unittest.main()
