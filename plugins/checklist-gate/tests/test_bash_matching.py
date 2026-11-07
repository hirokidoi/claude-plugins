"""Unit tests for Bash pattern matching in gate_check.py."""
import os
import sys
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'scripts'))

from gate_check import _matches_tool_pattern


def _bash(command: str) -> dict:
    return {'command': command}


class TestGitSubcommandMatching(unittest.TestCase):

    def assertMatches(self, pattern: str, command: str) -> None:
        self.assertTrue(_matches_tool_pattern(pattern, 'Bash', _bash(command)), command)

    def assertNotMatches(self, pattern: str, command: str) -> None:
        self.assertFalse(_matches_tool_pattern(pattern, 'Bash', _bash(command)), command)

    def test_plain_push(self):
        self.assertMatches('Bash(git push *)', 'git push')
        self.assertMatches('Bash(git push *)', 'git push origin master')

    def test_push_after_global_options(self):
        self.assertMatches('Bash(git push *)', 'git -C ~/repo push origin master')
        self.assertMatches('Bash(git push *)', 'git -c core.pager=cat --no-pager push')
        self.assertMatches('Bash(git push *)', '/usr/bin/git push')
        self.assertMatches('Bash(git push *)', 'GIT_TRACE=1 git push')
        self.assertMatches('Bash(git push *)', 'git --attr-source HEAD push')

    def test_push_in_chained_command(self):
        self.assertMatches('Bash(git push *)', 'git add . && git commit -m x && git push')
        self.assertMatches('Bash(git push *)', 'cd repo; git push')

    def test_stash_push_is_not_push(self):
        self.assertNotMatches('Bash(git push *)', 'git stash push app/models/user.rb')

    def test_push_in_path_is_not_push(self):
        self.assertNotMatches(
            'Bash(git push *)', 'git diff app/models/push_subscription.rb docs/web_push.md'
        )
        self.assertNotMatches('Bash(git push *)', 'git -C /path/to/push-repo status')

    def test_push_in_message_is_not_push(self):
        self.assertNotMatches('Bash(git push *)', 'git commit -m "prepare git push"')
        self.assertNotMatches('Bash(git push *)', 'echo "git push"')

    def test_commit_does_not_match_other_subcommands(self):
        self.assertNotMatches('Bash(git commit *)', 'git log --grep commit')
        self.assertNotMatches('Bash(git commit *)', 'git commit-tree HEAD^{tree}')
        self.assertMatches('Bash(git commit *)', 'git commit -F tmp/msg.txt')

    def test_dry_run_except_pattern(self):
        self.assertMatches('Bash(git push --dry-run *)', 'git -C repo push --dry-run origin')


class TestReadCommandMatching(unittest.TestCase):

    def test_read_commands(self):
        self.assertTrue(_matches_tool_pattern('Bash(grep *)', 'Bash', _bash('grep -rn foo app')))
        self.assertTrue(_matches_tool_pattern('Bash(sed *)', 'Bash', _bash('cd x && sed -n 1,20p a.rb')))
        self.assertFalse(_matches_tool_pattern('Bash(grep *)', 'Bash', _bash('git grep foo')))

    def test_readme_except(self):
        self.assertTrue(_matches_tool_pattern('Bash(*README*)', 'Bash', _bash('cat README.md')))


if __name__ == '__main__':
    unittest.main()
