import time
import unittest

import cairo

from nudgy.pet import Mood, draw, peek, tip


class PetTest(unittest.TestCase):
    def test_face_follows_the_list(self):
        mood = Mood()
        self.assertIn(mood.face(), ("happy", "sleep"))
        mood.pressing = 2
        self.assertEqual(mood.face(), "busy")
        mood.alarm = True
        self.assertEqual(mood.face(), "alarm")
        mood.cheer_until = time.time() + 5
        self.assertEqual(mood.face(), "cheer")

    def test_tip_names_the_next_item(self):
        mood = Mood()
        mood.pressing, mood.top = 1, ["Pay rent"]
        self.assertIn("1 thing needs you now", tip(mood))
        self.assertIn("•  Pay rent", tip(mood))

    def test_peek_is_short(self):
        mood = Mood()
        self.assertEqual(peek(mood), "All calm 🌿")
        mood.top = ["A very long title about applying to a film hackathon in Berlin tomorrow", "B", "C", "D"]
        mood.when = ["tomorrow 10:00", "today", "", "Fri"]
        lines = peek(mood).splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].endswith("· tomorrow 10:00"))
        self.assertIn("…", lines[0])
        self.assertEqual(lines[-1].strip(), "+1 more")

    def test_draws_at_any_size(self):
        for size in (22, 84):
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
            mood = Mood()
            mood.pressing = 12
            for face in ("happy", "busy", "alarm", "cheer", "love", "wink", "yawn", "look", "hop",
                         "surprised", "sideeye", "sulk", "back"):
                mood.act(face, 5)
                mood.judge = {"sideeye": 1, "sulk": 2, "back": 3}.get(face, 0)
                draw(cairo.Context(surface), size, mood, phase=1.0, blink=True)
                draw(cairo.Context(surface), size, mood, phase=2.0, still=True)
            self.assertTrue(any(surface.get_data()))


if __name__ == "__main__":
    unittest.main()
