"""UserPromptSubmit hook: which prompts are recorded."""
import os
import re
import unittest

from tests.hook_harness import (
    AGED_TIME, DB_NAME, HookTestCase, PROMPT_STORE, SESSION, assert_denied, run_hook,
)


def prompt_input(prompt: str, session_id: str = SESSION) -> dict:
    return {'session_id': session_id, 'prompt': prompt}


class TestRecording(HookTestCase):

    def store(self, prompt: str, session_id: str = SESSION):
        return run_hook(PROMPT_STORE, prompt_input(prompt, session_id), self.data_dir)

    def prompts(self) -> list:
        rows = self.state._get_conn().execute(
            'SELECT id, prompt FROM user_prompts WHERE session_id = ? ORDER BY id', (SESSION,)
        ).fetchall()
        return [(r['id'], r['prompt']) for r in rows]

    def test_prompt_is_recorded_and_its_id_is_announced(self):
        result = self.store('push して')
        (pid, text), = self.prompts()
        self.assertEqual(text, 'push して')
        self.assertIn(f'prompt_id={pid}', result.context)

    def test_system_notification_only_is_not_recorded(self):
        prompt = (
            '<task-notification> <summary>done</summary> </task-notification>\n'
            '<system-reminder> 連絡帳の新着 </system-reminder>'
        )
        result = self.store(prompt)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.prompts(), [])

    def test_prompt_with_user_text_next_to_a_notification_is_recorded(self):
        self.store('<task-notification>done</task-notification> push して')
        self.assertEqual(len(self.prompts()), 1)

    def test_unclosed_system_tag_in_a_user_sentence_is_still_recorded(self):
        self.store('<system-reminder> が混ざる件について push して')
        self.assertEqual(len(self.prompts()), 1)

    def test_unclosed_tag_text_never_authorizes_a_push(self):
        result = self.store('<system-reminder> が混ざる件について push して')
        pid = int(re.search(r'prompt_id=(\d+)', result.context).group(1))
        assert_denied(self.gate('git push'))
        output = self.gate_command(
            f'gate-ack user_authorized_push --prompt-id {pid} --reason xxxxxxxxxxxxxxxxxxxxxxxx'
        )
        self.assertIn('Rejected', output)

    def test_empty_prompt_or_session_is_ignored(self):
        self.assertEqual(self.store('').stdout, '')
        self.assertEqual(run_hook(PROMPT_STORE, {'prompt': 'x'}, self.data_dir).stdout, '')
        self.assertEqual(self.prompts(), [])

    def test_invalid_stdin_is_ignored(self):
        result = run_hook(PROMPT_STORE, 'nonsense', self.data_dir)
        self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_recording_marks_the_session_active(self):
        self.state._get_conn().execute('UPDATE sessions SET last_active_at = ?', (AGED_TIME,))
        self.state._get_conn().commit()
        self.store('hello')
        row = self.state._get_conn().execute(
            'SELECT last_active_at FROM sessions WHERE session_id = ?', (SESSION,)
        ).fetchone()
        self.assertGreater(row[0], AGED_TIME)

    def test_prompt_of_an_unknown_session_creates_the_session(self):
        self.store('hello', session_id='brand-new')
        row = self.state._get_conn().execute(
            'SELECT last_active_at FROM sessions WHERE session_id = ?', ('brand-new',)
        ).fetchone()
        self.assertIsNotNone(row[0])

    def test_database_failure_does_not_stop_the_prompt(self):
        self.state.close()
        for suffix in ('', '-wal', '-shm'):
            path = os.path.join(self.data_dir, DB_NAME + suffix)
            if os.path.exists(path):
                os.remove(path)
        with open(os.path.join(self.data_dir, DB_NAME), 'wb') as f:
            f.write(b'not a database' * 100)
        result = self.store('hello')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('prompt_store', result.stderr)


if __name__ == '__main__':
    unittest.main()
