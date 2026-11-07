"""Writes that loosen a gate take effect only after PostToolUse confirms that the call ran."""
import os
import unittest

from tests.hook_harness import (
    CONFIRM_RUN, DB_NAME, SESSION, HookTestCase, assert_context, assert_denied,
    assert_passed_silently, bash_call, post_call, run_hook,
)

REASON = 'xxxxxxxxxxxxxxxxxxxxxxxx'
ACK = f'gate-ack commit_ready --reason {REASON}'


class TestConfirmRun(HookTestCase):

    def pending_count(self) -> int:
        return self.state._get_conn().execute('SELECT COUNT(*) FROM pending_effects').fetchone()[0]

    def without_tool_use_id(self, command: str) -> str:
        call = bash_call(command)
        del call['tool_use_id']
        result = self.gate(call)
        return result.hook['updatedInput']['command']

    # --- gate-ack ---

    def test_ack_stopped_after_the_hook_has_no_effect(self):
        assert_denied(self.gate('git commit -m x'))
        self.assertIn('acknowledged', self.gate_command(ACK, confirm=False))
        assert_denied(self.gate('git commit -m x'))
        self.assertEqual(self.unconsumed('commit_ready'), 0)

    def test_ack_that_ran_takes_effect(self):
        assert_denied(self.gate('git commit -m x'))
        self.gate_command(ACK)
        assert_passed_silently(self.gate('git commit -m x'))

    def test_session_ack_needs_confirmation_too(self):
        assert_denied(self.gate('ls && cat x'))
        self.gate_command(f'gate-ack docs_checked --reason {REASON}', confirm=False)
        assert_denied(self.gate('ls && cat x'))
        self.gate_command(f'gate-ack docs_checked --reason {REASON}')
        assert_passed_silently(self.gate('ls && cat x'))

    def test_prompt_match_ack_needs_confirmation_too(self):
        pid = self.add_prompt('push して')
        assert_denied(self.gate('git push'))
        command = f'gate-ack user_authorized_push --prompt-id {pid} --reason {REASON}'
        self.gate_command(command, confirm=False)
        assert_denied(self.gate('git push'))
        self.gate_command(command)
        assert_passed_silently(self.gate('git push'))

    def test_confirmation_of_another_call_or_session_does_not_apply(self):
        assert_denied(self.gate('git commit -m x'))
        call = bash_call(ACK)
        self.gate(call)
        self.confirm(bash_call('ls'))
        self.confirm({**call, 'session_id': 'another-session'})
        acks = self.state._get_conn().execute('SELECT COUNT(*) FROM acks').fetchone()[0]
        self.assertEqual(acks, 0)
        self.confirm(call)
        self.assertEqual(self.unconsumed('commit_ready'), 1)

    def test_a_held_write_is_confirmed_once(self):
        assert_denied(self.gate('git commit -m x'))
        call = bash_call(ACK)
        self.gate(call)
        self.confirm(call)
        self.state.consume_acks(SESSION, ['commit_ready'])
        assert_passed_silently(self.confirm(call))
        self.assertEqual(self.unconsumed('commit_ready'), 0)
        self.assertEqual(self.pending_count(), 0)

    def test_second_held_ack_for_the_item_is_not_recorded(self):
        assert_denied(self.gate('git commit -m x'))
        first, second = bash_call(ACK), bash_call(ACK)
        self.gate(first)
        self.gate(second)
        self.confirm(first)
        assert_context(self.confirm(second), 'was not recorded')
        self.assertEqual(self.unconsumed('commit_ready'), 1)

    def test_second_held_ack_for_a_used_prompt_is_not_recorded(self):
        pid = self.add_prompt('push して')
        assert_denied(self.gate('git push'))
        command = f'gate-ack user_authorized_push --prompt-id {pid} --reason {REASON}'
        first, second = bash_call(command), bash_call(command)
        self.gate(first)
        self.gate(second)
        self.confirm(first)
        self.state.consume_acks(SESSION, ['user_authorized_push'])
        assert_context(self.confirm(second), f'prompt_id {pid} has already been used')
        self.assertEqual(self.unconsumed('user_authorized_push'), 0)

    def test_hook_run_twice_for_one_call_holds_one_write(self):
        assert_denied(self.gate('git commit -m x'))
        call = bash_call(ACK)
        self.assertIn('updatedInput', self.gate(call).hook)
        self.assertIn('updatedInput', self.gate(call).hook)
        self.assertEqual(self.pending_count(), 1)

    def test_ack_without_tool_use_id_is_an_error(self):
        assert_denied(self.gate('git commit -m x'))
        self.assertIn('no tool_use_id', self.without_tool_use_id(ACK))
        self.assertEqual(self.pending_count(), 0)

    # --- gate-toggle off ---

    def test_toggle_off_stopped_after_the_hook_keeps_the_gate(self):
        pid = self.add_prompt('git_push_gate を外して')
        command = f'gate-toggle off git_push_gate --prompt-id {pid}'
        self.assertIn('disabled', self.gate_command(command, confirm=False))
        assert_denied(self.gate('git push'))
        self.gate_command(command)
        assert_passed_silently(self.gate('git push'))

    def test_toggle_off_without_tool_use_id_is_an_error(self):
        pid = self.add_prompt('git_push_gate を外して')
        command = self.without_tool_use_id(f'gate-toggle off git_push_gate --prompt-id {pid}')
        self.assertIn('no tool_use_id', command)
        self.assertEqual(self.state.list_gate_toggles(SESSION), [])

    def test_toggle_on_takes_effect_at_once(self):
        self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=False)
        self.gate_command('gate-toggle on git_push_gate', confirm=False)
        assert_denied(self.gate('git push'))

    # --- gate passes for auto-authorization ---

    def test_commit_stopped_after_the_hook_does_not_authorize_push(self):
        self.add_prompt('commit -> push して')
        self.state.add_ack_if_allowed(SESSION, 'commit_ready', 'review passed')
        assert_passed_silently(self.gate('git commit -m x'))
        assert_denied(self.gate('git push'))

    def test_commit_that_ran_authorizes_push(self):
        self.add_prompt('commit -> push して')
        self.state.add_ack_if_allowed(SESSION, 'commit_ready', 'review passed')
        assert_passed_silently(self.run_call('git commit -m x'))
        assert_context(self.gate('git push'), 'auto-authorized')

    def test_commit_without_tool_use_id_does_not_authorize_push(self):
        self.add_prompt('commit -> push して')
        self.state.add_ack_if_allowed(SESSION, 'commit_ready', 'review passed')
        call = bash_call('git commit -m x')
        del call['tool_use_id']
        assert_passed_silently(self.gate(call))
        self.assertEqual(self.pending_count(), 0)

    # --- PostToolUse itself ---

    def test_call_with_nothing_held_passes_silently(self):
        assert_passed_silently(self.confirm(bash_call('ls')))

    def test_broken_database_leaves_writes_unconfirmed_and_exits_0(self):
        assert_denied(self.gate('git commit -m x'))
        call = bash_call(ACK)
        self.gate(call)
        self.state.close()
        for suffix in ('', '-wal', '-shm'):
            path = os.path.join(self.data_dir, DB_NAME + suffix)
            if os.path.exists(path):
                os.remove(path)
        with open(os.path.join(self.data_dir, DB_NAME), 'wb') as f:
            f.write(b'this is not a sqlite database' * 100)
        result = run_hook(CONFIRM_RUN, post_call(call), self.data_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('confirm_run', result.stderr)


if __name__ == '__main__':
    unittest.main()
