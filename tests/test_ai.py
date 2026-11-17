import json
import unittest
from unittest import mock

from nudgy import ai, models, settings, voice
from nudgy.model import Item


class ModelChoiceTest(unittest.TestCase):
    def test_recommended_by_hardware(self):
        self.assertEqual(models.recommended(vram=4, ram=31).name, "qwen3.5:4b")      # a 4 GB graphics card
        self.assertEqual(models.recommended(vram=8, ram=32).name, "qwen3.5:9b")
        self.assertEqual(models.recommended(vram=0, ram=8).name, "qwen3.5:2b")       # no graphics card
        self.assertEqual(models.recommended(vram=0, ram=3).name, "qwen3.5:0.8b")

    def test_your_choice_wins(self):
        settings.update("ai", {"local_model": "gemma4:e4b"})
        try:
            self.assertEqual(models.current(), "gemma4:e4b")
        finally:
            settings.update("ai", {"local_model": ""})


class RoutingTest(unittest.TestCase):
    def tearDown(self):
        settings.update("ai", {"claude": "", "claude_for": settings.DEFAULTS["ai"]["claude_for"]})
        ai.forget_key()

    def answer(self, job):
        with mock.patch.object(ai, "local", return_value=ai.Answer("local says", 0, "local")), \
                mock.patch.object(ai, "claude", return_value=ai.Answer("claude says", 5, "claude")):
            return ai.complete(job, "system", "user").by

    def test_local_unless_connected(self):
        for job in ai.JOBS:
            self.assertEqual(self.answer(job), "local", job)

    def test_claude_only_for_the_jobs_you_allow(self):
        ai.save_key("sk-ant-test")
        settings.update("ai", {"claude": "api", "claude_for": ["draft"]})
        self.assertEqual(self.answer("draft"), "claude")
        self.assertEqual(self.answer("steps"), "local")
        self.assertEqual(self.answer("talk"), "local")                 # talking never leaves the computer

    def test_claude_failing_falls_back_to_local(self):
        ai.save_key("sk-ant-test")
        settings.update("ai", {"claude": "api"})
        with mock.patch.object(ai, "local", return_value=ai.Answer("local says")), \
                mock.patch.object(ai, "claude", side_effect=ai.Unavailable("offline")), \
                self.assertLogs("nudgy.ai", "WARNING"):
            self.assertEqual(ai.complete("draft", "s", "u").text, "local says")

    def test_connection_needs_its_credentials(self):
        settings.update("ai", {"claude": "api"})
        self.assertEqual(ai.claude_connected(), "")                    # no key saved
        ai.save_key("sk-ant-test")
        self.assertEqual(ai.claude_connected(), "api")


class KeyTest(unittest.TestCase):
    def tearDown(self):
        ai.forget_key()

    def test_key_is_checked_and_private(self):
        with self.assertRaises(ValueError):
            ai.save_key("not a key")
        ai.save_key("  sk-ant-abc  ")
        self.assertEqual(ai.api_key(), "sk-ant-abc")
        self.assertEqual(ai.key_path().stat().st_mode & 0o777, 0o600)


class CleanTest(unittest.TestCase):
    def test_reasoning_is_dropped(self):
        self.assertEqual(ai.clean("Okay, the user wants...\n</think>\n\nThanks!"), "Thanks!")
        self.assertEqual(ai.clean("<think>hm</think>Hi"), "Hi")
        self.assertEqual(ai.clean("Just text"), "Just text")

    def test_local_request(self):
        sent = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({"message": {"content": 'reasons</think> {"a": 1}'}}).encode()

        def fake_open(request, timeout):
            sent.update(json.loads(request.data))
            return Response()
        with mock.patch.object(ai.urllib.request, "urlopen", fake_open):
            answer = ai.local("sys", "hello", json_mode=True, model="qwen3.5:4b")
        self.assertEqual(answer.text, '{"a": 1}')
        self.assertEqual((sent["model"], sent["think"], sent["format"]), ("qwen3.5:4b", False, "json"))


class IntentTest(unittest.TestCase):
    items = [(Item(id="task:poster", source="task", title="Print the conference poster"), "Soon"),
             (Item(id="signal:mia", source="signal", title="Mia: dinner tonight?", detail="Mia"), "Now"),
             (Item(id="gmail:1", source="gmail", title="Interview invitation", detail="Recruiting"), "Now")]

    def test_done_in_your_own_words(self):
        reply = voice.done_intent("I printed the conference poster", self.items)
        self.assertEqual((reply.action, reply.item.id), ("done", "task:poster"))
        self.assertIsNone(voice.done_intent("I think the poster looks great", self.items))
        self.assertIsNone(voice.done_intent("I sent the photos to grandma", self.items))   # nothing by that name

    def test_write_back(self):
        reply = voice.draft_intent("write back to Mia that I'm running late", self.items)
        self.assertEqual((reply.action, reply.item.id, reply.text), ("draft", "signal:mia", "I'm running late"))
        reply = voice.draft_intent("reply to the interview email saying Tuesday works", self.items)
        self.assertEqual((reply.item.id, reply.text), ("gmail:1", "Tuesday works"))


if __name__ == "__main__":
    unittest.main()
