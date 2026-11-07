"""Abnormal conditions stop calls that a require-gate covers, and let everything else through."""
import ast
import json
import os
import shlex
import subprocess
import sys
import unittest

from tests.hook_harness import (
    DB_NAME, GATE_CHECK, HOOK_TIMEOUT_SECONDS, HookTestCase, PLUGIN_DIR, assert_denied,
    assert_passed_silently, bash_call, example_policy, gate_of, run_hook, write_policy,
)
from state import BUSY_TIMEOUT_SECONDS


class _BrokenPolicyCase(HookTestCase):

    def break_policy(self) -> None:
        policy = example_policy()
        policy['unknown_key'] = 1
        gate_of(policy, 'git_push_gate')['trigger']['patterns'][0] = 'Bash(git push *) '
        write_policy(self.data_dir, policy)


class TestInvalidPolicy(_BrokenPolicyCase):

    def setUp(self):
        super().setUp()
        self.break_policy()

    def test_push_is_denied_with_the_location_of_each_problem(self):
        assert_denied(self.gate('git push'), 'unknown_key', 'git_push_gate', 'trigger.patterns[0]')

    def test_every_tool_except_read_only_ones_is_denied(self):
        assert_denied(self.gate('ls'))
        assert_denied(self.gate_tool('Agent', {'prompt': 'x'}), 'unknown_key')
        assert_denied(self.gate_tool('Edit', {'file_path': '/tmp/a.py'}))
        assert_denied(self.gate_tool('Monitor', {'command': 'ls'}))

    def test_read_only_tools_pass_silently(self):
        for tool_name, tool_input in (
            ('Read', {'file_path': '/tmp/a.py'}), ('Glob', {'pattern': '*.py'}),
            ('Grep', {'pattern': 'x'}), ('Skill', {'skill': 'x'}), ('ToolSearch', {'query': 'x'}),
        ):
            with self.subTest(tool=tool_name):
                assert_passed_silently(self.gate_tool(tool_name, tool_input))

    def test_deny_tells_how_to_recover(self):
        result = self.gate('ls')
        self.assertIn('apply-config', result.reason)

    def test_no_nudge_is_issued(self):
        result = self.gate_tool('Agent', {'prompt': 'x'})
        self.assertTrue(result.denied)
        self.assertNotIn('天の声', result.reason)
        self.assertEqual(result.context, '')

    def test_gate_commands_report_the_problem_instead_of_running(self):
        self.state.record_deny('sess-1', 'git_push_gate')
        output = self.gate_command('gate-ack user_authorized_push --reason xxxxxxxxxxxxxxxxxxxxxxxx')
        self.assertIn('unknown_key', output)
        self.assertIn('invalid', output)
        output = self.gate_command('gate-toggle list')
        self.assertIn('unknown_key', output)

    def test_unreadable_policy_file_is_invalid_too(self):
        with open(os.path.join(self.data_dir, 'policy.json'), 'wb') as f:
            f.write(b'\xff\xfe\x00')
        assert_denied(self.gate('git push'))

    def test_empty_policy_file_is_invalid(self):
        write_policy(self.data_dir, '')
        assert_denied(self.gate('git push'))


class TestMissingPolicy(HookTestCase):

    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.data_dir, 'policy.json'))

    def test_calls_pass_silently(self):
        assert_passed_silently(self.gate('git push'))
        assert_passed_silently(self.gate_tool('Edit', {'file_path': '/tmp/a.py'}))
        assert_passed_silently(self.gate_tool('Agent', {'prompt': 'x'}))

    def test_gate_commands_say_the_policy_is_missing(self):
        self.assertIn('not found', self.gate_command('gate-toggle list'))


class TestDatabaseFailure(HookTestCase):

    def setUp(self):
        super().setUp()
        self.state.close()
        for suffix in ('', '-wal', '-shm'):
            path = os.path.join(self.data_dir, DB_NAME + suffix)
            if os.path.exists(path):
                os.remove(path)
        with open(os.path.join(self.data_dir, DB_NAME), 'wb') as f:
            f.write(b'this is not a sqlite database' * 100)

    def test_require_gate_call_is_denied_with_a_retry_hint(self):
        assert_denied(self.gate('git push'), 'retry')

    def test_commit_gate_call_is_denied_too(self):
        assert_denied(self.gate('git commit -m x'), 'retry')

    def test_nudge_only_call_passes(self):
        assert_passed_silently(self.gate_tool('Agent', {'prompt': 'x'}))

    def test_call_no_gate_applies_to_passes_without_opening_the_database(self):
        assert_passed_silently(self.gate('ls'))
        assert_passed_silently(self.gate('echo hi'))

    def test_gate_command_prints_an_error_and_fails(self):
        result = self.gate('gate-ack docs_checked --reason xxxxxxxxxxxxxxxxxxxxxxxx')
        self.assertEqual(result.returncode, 0)
        proc = subprocess.run(
            ['bash', '-c', result.hook['updatedInput']['command']], capture_output=True, text=True,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Error', proc.stderr)


class TestMalformedInput(HookTestCase):

    def test_stdin_that_is_not_json_is_denied(self):
        assert_denied(run_hook(GATE_CHECK, 'not json at all', self.data_dir))
        assert_denied(run_hook(GATE_CHECK, '', self.data_dir))

    def test_json_that_is_not_an_object_is_denied(self):
        assert_denied(run_hook(GATE_CHECK, '[1, 2]', self.data_dir))
        assert_denied(run_hook(GATE_CHECK, '"x"', self.data_dir))

    def test_tool_input_that_is_not_an_object_is_denied(self):
        assert_denied(run_hook(GATE_CHECK, {'session_id': 's', 'tool_name': 'Bash', 'tool_input': 'ls'}, self.data_dir))

    def test_missing_plugin_data_variable_is_denied(self):
        env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PLUGIN_DATA'}
        proc = subprocess.run(
            [sys.executable, GATE_CHECK], input=json.dumps(bash_call('ls')),
            capture_output=True, text=True, env=env, timeout=HOOK_TIMEOUT_SECONDS,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')


class TestCannotReportTheDecision(HookTestCase):

    def test_exit_code_2_when_stdout_is_closed(self):
        proc = subprocess.run(
            ['sh', '-c', f'{shlex.quote(sys.executable)} {shlex.quote(GATE_CHECK)} >&-'],
            input=json.dumps(bash_call('git push')), text=True, stderr=subprocess.PIPE,
            env={**os.environ, 'CLAUDE_PLUGIN_DATA': self.data_dir}, timeout=HOOK_TIMEOUT_SECONDS,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn('could not report', proc.stderr)

    def test_exit_code_2_when_stdout_is_a_broken_pipe(self):
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                [sys.executable, GATE_CHECK], input=json.dumps(bash_call('git push')).encode(),
                stdout=write_end, stderr=subprocess.PIPE,
                env={**os.environ, 'CLAUDE_PLUGIN_DATA': self.data_dir},
                timeout=HOOK_TIMEOUT_SECONDS,
            )
        finally:
            os.close(write_end)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b'could not report', proc.stderr)


class TestHookDeclaration(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(PLUGIN_DIR, 'hooks', 'hooks.json'), encoding='utf-8') as f:
            self.hooks = json.load(f)['hooks']

    def _entry(self, event: str) -> dict:
        return self.hooks[event][0]['hooks'][0]

    def test_pre_tool_use_blocks_on_failure_and_outlasts_the_database_wait(self):
        entry = self._entry('PreToolUse')
        self.assertEqual(entry['onFailure'], 'block')
        self.assertGreater(entry['timeout'], BUSY_TIMEOUT_SECONDS)

    def test_other_hooks_never_block_on_failure(self):
        for event in ('SessionStart', 'UserPromptSubmit'):
            with self.subTest(event=event):
                self.assertNotIn('onFailure', self._entry(event))


class TestPython39Syntax(unittest.TestCase):

    def _sources(self):
        for folder in ('lib', 'scripts'):
            base = os.path.join(PLUGIN_DIR, folder)
            for name in sorted(os.listdir(base)):
                if name.endswith('.py'):
                    yield os.path.join(base, name)

    def test_files_parse_as_python_39(self):
        for path in self._sources():
            with self.subTest(path=os.path.basename(path)):
                with open(path, encoding='utf-8') as f:
                    ast.parse(f.read(), filename=path, feature_version=(3, 9))

    def test_annotations_do_not_use_union_bars(self):
        for path in self._sources():
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), filename=path)
            annotations = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    annotations.append(node.returns)
                    args = node.args
                    for arg in args.posonlyargs + args.args + args.kwonlyargs + [args.vararg, args.kwarg]:
                        if arg is not None:
                            annotations.append(arg.annotation)
                elif isinstance(node, ast.AnnAssign):
                    annotations.append(node.annotation)
            for annotation in annotations:
                if annotation is None:
                    continue
                for sub in ast.walk(annotation):
                    with self.subTest(path=os.path.basename(path), line=getattr(sub, 'lineno', 0)):
                        self.assertFalse(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr))


if __name__ == '__main__':
    unittest.main()
