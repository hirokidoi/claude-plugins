"""scripts/validate_policy.py as the apply-config skill runs it."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.hook_harness import HOOK_TIMEOUT_SECONDS, POLICY_EXAMPLE, USER_POLICY, VALIDATE_POLICY, example_policy


def run_cli(*args: str, stdin: str = '', env_extra: dict = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_PLUGIN_DATA'}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, VALIDATE_POLICY, *args], input=stdin, capture_output=True, text=True,
        env=env, timeout=HOOK_TIMEOUT_SECONDS,
    )


class TestValidatePolicyCli(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'p.json')

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, policy) -> str:
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(policy if isinstance(policy, str) else json.dumps(policy))
        return self.path

    def test_bundled_example_is_ok(self):
        proc = run_cli(POLICY_EXAMPLE)
        self.assertEqual((proc.returncode, proc.stdout.strip(), proc.stderr), (0, 'OK', ''))

    @unittest.skipUnless(os.path.isfile(USER_POLICY), 'no user policy.json in this environment')
    def test_user_policy_is_ok(self):
        proc = run_cli(USER_POLICY)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, 'OK'), proc.stdout)

    def test_invalid_file_lists_problems_one_per_line(self):
        policy = example_policy()
        policy['extra'] = 1
        policy['ack_items']['docs_checked']['type'] = 'bad'
        proc = run_cli(self._write(policy))
        self.assertEqual(proc.returncode, 1)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn('OK', proc.stdout)
        self.assertIn('extra', proc.stdout)
        self.assertIn('docs_checked', proc.stdout)

    def test_invalid_json(self):
        proc = run_cli(self._write('{"gates": ['))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('JSON', proc.stdout)

    def test_stdin(self):
        proc = run_cli('-', stdin=json.dumps(example_policy()))
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, 'OK'))
        proc = run_cli('-', stdin='[]')
        self.assertEqual(proc.returncode, 1)

    def test_does_not_need_the_plugin_data_variable(self):
        proc = run_cli('-', stdin='{}')
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, 'OK'))

    def test_usage_errors(self):
        for args in ((), ('a', 'b')):
            with self.subTest(args=args):
                proc = run_cli(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn('usage', proc.stderr)

    def test_unreadable_file_is_a_usage_error(self):
        proc = run_cli(os.path.join(self._tmp.name, 'missing.json'))
        self.assertEqual(proc.returncode, 2)
        self.assertIn('missing.json', proc.stderr)


if __name__ == '__main__':
    unittest.main()
