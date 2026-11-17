import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from nudgy import ai, rerank, settings
from nudgy.model import LATER, NOW, SOON, Item

NOW_T = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
SCORED = [(Item(id=f"note:{n}", source="note", title=f"Item {n}"), value)
          for n, value in enumerate([85, 60, 60, 40, 40, 40, 40, 40], 1)]       # 1 Now, 2 Soon, 5 Later


def answer(sections, reason="because"):
    return json.dumps({"items": [{"n": n, "section": s, "reason": reason} for n, s in enumerate(sections, 1)]})


class ParseTest(unittest.TestCase):
    def test_only_known_items_and_sections(self):
        text = json.dumps({"items": [{"n": 1, "section": "later", "reason": "x"}, {"n": 99, "section": "now"},
                                     {"n": 2, "section": "delete everything"}, {"n": "x", "section": "now"}]})
        found = rerank.parse(text, SCORED)
        self.assertEqual([(s.item.id, s.current, s.section) for s in found], [("note:1", NOW, LATER)])

    def test_broken_answer(self):
        self.assertEqual(rerank.parse("sure! here you go", SCORED), [])


class QualityTest(unittest.TestCase):
    def test_checks(self):
        good = rerank.parse(answer(["now", "soon", "soon", "later", "later", "later", "later", "later"]), SCORED)
        self.assertEqual(rerank.good_enough(good, 8), "")
        self.assertIn("only ranked", rerank.good_enough(good[:3], 8))
        all_now = rerank.parse(answer(["now"] * 8), SCORED)
        self.assertIn("in Now", rerank.good_enough(all_now, 8))
        no_reasons = rerank.parse(answer(["later"] * 8, reason=""), SCORED)
        self.assertIn("reasons", rerank.good_enough(no_reasons, 8))


class SuggestTest(unittest.TestCase):
    def tearDown(self):
        settings.update("ai", {"claude": ""})
        ai.forget_key()

    def test_only_changes_are_suggested(self):
        sections = ["now", "soon", "now", "later", "later", "later", "later", "later"]    # item 3: Soon -> Now
        with mock.patch.object(ai, "local", return_value=ai.Answer(answer(sections))):
            suggestions, by, note = rerank.suggest(SCORED, NOW_T)
        self.assertEqual([(s.item.id, s.current, s.section) for s in suggestions], [("note:3", SOON, NOW)])
        self.assertEqual(by, "local")
        self.assertNotIn("carefully", note)

    def test_poor_local_answer_goes_to_claude_when_connected(self):
        ai.save_key("sk-ant-test")
        settings.update("ai", {"claude": "api"})
        good = answer(["later", "soon", "soon", "later", "later", "later", "later", "later"])
        with mock.patch.object(ai, "local", return_value=ai.Answer(answer(["now"] * 8))), \
                mock.patch.object(ai, "claude", return_value=ai.Answer(good, 10, "claude")) as asked:
            suggestions, by, _note = rerank.suggest(SCORED, NOW_T)
        asked.assert_called_once()
        self.assertEqual((by, [s.item.id for s in suggestions]), ("claude", ["note:1"]))

    def test_poor_local_answer_without_claude_is_flagged(self):
        with mock.patch.object(ai, "local", return_value=ai.Answer(answer(["now"] * 8))), \
                mock.patch.object(ai, "claude", side_effect=AssertionError("not connected")):
            _suggestions, by, note = rerank.suggest(SCORED, NOW_T)
        self.assertEqual(by, "local")
        self.assertIn("Check them carefully", note)

    def test_no_model_at_all(self):
        with mock.patch.object(ai, "local", side_effect=ai.Unavailable("Ollama isn't running")):
            with self.assertRaises(ai.Unavailable):
                rerank.suggest(SCORED, NOW_T)


class ApplyTest(unittest.TestCase):
    def test_moves_and_returns_to_automatic(self):
        from nudgy.model import bucket, score
        items = {i.id: i for i, _ in SCORED}
        overrides = {"note:4": "high"}                               # you had moved item 4 up by hand
        chosen = [rerank.Suggestion(items["note:3"], SOON, NOW, "today"),
                  rerank.Suggestion(items["note:4"], NOW, bucket(score(items["note:4"], NOW_T)), "back")]
        rerank.apply(overrides, chosen, NOW_T)
        self.assertEqual(overrides, {"note:3": "high"})              # 3 moved up; 4 back to automatic


class VoiceTest(unittest.TestCase):
    def test_say_rerank(self):
        from nudgy import voice
        self.assertEqual(voice.control("re-rank my list").command, "rerank")
        self.assertEqual(voice.control("can you reprioritize my todos").command, "rerank")
        self.assertIsNone(voice.control("what's on my list"))


if __name__ == "__main__":
    unittest.main()
