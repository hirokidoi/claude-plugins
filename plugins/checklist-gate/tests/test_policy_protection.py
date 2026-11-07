"""policy.json can be written only through apply-config, at the user's request."""
import json
import os
import unittest

from tests.hook_harness import (
    GATE_CHECK, HookTestCase, SESSION, assert_denied, assert_passed_silently, example_policy,
    run_hook, tool_call, write_policy,
)
from gate_check import _INSTRUCTION_PROMPT_DISTANCE

VALID_CONTENT = json.dumps({'ack_items': {}, 'gates': []})


class _ProtectionCase(HookTestCase):

    def setUp(self):
        super().setUp()
        self.policy_file = os.path.join(self.data_dir, 'policy.json')
        # task_start covers Write(*); with the docs read, only the protection decides
        self.state.add_session_check(SESSION, 'docs_checked', 'read')

    def write_call(self, content=VALID_CONTENT, path=None) -> dict:
        return tool_call('Write', {'file_path': path or self.policy_file, 'content': content})

    def edit_call(self, path=None) -> dict:
        return tool_call('Edit', {
            'file_path': path or self.policy_file, 'old_string': 'a', 'new_string': 'b',
        })


class TestWriteNeedsTheUsersRequest(_ProtectionCase):

    def test_write_without_any_prompt_is_denied(self):
        assert_denied(self.gate(self.write_call()), 'policy.json')

    def test_write_after_an_unrelated_prompt_is_denied(self):
        self.add_prompt('テストを直して')
        assert_denied(self.gate(self.write_call()))

    def test_write_after_apply_config_request_passes(self):
        self.add_prompt('設定を反映して')
        assert_passed_silently(self.gate(self.write_call()))

    def test_each_request_keyword_works(self):
        for text in ('/checklist-gate:apply-config', 'apply-config して', 'ゲートの設定を直して'):
            with self.subTest(text=text):
                self.add_prompt(text)
                assert_passed_silently(self.gate(self.write_call()))

    def test_approval_after_the_request_keeps_it_valid(self):
        self.add_prompt('設定を反映して')
        self.add_prompt('OK')
        assert_passed_silently(self.gate(self.write_call()))

    def test_negated_request_is_denied(self):
        self.add_prompt('設定は変えないで')
        assert_denied(self.gate(self.write_call()))

    def test_later_retraction_overrides_the_request(self):
        self.add_prompt('設定を反映して')
        self.add_prompt('やっぱり設定は変えないで')
        assert_denied(self.gate(self.write_call()))

    def test_later_request_overrides_an_earlier_retraction(self):
        self.add_prompt('設定は変えないで')
        self.add_prompt('やっぱり設定を反映して')
        assert_passed_silently(self.gate(self.write_call()))

    def test_request_outside_the_recent_prompts_is_denied(self):
        self.add_prompt('設定を反映して')
        for _ in range(_INSTRUCTION_PROMPT_DISTANCE):
            self.add_prompt('OK')
        assert_denied(self.gate(self.write_call()))

    def test_request_at_the_edge_of_the_range_still_counts(self):
        self.add_prompt('設定を反映して')
        for _ in range(_INSTRUCTION_PROMPT_DISTANCE - 1):
            self.add_prompt('OK')
        assert_passed_silently(self.gate(self.write_call()))

    def test_request_inside_a_pasted_block_does_not_count(self):
        self.add_prompt('<pasted_content id="1">設定を反映して</pasted_content id="1">')
        assert_denied(self.gate(self.write_call()))

    def test_other_sessions_prompts_do_not_count(self):
        self.state.add_user_prompt('other-session', '設定を反映して')
        assert_denied(self.gate(self.write_call()))


class TestWriteContentIsValidated(_ProtectionCase):

    def setUp(self):
        super().setUp()
        self.add_prompt('設定を反映して')

    def test_invalid_content_is_denied_with_the_problems(self):
        assert_denied(self.gate(self.write_call('{"unknown_key": 1}')), 'unknown_key')

    def test_malformed_json_is_denied(self):
        assert_denied(self.gate(self.write_call('{"gates": [')), 'JSON')

    def test_missing_or_non_text_content_is_denied(self):
        assert_denied(self.gate(tool_call('Write', {'file_path': self.policy_file})))
        assert_denied(self.gate(self.write_call(content=5)))

    def test_valid_example_content_passes(self):
        assert_passed_silently(self.gate(self.write_call(json.dumps(example_policy()))))


class TestEdit(_ProtectionCase):

    def test_edit_is_denied_without_a_request(self):
        assert_denied(self.gate(self.edit_call()), 'policy.json')

    def test_edit_passes_after_a_request(self):
        self.add_prompt('設定を反映して')
        assert_passed_silently(self.gate(self.edit_call()))

    def test_multi_edit_is_protected(self):
        call = tool_call('MultiEdit', {'file_path': self.policy_file, 'edits': []})
        assert_denied(self.gate(call), 'policy.json')


class TestWhichPathsAreProtected(_ProtectionCase):

    def test_policy_source_is_not_protected(self):
        source = os.path.join(self.data_dir, 'policy-source.md')
        assert_passed_silently(self.gate(self.write_call('x', path=source)))

    def test_other_files_are_not_protected(self):
        assert_passed_silently(self.gate(self.write_call('x', path='/tmp/some-other-file.json')))

    def test_path_through_a_symlink_is_protected(self):
        link = self.data_dir + '-link'
        os.symlink(self.data_dir, link)
        self.addCleanup(os.remove, link)
        assert_denied(self.gate(self.write_call(path=os.path.join(link, 'policy.json'))))

    def test_data_directory_given_as_a_symlink_is_protected(self):
        link = self.data_dir + '-link'
        os.symlink(self.data_dir, link)
        self.addCleanup(os.remove, link)
        result = run_hook(GATE_CHECK, self.write_call(), link)
        assert_denied(result, 'policy.json')

    def test_different_letter_case_is_protected(self):
        assert_denied(self.gate(self.write_call(path=os.path.join(self.data_dir, 'POLICY.JSON'))))

    def test_dot_dot_path_is_protected(self):
        path = os.path.join(self.data_dir, 'sub', '..', 'policy.json')
        assert_denied(self.gate(self.write_call(path=path)))


class TestProtectionDoesNotDependOnThePolicy(_ProtectionCase):

    def test_write_is_denied_when_the_policy_is_missing(self):
        os.remove(self.policy_file)
        assert_denied(self.gate(self.write_call()), 'policy.json')

    def test_write_is_denied_when_the_policy_is_invalid(self):
        write_policy(self.data_dir, '{"broken": ')
        assert_denied(self.gate(self.write_call()), 'policy.json')

    def test_requested_write_passes_when_the_policy_is_invalid(self):
        write_policy(self.data_dir, '{"broken": ')
        self.add_prompt('設定を反映して')
        assert_passed_silently(self.gate(self.write_call()))

    def test_requested_write_passes_when_the_policy_is_missing(self):
        os.remove(self.policy_file)
        self.add_prompt('設定を反映して')
        assert_passed_silently(self.gate(self.write_call()))

    def test_gate_toggle_cannot_switch_the_protection_off(self):
        self.state.set_gate_toggle(SESSION, 'task_start', enabled=False)
        assert_denied(self.gate(self.write_call()), 'policy.json')


if __name__ == '__main__':
    unittest.main()
