import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from nudgy import phone
from nudgy.model import Item

NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


class ExportTest(unittest.TestCase):
    def scored(self):
        return [(Item(id="n1", source="note", title="Send application", url="file:///x"), 92),
                (Item(id="t1", source="task", title="Book flat viewing", url="https://tasks.google.com/x"), 60),
                (Item(id="p", source="potential", title="Talk", url="https://x"), 25)]

    def test_summary(self):
        data = phone.summary(self.scored(), NOW)
        self.assertEqual((data["pressing"], data["alarm"]), (1, True))
        self.assertEqual(data["now"][0]["title"], "Send application")
        self.assertEqual(data["now"][0]["id"], "n1")            # the phone tells items apart by id
        self.assertEqual(data["now"][0]["url"], "")              # only web links reach the phone
        self.assertEqual([s["title"] for s in data["soon"]], ["Book flat viewing"])

    def test_writes_only_when_changed_and_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.assertTrue(phone.export(self.scored(), NOW, target))
            self.assertFalse(phone.export(self.scored(), NOW, target))
            self.assertTrue(phone.export(self.scored(), NOW + phone.HEARTBEAT, target))   # still alive
            path = target / phone.FILE
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual(list(target.glob(".nudgy-now.*")), [])

    def test_off_without_folder(self):
        self.assertFalse(phone.export(self.scored(), NOW, None) if phone.folder() is None else False)


if __name__ == "__main__":
    unittest.main()
