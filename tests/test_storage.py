import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from nudgy.__main__ import export_command
from nudgy.model import Store
from nudgy.storage import Database


class Export(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        patch = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.folder)})
        patch.start()
        self.addCleanup(patch.stop)
        store = Store()
        store.notes = []
        store.overrides = {"note:1": "high"}
        store.save()

    def test_to_screen(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(export_command([]), 0)
        self.assertEqual(json.loads(out.getvalue())["overrides"], {"note:1": "high"})

    def test_to_a_private_file(self):
        target = self.folder / "backup.json"
        with redirect_stdout(io.StringIO()):
            export_command([str(target)])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(target.read_text())["version"], 2)


class Database_(unittest.TestCase):
    def test_file_is_private_and_log_is_capped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nudgy.db"
            db = Database(path)
            self.addCleanup(db.close)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with mock.patch("nudgy.storage.KEEP_CHANGES", 3):
                for n in range(6):
                    db.write({"notes": [n]}, logged=frozenset({"notes"}))
            self.assertEqual([c["body"] for c in db.changes()], [[3], [4], [5]])

    def test_two_connections_see_each_others_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nudgy.db"
            first, second = Database(path), Database(path)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            first.write({"notes": ["a"]})
            self.assertEqual(second.read(), {"notes": ["a"]})


if __name__ == "__main__":
    unittest.main()
