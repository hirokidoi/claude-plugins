"""Shell interpretation: which commands the push gate applies to, and how real shells read them."""
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.hook_harness import gate_of, example_policy
from gate_check import _gate_applies
from shell_parse import OPAQUE, command_forms

PUSH_TRIGGER = {
    'patterns': ['Bash(git push *)'],
    'except_patterns': ['Bash(git push --dry-run *)'],
}

# (command, whether the push gate applies)
GATE_TABLE = [
    # plain and wrapped
    ('git push', True),
    ('git push origin master', True),
    ('git -C repo push origin master', True),
    ('git -C ~/repo push origin master', True),
    ('git -c core.pager=cat --no-pager push', True),
    ('git --attr-source HEAD push', True),
    ('/usr/bin/git push', True),
    ('git add . && git commit -m x && git push', True),
    ('cd repo; git push', True),
    ('GIT_TRACE=1 git push', True),
    ('Git push', True),
    # wrapper variants and zsh forms
    ("eval -- 'git push'", True),
    ('bash - <<EOF\ngit push\nEOF', True),
    ('env -P /usr/bin git push', True),
    ('env -P /usr/bin -i git push', True),
    ('env -L root git push', True),
    ('env -x unknown-value git push', True),
    ('env - git push', True),
    ('git -c Alias.p=push p', True),
    ('git -c alias.P=push p', True),
    ('git -c alias.p=push P', True),
    ('- git push', True),
    ('=git push', True),
    ("echo ${(e):-'$(git push)'}", True),
    ('caffeinate -t 5 git push', True),
    ('arch -arm64 git push', True),
    ('xcrun git push', True),
    ('git-push origin', True),
    ('/usr/libexec/git-core/git-push origin', True),
    # nested past the unwrap limits: treated as matching
    (' '.join(['command'] * 200) + ' git push', True),
    (' '.join(['eval'] * 12) + ' git push', True),
    ('nice -x nice ' * 30 + 'git push', True),
    ('$(' * 41 + "bash -c 'git push'" + ')' * 41, True),
    # bundled shell options whose letters take values
    ("bash -oc pipefail 'git push'", True),
    ("bash -co pipefail 'git push'", True),
    ("sh -eoc pipefail 'git push'", True),
    ("bash -Oc extglob 'git push'", True),
    # parse failures still unwrap each piece
    ("echo $(case x in x) :;; esac); bash -c 'git push'", True),
    ("echo $((echo a) ); bash -c 'git push'", True),
    ("echo $(case x in x) :;; esac); eval 'git push'", True),
    ("echo $(case x in x) :;; esac); env -i git push", True),
    ("echo $(case x in x) :;; esac); bash <<<'git push'", True),
    ("echo $((echo a) ); bash -c 'cd x && git push'", True),
    ("echo $((echo a) ); bash -c 'git status; git push'", True),
    ('echo $((echo a) ); eval "git status; git push"', True),
    ("echo $((echo a) ); env -i bash -c 'true; git push'", True),
    ("echo $((echo a) ); bash -c 'git push|cat'", True),
    ("echo $((echo a) ); bash -c 'f() { git push; }; f'", True),
    ("echo $(case x in x) :;; esac); sh -c 'cd /x && git push'", True),
    ("echo $((echo a) ); cat <<EOF\nit's done\nEOF\nbash -c 'cd x && git push'", True),
    ("echo $((echo a) ); # don't\nbash -c 'cd x && git push'", True),
    ("echo $((echo a) ); echo it\\'s; bash -c 'cd x && git push'", True),
    ('echo $((echo a) ); echo \\"; bash -c "cd x && git push"', True),
    ("echo $((echo a) ); echo $'it\\'s'; bash -c 'cd x && git push'", True),
    # unreadable input matches every pattern, and no except pattern
    ('echo $((echo a) ); git push --dry-run origin', True),
    ('/usr/bin/GIT push', True),
    ('\\git push', True),
    ('g\\it push', True),
    ('git \\\npush', True),
    ('bash -c "git push"', True),
    ("sh -c 'git push'", True),
    ('bash -lc "git push"', True),
    ('eval git push', True),
    ('eval "git push"', True),
    ('env FOO=1 git push', True),
    ("env -S 'git push'", True),
    ('command git push', True),
    ('time git push', True),
    ('nohup git push', True),
    ('sudo git push', True),
    ('sudo -u root git push', True),
    ('timeout 5 git push', True),
    ('xargs git push', True),
    ('xargs -a f git push', True),
    ('noglob git push', True),
    ('repeat 1 git push', True),
    # grouping, substitution, control flow
    ('(git push)', True),
    ('{ git push; }', True),
    ('echo $(git push)', True),
    ('echo "$(git push)"', True),
    ('echo `git push`', True),
    ('echo "`git push`"', True),
    ('x=$(git push)', True),
    ('diff <(git push) b', True),
    ('echo $(( $(git push) ))', True),
    ('echo ${x:-$(git push)}', True),
    ('echo "a$(echo "b$(git push)")"', True),
    ('if true; then git push; fi', True),
    ('for f in a; do git push; done', True),
    ('! git push', True),
    ('git push &', True),
    ('git push&&echo', True),
    ('echo a | git push', True),
    ('git push 2>&1', True),
    ('git push|cat', True),
    ('git push>/dev/null', True),
    ('git push<<<x', True),
    ('true & git push', True),
    ('true &git push', True),
    ('git push\\\n origin', True),
    ('git push # (comment) ; ls', True),
    ('git push origin feat#1', True),
    ('echo a#b; git push', True),
    ('git commit -m x\ngit push', True),
    ('f() { git push; }; f', True),
    ('case x in x) git push;; esac', True),
    ('cat <<EOF | git push\nx\nEOF', True),
    ("git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" && git push", True),
    ('exec git push', True),
    ('nice git push', True),
    ('echo x | xargs git push', True),
    ("bash -c \"bash -c 'git push'\"", True),
    ('git "push"', True),
    ("git 'push'", True),
    ('git pu""sh', True),
    ('git \\push', True),
    ('"git" push', True),
    ('FOO=1 /usr/bin/git push', True),
    ('cd repo\ngit push', True),
    ('git commit -m x && git push', True),
    ('echo hi;# c\ngit push', True),
    # here-documents and here-strings
    ('bash <<EOF\ngit push\nEOF', True),
    ('bash <<< "git push"', True),
    ('cat <<EOF\n$(git push)\nEOF', True),
    ('echo "$(cat <<EOF\n$(git push)\nEOF\n)"', True),
    ('cat <<EOF\nfoo\n; git push', True),
    ('cat <<EOF; git push\nx\nEOF', True),
    # aliases given on the command line
    ('git -c alias.x=push x', True),
    ("git -c alias.p='!git push' p", True),
    # unreadable input matches every pattern
    ("echo it's; git push", True),
    # shell reads differ: stay on the gated side
    ('echo "$(case x in x) git push;; esac)"', True),
    ('echo \\$(git push)', True),
    # an excepted part does not exempt the rest
    ('git push --dry-run origin; git push origin master', True),
    ('git push --dry-run && git push', True),
    # not a push
    ('git push --dry-run origin', False),
    ('git stash push', False),
    ('git stash push app/models/user.rb', False),
    ('git commit-tree HEAD', False),
    ('git -C /path/to/push-repo status', False),
    ('git diff app/models/push_subscription.rb docs/web_push.md', False),
    ('echo "x; git push y"', False),
    ('echo "git push"', False),
    ("echo 'git push'", False),
    ('git commit -m "fix\nline2; git push"', False),
    ('git commit -m "prepare git push"', False),
    ("git commit -m '$(git push) について'", False),
    ('grep "git push" README.md', False),
    ('echo hi # ; git push', False),
    ('echo foo #bar $(git push)', False),
    ('echo feat#1', False),
    ("echo '$(git push)'", False),
    ("echo '`git push`'", False),
    ('echo "\\`git push\\`"', False),
    ('echo "\\$(git push)"', False),
    ('echo a\\;git push', False),
    ('cat <<EOF\ngit push\nEOF', False),
    ("cat <<'EOF'\n$(git push)\nEOF", False),
    ('cat <<"EOF"\n$(git push)\nEOF', False),
    ('cat <<\\EOF\n$(git push)\nEOF', False),
    ('cat <<EOF\n\\$(git push)\nEOF', False),
    ('cat <<-EOF\n\tgit push\n\tEOF', False),
    ("git commit -m \"$(cat <<'EOF'\ndon't\n1) item\ngit push later\nEOF\n)\"", False),
    ("echo '${(e):-x}' git push", False),
    ('env FOO=1 git status', False),
    ('caffeinate -t 5 make push', False),
]

# Wrappers that need a real external binary are not run for real.
_NOT_RUN = ('sudo ', 'timeout ', 'xargs ')
# Commands that reach the real git, not the stub, are never run (a login shell resets PATH).
_BYPASSES_STUB = ('/git', 'git-core/', 'env -', 'xcrun', ' -l', '--login')


def _bypasses_stub(command: str) -> bool:
    return any(p in command.lower() for p in _BYPASSES_STUB)


def _applies(command: str) -> bool:
    return _gate_applies(PUSH_TRIGGER, 'Bash', {'command': command})


def _detected(command: str) -> bool:
    """A push is found in the command, before any except pattern."""
    trigger = {'patterns': PUSH_TRIGGER['patterns'], 'except_patterns': []}
    return _gate_applies(trigger, 'Bash', {'command': command})


class TestPushGateTable(unittest.TestCase):

    def test_table(self):
        for command, expected in GATE_TABLE:
            with self.subTest(command=command):
                self.assertEqual(_applies(command), expected)


class TestOtherGitAndReadCommands(unittest.TestCase):

    def _matches(self, pattern: str, command: str) -> bool:
        return _gate_applies({'patterns': [pattern], 'except_patterns': []}, 'Bash', {'command': command})

    def test_commit_pattern_is_exact_about_the_subcommand(self):
        self.assertTrue(self._matches('Bash(git commit *)', 'git commit -F tmp/msg.txt'))
        self.assertFalse(self._matches('Bash(git commit *)', 'git log --grep commit'))
        self.assertFalse(self._matches('Bash(git commit *)', 'git commit-tree HEAD^{tree}'))

    def test_read_command_patterns(self):
        self.assertTrue(self._matches('Bash(grep *)', 'grep -rn foo app'))
        self.assertTrue(self._matches('Bash(sed *)', 'cd x && sed -n 1,20p a.rb'))
        self.assertFalse(self._matches('Bash(grep *)', 'git grep foo'))

    def test_dry_run_except_pattern_sees_through_global_options(self):
        self.assertTrue(self._matches('Bash(git push --dry-run *)', 'git -C repo push --dry-run origin'))

    def test_unreadable_input_matches_every_shell_pattern(self):
        unreadable = 'echo $(case x in x) :;; esac)'
        for pattern in ('Bash(git commit *)', 'Bash(grep *)', 'Bash(anything)'):
            with self.subTest(pattern=pattern):
                self.assertTrue(self._matches(pattern, unreadable))
        self.assertFalse(_gate_applies({'patterns': ['Edit(*)'], 'except_patterns': []},
                                       'Bash', {'command': unreadable}))


class TestCommandForms(unittest.TestCase):

    def test_empty_command_has_no_simple_command(self):
        self.assertEqual(command_forms(''), [])
        self.assertEqual(command_forms('# only a comment'), [])

    def test_substitution_body_does_not_leak_into_the_literal(self):
        literals = [lit for lit, _ in command_forms('echo "$(git push)" done')]
        self.assertIn('git push', literals)
        outer = [lit for lit in literals if lit.startswith('echo')]
        self.assertEqual(len(outer), 1)
        self.assertNotIn('git push', outer[0])

    def test_heredoc_in_substitution_yields_two_simple_commands(self):
        command = "git commit -m \"$(cat <<'EOF'\ndon't\n1) item\ngit push later\nEOF\n)\""
        forms = command_forms(command)
        self.assertEqual(sorted(canon.split()[0] for _, canon in forms), ['cat', 'git'])
        self.assertTrue(any(canon.startswith('git commit') for _, canon in forms))
        self.assertFalse(any('later' in lit or 'item' in lit for lit, _ in forms))

    def test_heredoc_in_substitution_is_not_a_parse_failure(self):
        command = "git commit -m \"$(cat <<'EOF'\ndon't\n1) item\ngit push later\nEOF\n)\""
        self.assertNotIn(OPAQUE, [canon for _, canon in command_forms(command)])
        self.assertFalse(_applies(command))

    def test_wrapped_command_returns_the_wrapper_and_the_inner_command(self):
        canons = [canon for _, canon in command_forms('bash -c "git push"')]
        self.assertIn('git push', canons)
        self.assertEqual(len(canons), 2)

    def test_git_alias_is_expanded(self):
        canons = [canon for _, canon in command_forms('git -c alias.x=push x')]
        self.assertEqual(canons, ['git push'])


class TestTaskStartGateOnHeredocBody(unittest.TestCase):
    """Body lines of a heredoc are not commands; cat itself is (the example's task_start reads it)."""

    @classmethod
    def setUpClass(cls):
        cls.trigger = gate_of(example_policy(), 'task_start')['trigger']

    def _applies(self, command: str) -> bool:
        return _gate_applies(self.trigger, 'Bash', {'command': command})

    def test_body_lines_do_not_apply(self):
        command = "git commit -m \"$(cat <<'EOF'\ngrep foo\nsed -n 1p x\nhead x\nEOF\n)\""
        forms = [canon for _, canon in command_forms(command)]
        self.assertEqual(sorted(forms), ['cat', 'git commit -m $(...)'])

    def test_cat_in_substitution_applies_to_task_start(self):
        command = "git commit -m \"$(cat <<'EOF'\nmessage\nEOF\n)\""
        self.assertTrue(self._applies(command))

    def test_body_lines_alone_do_not_apply(self):
        self.assertFalse(self._applies("git commit -m 'grep foo; head x'"))


@unittest.skipUnless(shutil.which('zsh'), 'zsh is not installed')
class TestAgainstRealShells(unittest.TestCase):
    """Run each table command with git replaced by a stub; a push that really runs must be detected."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.stub_dir = os.path.join(cls._tmp.name, 'stub')
        cls.work_dir = os.path.join(cls._tmp.name, 'work')
        os.makedirs(cls.stub_dir)
        os.makedirs(cls.work_dir)
        stub = os.path.join(cls.stub_dir, 'git')
        with open(stub, 'w') as f:
            f.write('#!/bin/sh\necho "RAN git $*"\n')
        os.chmod(stub, 0o755)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _ran_push(self, shell: str, command: str) -> bool:
        proc = subprocess.run(
            [shell, '-c', command], capture_output=True, text=True, timeout=10,
            cwd=self.work_dir,
            env={
                'PATH': f'{self.stub_dir}:/usr/bin:/bin', 'HOME': self.work_dir,
                # A real git that slips past the stub must not find any repository
                'GIT_CEILING_DIRECTORIES': self._tmp.name,
            },
        )
        return 'RAN git push' in proc.stdout

    def test_executed_push_is_never_hidden(self):
        shells = [s for s in ('zsh', 'bash') if shutil.which(s)]
        for command, expected in GATE_TABLE:
            if command.startswith(_NOT_RUN) or _bypasses_stub(command):
                continue
            for shell in shells:
                with self.subTest(shell=shell, command=command):
                    if self._ran_push(shell, command):
                        self.assertTrue(_detected(command), 'a push that runs is not detected')

    def test_commands_expected_not_to_apply_do_not_push_in_zsh(self):
        for command, expected in GATE_TABLE:
            if (expected or command.startswith(_NOT_RUN) or '--dry-run' in command
                    or _bypasses_stub(command)):
                continue
            with self.subTest(command=command):
                self.assertFalse(self._ran_push('zsh', command))

    def test_heredoc_message_runs_the_commit_only(self):
        command = "git commit -m \"$(cat <<'EOF'\ndon't\n1) item\ngit push later\nEOF\n)\""
        self.assertFalse(self._ran_push('zsh', command))


if __name__ == '__main__':
    unittest.main()
