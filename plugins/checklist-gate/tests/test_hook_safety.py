"""Hook-level tests: gate_check.py is run with the same stdin JSON as the PreToolUse hook."""
import os
import unittest

from tests.hook_harness import (
    CONFIRM_RUN, GATE_CHECK, HookTestCase, SESSION, assert_context, assert_denied,
    assert_passed_silently, bash_call, example_policy, post_call, run_hooks_parallel,
)

# Parallel runs repeat until a non-atomic version of the code would fail them.
PARALLEL_PROCESSES = 4
PARALLEL_ROUNDS = 25


class TestRewrittenCommandIsInert(HookTestCase):

    def make_policy(self) -> dict:
        policy = example_policy()
        policy['ack_items']['docs_checked']['hint'] = 'run `touch HINT_MARKER`'
        return policy

    def test_agent_input_is_not_expanded(self):
        for command in (
            "gate-ack '$(touch MARKER)' --reason xxxxxxxxxxxxxxxxxxxxxxxx",
            "gate-ack user_authorized_push --prompt-id '`touch MARKER`' --reason xxxxxxxxxxxxxxxxxxxxxxxx",
            "gate-toggle off '$(touch MARKER)'",
            "gate-toggle '$(touch MARKER)'",
        ):
            with self.subTest(command=command):
                self.state.record_deny(SESSION, 'git_push_gate')
                output = self.gate_command(command)
                self.assertFalse(os.path.exists(os.path.join(self.data_dir, 'MARKER')))
                self.assertIn('touch MARKER', output)

    def test_policy_text_is_not_expanded(self):
        output = self.gate_command('gate-ack --help-gates')
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, 'HINT_MARKER')))
        self.assertIn('`touch HINT_MARKER`', output)
        self.assertIn('\n=== Gates ===', output)

    def test_rewritten_command_is_a_single_printf(self):
        command = self.shell_command('gate-toggle list')
        self.assertTrue(command.startswith("printf '%s\\n' "))


class TestGateToggleOff(HookTestCase):

    def _off(self, prompt_id=None) -> str:
        suffix = '' if prompt_id is None else f' --prompt-id {prompt_id}'
        return self.gate_command(f'gate-toggle off git_push_gate{suffix}')

    def test_off_requires_user_instruction(self):
        self.add_prompt('push して')
        self.assertIn('Rejected', self._off())
        assert_denied(self.gate('git push'))

    def test_off_with_unrelated_prompt_is_rejected(self):
        for text in (
            'テストを直して', 'オフィスで作業中', '無効な値のエラーを直して', 'git_push_gate の状態を教えて',
        ):
            with self.subTest(text=text):
                self.assertIn('Rejected', self._off(self.add_prompt(text)))

    def test_off_with_gate_word_and_off_word(self):
        for text in (
            'push のゲートを OFF にして', '/checklist-gate:gate-off git_push_gate', 'gate-off して',
        ):
            with self.subTest(text=text):
                self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=True)
                self.assertIn('disabled', self._off(self.add_prompt(text)))

    def test_off_with_negated_prompt_is_rejected(self):
        for text in ('ゲートは off にしないで', 'git_push_gate は外さないで'):
            with self.subTest(text=text):
                self.assertIn('Rejected', self._off(self.add_prompt(text)))

    def test_off_with_prompt_outside_the_recent_ones_is_rejected(self):
        old = self.add_prompt('git_push_gate を一旦外して')
        for _ in range(3):
            self.add_prompt('OK')
        self.assertIn('Rejected', self._off(old))

    def test_off_with_instruction(self):
        pid = self.add_prompt('git_push_gate を一旦外して')
        self.assertIn('disabled', self._off(pid))
        assert_passed_silently(self.gate('git push'))

    def test_on_needs_no_instruction(self):
        self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=False)
        self.assertIn('enabled', self.gate_command('gate-toggle on git_push_gate'))
        assert_denied(self.gate('git push'))


class TestExceptPerSubcommand(HookTestCase):

    def test_excepted_part_does_not_exempt_the_rest(self):
        assert_passed_silently(self.gate('git push --dry-run origin'))
        assert_denied(self.gate('git push --dry-run origin; git push origin master'))
        assert_denied(self.gate('git push --dry-run && git push'))
        assert_passed_silently(self.gate('cat README.md'))
        assert_denied(self.gate('cat README.md && grep -rn secret app'))


class TestAckStacking(HookTestCase):

    def test_one_deny_one_consumable_ack(self):
        assert_denied(self.gate('git commit -m x'))
        self.assertIn('acknowledged', self.gate_command('gate-ack commit_ready --reason xxxxxxxxxxxxxxxxxxxxxxxx'))
        self.assertIn('already acknowledged', self.gate_command('gate-ack commit_ready --reason xxxxxxxxxxxxxxxxxxxxxxxx'))
        assert_passed_silently(self.gate('git commit -m x'))
        assert_denied(self.gate('git commit -m y'))

    def test_stale_prompt_ack_does_not_block_new_ack(self):
        old = self.add_prompt('push して')
        self.state.add_ack_if_allowed(SESSION, 'user_authorized_push', 'old', prompt_id=old)
        for _ in range(3):
            self.add_prompt('OK')
        new = self.add_prompt('push して')
        assert_denied(self.gate('git push'))
        self.assertIn('acknowledged', self.gate_command(
            f'gate-ack user_authorized_push --prompt-id {new} --reason xxxxxxxxxxxxxxxxxxxxxxxx'
        ))
        assert_passed_silently(self.gate('git push'))


class TestCommitAndPushFlow(HookTestCase):

    def _review_done(self) -> None:
        self.state.add_ack_if_allowed(SESSION, 'commit_ready', 'review passed')

    def test_dry_run_commit_does_not_enable_auto_push(self):
        self.add_prompt('push の方法を教えて')
        assert_passed_silently(self.gate('git commit --dry-run'))
        assert_denied(self.gate('git push origin master'))

    def test_chained_commit_and_push_passes_together(self):
        self.add_prompt('commit して push まで')
        self._review_done()
        assert_context(self.gate('git commit -m x && git push'), 'auto-authorized')

    def test_denied_chain_consumes_nothing(self):
        self.add_prompt('commit して')
        self._review_done()
        assert_denied(self.gate('git commit -m x && git push'))
        self.assertTrue(self.state.has_unconsumed_ack(SESSION, 'commit_ready'))

    def test_parallel_calls_use_one_ack_once(self):
        for _ in range(PARALLEL_ROUNDS):
            with self.subTest():
                self._review_done()
                results = run_hooks_parallel(
                    GATE_CHECK, [bash_call('git commit -m x') for _ in range(PARALLEL_PROCESSES)],
                    self.data_dir,
                )
                allowed = [r for r in results if not r.denied]
                self.assertEqual(len(allowed), 1, results)
                self.assertTrue(all(r.stderr == '' for r in results), results)
                self.assertFalse(self.state.has_unconsumed_ack(SESSION, 'commit_ready'))


class TestParallelGateAck(HookTestCase):

    def test_parallel_acks_record_one(self):
        for _ in range(PARALLEL_ROUNDS):
            with self.subTest():
                pid = self.add_prompt('push して')
                self.state.record_deny(SESSION, 'git_commit_gate')
                self.state.record_deny(SESSION, 'git_push_gate')
                ack = 'gate-ack commit_ready --reason xxxxxxxxxxxxxxxxxxxxxxxx'
                push_ack = (
                    f'gate-ack user_authorized_push --prompt-id {pid} --reason xxxxxxxxxxxxxxxxxxxxxxxx'
                )
                for command in (ack, push_ack):
                    calls = [bash_call(command) for _ in range(PARALLEL_PROCESSES)]
                    run_hooks_parallel(GATE_CHECK, calls, self.data_dir)
                    run_hooks_parallel(CONFIRM_RUN, [post_call(c) for c in calls], self.data_dir)
                self.assertEqual(self.unconsumed('commit_ready'), 1)
                self.assertEqual(self.unconsumed('user_authorized_push'), 1)
                self.state.consume_acks(SESSION, ['commit_ready', 'user_authorized_push'])


class TestNudgeOnDeny(HookTestCase):

    def make_policy(self) -> dict:
        policy = example_policy()
        policy['gates'].append({
            'name': 'bash_nudge',
            'trigger': {'patterns': ['Bash(git *)']},
            'message': 'NUDGE',
        })
        return policy

    def test_nudge_is_not_used_up_by_a_denied_call(self):
        self.add_prompt('作業して')
        assert_denied(self.gate('git push'))
        assert_context(self.gate('git status'), 'NUDGE')

    def test_nudge_fires_once_per_turn(self):
        self.add_prompt('作業して')
        assert_context(self.gate('git status'), 'NUDGE')
        assert_passed_silently(self.gate('git status'))
        self.add_prompt('次')
        assert_context(self.gate('git status'), 'NUDGE')


if __name__ == '__main__':
    unittest.main()
