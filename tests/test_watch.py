import unittest

from nudgy.watch import DEFAULTS, Escalation, Judge, Verdict, parse_verdict, rules, site_of

TASKS = [("Apply to the film contest with the short clip", "tomorrow 10:00"), ("Grant submission", "overdue")]


class Rules(unittest.TestCase):
    def test_task_word_in_title_is_on_task(self):
        verdict = rules("blender", "kettle_scene01.blend - Blender", TASKS, DEFAULTS)
        self.assertEqual((verdict.verdict, verdict.judge), ("on_task", "rules"))

    def test_work_app_is_on_task(self):
        self.assertEqual(rules("Gnome-terminal", "user@laptop: ~", TASKS, DEFAULTS).verdict, "on_task")

    def test_distraction_goes_to_the_model(self):
        self.assertIsNone(rules("firefox", "Funny cats - YouTube — Mozilla Firefox", TASKS, DEFAULTS))

    def test_site_name(self):
        self.assertEqual(site_of("firefox", "Funny cats - YouTube — Mozilla Firefox", DEFAULTS), "Youtube")
        self.assertEqual(site_of("Nautilus", "Downloads", DEFAULTS), "Nautilus")


class Parsing(unittest.TestCase):
    def test_reads_json_inside_text(self):
        verdict = parse_verdict('Sure! {"verdict": "digressing", "confidence": "0.8", "task": "x"} ok', "local")
        self.assertEqual((verdict.verdict, verdict.confidence), ("digressing", 0.8))

    def test_broken_answer_is_unsure(self):
        self.assertEqual(parse_verdict("no idea", "local").verdict, "unsure")
        self.assertEqual(parse_verdict('{"verdict": "maybe"}', "local").verdict, "unsure")

    def test_long_lookup_is_dropped(self):
        self.assertEqual(parse_verdict('{"verdict":"unsure","lookup":"' + "x" * 80 + '"}', "local").lookup, "")


class Chain(unittest.TestCase):
    def make(self, local, claude=None, cap=20):
        calls = {"local": 0, "claude": 0, "lookup": 0}

        def fake_local(app, title, tasks, config, extra=""):
            calls["local"] += 1
            return local(extra)

        def fake_claude(app, title, tasks, config):
            calls["claude"] += 1
            return (claude or Verdict("digressing", 0.9, "", "", "claude")), 800

        def fake_lookup(term):
            calls["lookup"] += 1
            return "a video game"

        judge = Judge({**DEFAULTS, "claude_daily_cap": cap}, fake_local, fake_claude, fake_lookup)
        return judge, calls

    def test_confident_local_skips_claude_and_is_cached(self):
        judge, calls = self.make(lambda extra: Verdict("digressing", 0.9, "", "", "local"))
        judge.decide("firefox", "Cats - YouTube", TASKS)
        verdict = judge.decide("firefox", "Cats - YouTube", TASKS)
        self.assertEqual((calls["local"], calls["claude"], verdict.judge), (1, 0, "cache"))

    def test_lookup_then_claude_when_still_unsure(self):
        judge, calls = self.make(lambda extra: Verdict("unsure", 0.3, "", "", "local", lookup="Hollow Knight"))
        verdict = judge.decide("firefox", "Hollow Knight speedrun", TASKS)
        self.assertEqual((calls["lookup"], calls["local"], calls["claude"]), (1, 2, 1))
        self.assertEqual(verdict.judge, "claude")
        self.assertEqual(judge.claude_tokens, 800)

    def test_daily_cap(self):
        judge, calls = self.make(lambda extra: Verdict("unsure", 0.2, "", "", "local"), cap=1)
        judge.decide("a", "one", TASKS)
        verdict = judge.decide("a", "two", TASKS)
        self.assertEqual(calls["claude"], 1)
        self.assertEqual(verdict.verdict, "unsure")

    def test_you_said_so(self):
        judge, calls = self.make(lambda extra: Verdict("digressing", 0.9, "", "", "local"))
        judge.allow("firefox", "Blender tips - YouTube", TASKS)
        self.assertEqual(judge.decide("firefox", "Blender tips - YouTube", TASKS).judge, "you")
        self.assertEqual(calls["local"], 0)


class Escalating(unittest.TestCase):
    def test_hourly_checks_step_up_once_each(self):
        steps = Escalation(DEFAULTS)
        self.assertEqual([steps.feed("digressing", h * 3600, False) for h in range(4)], [1, 2, 3, 3])

    def test_steps_follow_drifting_time(self):
        steps = Escalation({**DEFAULTS, "steps_minutes": [10, 15], "steps_minutes_overdue": [5, 8]})
        self.assertEqual(steps.feed("digressing", 0, False), 1)
        self.assertEqual(steps.feed("digressing", 9 * 60, False), 1)
        self.assertEqual(steps.feed("digressing", 10 * 60, False), 2)
        self.assertEqual(steps.feed("unsure", 20 * 60, False), 2)
        self.assertEqual(steps.feed("digressing", 25 * 60, False), 3)
        self.assertEqual(steps.feed("on_task", 26 * 60, False), 0)

    def test_faster_when_overdue(self):
        steps = Escalation({**DEFAULTS, "steps_minutes": [10, 15], "steps_minutes_overdue": [5, 8]})
        steps.feed("digressing", 0, True)
        self.assertEqual(steps.feed("digressing", 5 * 60, True), 2)
        self.assertEqual(steps.feed("digressing", 13 * 60, True), 3)


if __name__ == "__main__":
    unittest.main()
