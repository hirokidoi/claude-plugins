"""The deny message explains only what the missing items need."""
import unittest

from tests.hook_harness import HookTestCase, assert_denied, example_policy, write_policy

PROMPT_MATCH_MARK = '--prompt-id'
EXIT_ACK_MARK = 'Confirmed absent'


def _ack_line(item: str) -> str:
    return f'gate-ack {item}'


class TestDenyMessageSections(HookTestCase):

    def test_consumable_gate_has_the_absent_ack_and_no_prompt_match_text(self):
        result = self.gate('git commit -m x')
        assert_denied(result, 'commit_ready', EXIT_ACK_MARK)
        self.assertNotIn(PROMPT_MATCH_MARK, result.reason)
        self.assertNotIn('user-prompt-match', result.reason)
        self.assertNotIn('affirm', result.reason)

    def test_prompt_match_gate_explains_prompt_ids_and_has_no_absent_ack(self):
        result = self.gate('git push origin master')
        assert_denied(result, 'user_authorized_push', PROMPT_MATCH_MARK, '--affirm')
        self.assertNotIn(EXIT_ACK_MARK, result.reason)
        self.assertNotIn('genuinely absent', result.reason)

    def test_prompt_match_example_command_carries_the_prompt_id(self):
        result = self.gate('git push')
        self.assertIn(f'{_ack_line("user_authorized_push")} {PROMPT_MATCH_MARK}', result.reason)

    def test_other_ack_example_command_has_no_prompt_id(self):
        result = self.gate('git commit -m x')
        line = next(l for l in result.reason.splitlines() if _ack_line('commit_ready') in l)
        self.assertNotIn(PROMPT_MATCH_MARK, line)

    def test_session_gate_has_the_absent_ack(self):
        result = self.gate_tool('Read', {'file_path': '/repo/src/a.py'})
        assert_denied(result, 'docs_checked', EXIT_ACK_MARK)
        self.assertNotIn(PROMPT_MATCH_MARK, result.reason)

    def test_gate_name_is_shown(self):
        assert_denied(self.gate('git commit -m x'), 'git_commit_gate')
        assert_denied(self.gate('git push'), 'git_push_gate')


class TestMixedRequirements(HookTestCase):

    def make_policy(self) -> dict:
        policy = example_policy()
        policy['gates'].append({
            'name': 'deploy_gate',
            'trigger': {'patterns': ['Bash(deploy *)']},
            'require': ['docs_checked', 'user_authorized_push'],
        })
        return policy

    def test_absent_ack_lists_only_items_that_can_be_absent(self):
        result = self.gate('deploy now')
        assert_denied(result, 'docs_checked', 'user_authorized_push', PROMPT_MATCH_MARK)
        self.assertIn(f'{_ack_line("docs_checked")} --reason "{EXIT_ACK_MARK}', result.reason)
        self.assertNotIn(f'{_ack_line("user_authorized_push")} --reason "{EXIT_ACK_MARK}', result.reason)

    def test_only_missing_items_are_listed(self):
        self.state.add_session_check('sess-1', 'docs_checked', 'read')
        result = self.gate('deploy now')
        assert_denied(result, 'user_authorized_push', PROMPT_MATCH_MARK)
        self.assertNotIn('docs_checked', result.reason)
        self.assertNotIn(EXIT_ACK_MARK, result.reason)


class TestHints(HookTestCase):

    def _hint(self, item: str) -> str:
        return example_policy()['ack_items'][item]['hint']

    def test_hint_of_the_missing_item_is_shown(self):
        assert_denied(self.gate('git commit -m x'), self._hint('commit_ready'))
        assert_denied(self.gate('git push'), self._hint('user_authorized_push'))

    def test_hints_of_other_items_are_not_shown(self):
        result = self.gate('git commit -m x')
        self.assertNotIn(self._hint('docs_checked'), result.reason)
        self.assertNotIn(self._hint('user_authorized_push'), result.reason)

    def test_item_without_hint_adds_no_hint_line(self):
        policy = example_policy()
        del policy['ack_items']['commit_ready']['hint']
        write_policy(self.data_dir, policy)
        result = self.gate('git commit -m x')
        assert_denied(result, 'commit_ready')
        self.assertNotIn('commit_ready:', result.reason)


if __name__ == '__main__':
    unittest.main()
