"""Unit tests for lib/prompt_text.py."""
import os
import sys
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from prompt_text import authored_text, is_system_only


class TestAuthoredText(unittest.TestCase):

    def test_strips_ide_opened_file(self):
        prompt = (
            '<ide_opened_file>The user opened the file /app/docs/web_push.md in the IDE.'
            '</ide_opened_file> commit して'
        )
        self.assertEqual(authored_text(prompt), 'commit して')

    def test_strips_pasted_content_with_id_on_closing_tag(self):
        prompt = '見て\n<pasted_content id="8882">\ngit push origin\n</pasted_content id="8882">\nどう？'
        self.assertNotIn('push', authored_text(prompt))
        self.assertIn('見て', authored_text(prompt))
        self.assertIn('どう？', authored_text(prompt))

    def test_strips_ide_selection_multiline(self):
        prompt = '<ide_selection>line1\npush\nline3</ide_selection>\nこれ何？'
        self.assertEqual(authored_text(prompt), 'これ何？')

    def test_unclosed_block_is_removed_to_end(self):
        self.assertEqual(authored_text('OK <pasted_content id="1">git push'), 'OK')

    def test_plain_prompt_is_unchanged(self):
        self.assertEqual(authored_text('commit -> push して'), 'commit -> push して')


class TestIsSystemOnly(unittest.TestCase):

    def test_task_notification_and_reminder_only(self):
        prompt = (
            '<task-notification> <summary>Stop hook feedback</summary> </task-notification>\n'
            '<system-reminder> 連絡帳の新着 </system-reminder>'
        )
        self.assertTrue(is_system_only(prompt))

    def test_user_text_with_notification(self):
        prompt = '<task-notification>done</task-notification> push して'
        self.assertFalse(is_system_only(prompt))

    def test_ide_tag_with_user_text_is_not_system_only(self):
        prompt = '<ide_opened_file>/a/b.rb</ide_opened_file> OK'
        self.assertFalse(is_system_only(prompt))

    def test_paste_only_is_not_system_only(self):
        self.assertFalse(is_system_only('<pasted_content id="1">log</pasted_content id="1">'))


if __name__ == '__main__':
    unittest.main()
