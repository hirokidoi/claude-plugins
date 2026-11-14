"""Pattern matching of gate triggers: Tool(glob) rules, aliases and normalization."""
import unittest

from tests.hook_harness import example_policy, gate_of
from gate_check import _gate_applies


def applies(patterns, tool_name, tool_input, except_patterns=()) -> bool:
    trigger = {'patterns': list(patterns), 'except_patterns': list(except_patterns)}
    return _gate_applies(trigger, tool_name, tool_input)


def bash(command: str) -> dict:
    return {'command': command}


class TestGlob(unittest.TestCase):

    def test_case_is_ignored(self):
        self.assertTrue(applies(['Edit(*/policy.json)'], 'Edit', {'file_path': '/d/POLICY.json'}))
        self.assertTrue(applies(['Bash(git push *)'], 'Bash', bash('GIT PUSH origin')))

    def test_brackets_and_question_mark_are_literal(self):
        pattern = ['Edit(app/[id]/x.ts)']
        self.assertTrue(applies(pattern, 'Edit', {'file_path': 'app/[id]/x.ts'}))
        self.assertFalse(applies(pattern, 'Edit', {'file_path': 'app/d/x.ts'}))
        pattern = ['Edit(a?.txt)']
        self.assertTrue(applies(pattern, 'Edit', {'file_path': 'a?.txt'}))
        self.assertFalse(applies(pattern, 'Edit', {'file_path': 'ab.txt'}))

    def test_star_is_the_only_wildcard_and_crosses_slashes_and_newlines(self):
        self.assertTrue(applies(['Edit(/a/*.py)'], 'Edit', {'file_path': '/a/b/c/d.py'}))
        self.assertTrue(applies(['Bash(echo *)'], 'Bash', bash('echo "a\nb"')))

    def test_pattern_must_match_the_whole_value(self):
        self.assertFalse(applies(['Edit(/a/b)'], 'Edit', {'file_path': '/a/b/c'}))
        self.assertFalse(applies(['Edit(b)'], 'Edit', {'file_path': '/a/b'}))

    def test_trailing_space_star_also_matches_no_arguments(self):
        pattern = ['Bash(git push *)']
        self.assertTrue(applies(pattern, 'Bash', bash('git push')))
        self.assertTrue(applies(pattern, 'Bash', bash('git push origin')))
        self.assertFalse(applies(pattern, 'Bash', bash('git pushx')))

    def test_whole_tool_pattern_ignores_the_value(self):
        self.assertTrue(applies(['Agent(*)'], 'Agent', {'prompt': 'x'}))
        self.assertTrue(applies(['Agent'], 'Agent', {}))
        self.assertTrue(applies(['Bash(*)'], 'Bash', {}))


class TestPathNormalization(unittest.TestCase):

    def test_dot_dot_does_not_hide_the_real_location(self):
        patterns, excepted = ['Edit(*)'], ['Edit(*/tmp/*)']
        self.assertTrue(applies(patterns, 'Edit', {'file_path': '/repo/tmp/../src/a.py'}, excepted))
        self.assertFalse(applies(patterns, 'Edit', {'file_path': '/repo/tmp/a.py'}, excepted))

    def test_dot_dot_cannot_escape_into_an_excepted_name(self):
        patterns, excepted = ['Read(*)'], ['Read(*README*)']
        self.assertTrue(applies(patterns, 'Read', {'file_path': '/repo/src/../a.py'}, excepted))
        self.assertFalse(applies(patterns, 'Read', {'file_path': '/repo/src/../README.md'}, excepted))

    def test_notebook_and_search_path_keys(self):
        self.assertTrue(applies(['Edit(/n/*.ipynb)'], 'NotebookEdit', {'notebook_path': '/n/x.ipynb'}))
        self.assertTrue(applies(['Glob(/src/*)'], 'Glob', {'path': '/src/a'}))

    def test_search_pattern_value_is_matched_as_is(self):
        self.assertTrue(applies(['Grep(*secret*)'], 'Grep', {'pattern': 'my_secret_key'}))
        self.assertFalse(applies(['Grep(*secret*)'], 'Grep', {'pattern': 'other'}))


class TestToolNames(unittest.TestCase):

    def test_wildcard_in_tool_name(self):
        self.assertTrue(applies(['mcp__*(*)'], 'mcp__server__tool', {'x': 1}))
        self.assertTrue(applies(['mcp__*'], 'mcp__server__tool', {}))
        self.assertFalse(applies(['mcp__*(*)'], 'Bash', bash('ls')))
        self.assertTrue(applies(['mcp__srv__*(*)'], 'mcp__srv__a', {}))
        self.assertFalse(applies(['mcp__srv__*(*)'], 'mcp__other__a', {}))

    def test_tool_name_must_match_entirely(self):
        self.assertFalse(applies(['Edit(*)'], 'MultiEdit', {'file_path': '/a'}))
        self.assertFalse(applies(['Bash(*)'], 'Read', {'file_path': '/a'}))

    def test_monitor_is_matched_by_bash_patterns(self):
        self.assertTrue(applies(['Bash(git push *)'], 'Monitor', bash('git push origin')))
        self.assertFalse(applies(['Bash(git push *)'], 'Monitor', bash('git status')))

    def test_monitor_pattern_can_be_written_directly(self):
        self.assertTrue(applies(['Monitor(git push *)'], 'Monitor', bash('git push')))
        self.assertFalse(applies(['Monitor(git push *)'], 'Bash', bash('git push')))

    def test_notebook_edit_is_matched_by_edit_patterns(self):
        call = {'notebook_path': '/n/a.ipynb'}
        self.assertTrue(applies(['Edit(*.ipynb)'], 'NotebookEdit', call))
        self.assertTrue(applies(['NotebookEdit(*.ipynb)'], 'NotebookEdit', call))
        self.assertFalse(applies(['NotebookEdit(*.ipynb)'], 'Edit', {'file_path': '/n/a.ipynb'}))

    def test_edit_patterns_do_not_cover_write(self):
        self.assertFalse(applies(['Edit(*)'], 'Write', {'file_path': '/a'}))

    def test_monitor_follows_shell_interpretation(self):
        self.assertTrue(applies(['Bash(git push *)'], 'Monitor', bash('bash -c "git push"')))
        self.assertFalse(applies(['Bash(git push *)'], 'Monitor', bash('echo "git push"')))


class TestWorkflowName(unittest.TestCase):

    def test_workflow_name_is_matched(self):
        self.assertTrue(applies(['Workflow(sv-*)'], 'Workflow', {'name': 'sv-review-changes'}))
        self.assertFalse(applies(['Workflow(sv-*)'], 'Workflow', {'name': 'other-flow'}))

    def test_workflow_without_name_is_not_matched(self):
        pattern = ['Workflow(*sv-*)']
        self.assertFalse(applies(pattern, 'Workflow', {'script': 'export const meta = {name: "sv-x"}'}))
        self.assertFalse(applies(pattern, 'Workflow', {'scriptPath': '/w/sv-review-changes.js'}))

    def test_name_is_ignored_for_other_tools(self):
        self.assertFalse(applies(['Skill(sv-*)'], 'Skill', {'skill': 'sv-x', 'name': 'sv-x'}))


class TestExceptPerSimpleCommand(unittest.TestCase):

    def test_except_clears_only_its_own_command(self):
        patterns, excepted = ['Bash(grep *)', 'Bash(cat *)'], ['Bash(*README*)']
        self.assertFalse(applies(patterns, 'Bash', bash('cat README.md'), excepted))
        self.assertTrue(applies(patterns, 'Bash', bash('cat README.md && grep -rn x app'), excepted))
        self.assertTrue(applies(patterns, 'Bash', bash('grep x app; cat README.md'), excepted))

    def test_command_after_a_pipe_is_matched(self):
        patterns, excepted = ['Bash(head *)'], ['Bash(*README*)']
        self.assertTrue(applies(patterns, 'Bash', bash('cat README.md | head -5'), excepted))
        self.assertFalse(applies(patterns, 'Bash', bash('head -5 README.md'), excepted))

    def test_example_task_start_gate(self):
        trigger = gate_of(example_policy(), 'task_start')['trigger']
        for tool_name, tool_input, expected in (
            ('Read', {'file_path': '/repo/README.md'}, False),
            ('Read', {'file_path': '/repo/src/a.py'}, True),
            ('Read', {'file_path': '/repo/docs/../src/a.py'}, True),
            ('Bash', bash('cat README.md'), False),
            ('Bash', bash('cat README.md | head -3'), True),
            ('Bash', bash('ls'), False),
            ('NotebookEdit', {'notebook_path': '/n/a.ipynb'}, True),
        ):
            with self.subTest(tool=tool_name, input=tool_input):
                self.assertEqual(_gate_applies(trigger, tool_name, tool_input), expected)

    def test_non_string_command_is_treated_as_empty(self):
        self.assertFalse(applies(['Bash(git push *)'], 'Bash', {'command': None}))
        self.assertTrue(applies(['Bash(*)'], 'Bash', {'command': None}))


if __name__ == '__main__':
    unittest.main()
