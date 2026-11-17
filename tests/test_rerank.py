import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from nudgy import ai, rerank, settings
from nudgy.model import LATER, NOW, SOON, Item

NOW_T = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
SCORED = [(Item(id=f"note:{n}", source="note", title=f"Item {n}"), value)
          for n, value in enumerate([85, 60, 60, 40, 40, 40, 40, 40], 1)]       # 1 Now, 2 Soon, 5 Later


def answer(now=(), later=(), reason="because"):
    return json.dumps({"now": [{"n": n, "r": reason} for n in now], "later": [{"n": n, "r": reason} for n in later]})


class ParseTest(unittest.TestCase):
    def test_only_known_items(self):
        text = json.dumps({"now": [{"n": 3, "r": "today"}, {"n": 99, "r": "x"}, {"n": "x"}, "junk"],
                           "later": [{"n": 1, "r": "info"}, {"n": 3, "r": "dup"}], "delete": [{"n": 2}]})
        found = rerank.parse(text, SCORED)
        self.assertEqual([(s.item.id, s.current, s.section, s.reason) for s in found],
                         [("note:1", NOW, LATER, "info"), ("note:3", SOON, NOW, "today")])

    def test_broken_answer(self):
        self.assertEqual(rerank.check("sure! here you go", SCORED), ([], "its answer couldn't be read"))
        self.assertEqual(rerank.parse('{"now": "everything"}', SCORED), [])


class QualityTest(unittest.TestCase):
    def test_checks(self):
        self.assertEqual(rerank.good_enough(rerank.parse(answer(now=[2, 3], later=[4]), SCORED), 8), "")
        self.assertEqual(rerank.good_enough([], 8), "")                       # nothing to move is fine
        self.assertIn("in Now", rerank.good_enough(rerank.parse(answer(now=range(1, 8)), SCORED), 8))
        self.assertIn("reasons", rerank.good_enough(rerank.parse(answer(now=[2], reason=""), SCORED), 8))


class SuggestTest(unittest.TestCase):
    def tearDown(self):
        settings.update("ai", {"claude": ""})
        ai.forget_key()

    def test_only_changes_are_suggested(self):
        with mock.patch.object(ai, "local", return_value=ai.Answer(answer(now=[1, 3], later=[8]))):  # 1, 8 stay
            suggestions, by, note = rerank.suggest(SCORED, NOW_T)
        self.assertEqual([(s.item.id, s.current, s.section) for s in suggestions], [("note:3", SOON, NOW)])
        self.assertEqual(by, "local")
        self.assertNotIn("carefully", note)

    def test_poor_local_answer_goes_to_claude_when_connected(self):
        ai.save_key("sk-ant-test")
        settings.update("ai", {"claude": "api"})
        good = answer(later=[1])
        with mock.patch.object(ai, "local", return_value=ai.Answer(answer(now=range(1, 9)))), \
                mock.patch.object(ai, "claude", return_value=ai.Answer(good, 10, "claude")) as asked:
            suggestions, by, _note = rerank.suggest(SCORED, NOW_T)
        asked.assert_called_once()
        self.assertEqual((by, [s.item.id for s in suggestions]), ("claude", ["note:1"]))

    def test_poor_local_answer_without_claude_is_flagged(self):
        with mock.patch.object(ai, "local", return_value=ai.Answer(answer(now=range(1, 9)))), \
                mock.patch.object(ai, "claude", side_effect=AssertionError("not connected")):
            _suggestions, by, note = rerank.suggest(SCORED, NOW_T)
        self.assertEqual(by, "local")
        self.assertIn("Check these carefully", note)

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
