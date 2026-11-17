import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from nudgy import chats, claude_notes, discover, google, keep, luma, sources
from nudgy.model import NOW, SOON, LATER, Item, bucket, score

T = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
MS = int(T.timestamp() * 1000)


def incoming(text, ts, uuid="u-ana", number="+491", name="Ana", group=None):
    data = {"timestamp": ts, "message": text}
    if group:
        data["groupInfo"] = {"groupId": group, "groupName": "Hack team", "type": "DELIVER"}
    return {"envelope": {"sourceUuid": uuid, "sourceNumber": number, "sourceName": name,
                         "timestamp": ts, "dataMessage": data}, "account": "+49me"}


class Signal(unittest.TestCase):
    def test_messages_group_by_chat(self):
        inbox = chats.apply_signal({}, [incoming("hi", MS - 2000), incoming("free tonight?", MS - 1000),
                                        incoming("standup moved", MS, uuid="u-ben", name="Ben", group="G1")], T)
        items = {i.id: i for i in chats.signal_items(inbox)}
        self.assertEqual(items["signal:u-ana"].title, "free tonight?")
        self.assertEqual(items["signal:u-ana"].detail, "Ana · 2 new")
        self.assertEqual(items["signal:group:G1"].title, "Ben: standup moved")
        self.assertEqual(items["signal:group:G1"].detail, "Hack team")
        self.assertEqual(items["signal:group:G1"].tags, ["group"])

    def test_read_on_phone_removes_up_to_that_message(self):
        inbox = chats.apply_signal({}, [incoming("a", MS - 2000), incoming("b", MS)], T)
        read = {"envelope": {"sourceUuid": "me", "timestamp": MS + 5, "syncMessage": {
            "readMessages": [{"senderUuid": "u-ana", "senderNumber": "+491", "timestamp": MS - 2000}]}}}
        inbox = chats.apply_signal(inbox, [read], T)
        self.assertEqual([m["text"] for m in inbox["u-ana"]["messages"]], ["b"])
        read["envelope"]["syncMessage"]["readMessages"][0]["timestamp"] = MS
        self.assertEqual(chats.apply_signal(inbox, [read], T), {})

    def test_reply_from_phone_clears_chat(self):
        inbox = chats.apply_signal({}, [incoming("a", MS)], T)
        sent = {"envelope": {"syncMessage": {"sentMessage": {"destinationUuid": "u-ana", "message": "ok",
                                                             "timestamp": MS + 1}}}}
        self.assertEqual(chats.apply_signal(inbox, [sent], T), {})

    def test_old_messages_and_receipts_ignored(self):
        old = MS - 20 * 86400 * 1000
        receipt = {"envelope": {"sourceUuid": "u-ana", "receiptMessage": {"isDelivery": True}}}
        self.assertEqual(chats.apply_signal({}, [incoming("ancient", old), receipt], T), {})

    def test_fetch_saves_before_returning_and_skips_when_unlinked(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_DATA_HOME": home}):
            with mock.patch.object(chats, "signal_cli", return_value=None):
                self.assertIsNone(chats.fetch_signal(T))
            accounts = Path(home) / "signal-cli/data/accounts.json"
            accounts.parent.mkdir(parents=True)
            accounts.write_text(json.dumps({"accounts": [{"number": "+49me", "path": "1"}]}))
            output = mock.Mock(returncode=0, stderr="", stdout=json.dumps(incoming("hi", MS)) + "\nnot json\n")
            with mock.patch.object(chats, "signal_cli", return_value="/x/signal-cli"), \
                    mock.patch.object(chats.subprocess, "run", return_value=output) as run:
                items = chats.fetch_signal(T)
            self.assertEqual([i.title for i in items], ["hi"])
            self.assertIn("-a", run.call_args.args[0])
            self.assertNotIn("--send-read-receipts", run.call_args.args[0])
            saved = Path(home) / "nudgy/signal-inbox.json"
            self.assertEqual(oct(saved.stat().st_mode & 0o777), "0o600")
            self.assertIn("u-ana", json.loads(saved.read_text()))


class Combined(unittest.TestCase):
    def test_unlinked_chats_are_not_errors(self):
        with mock.patch.object(google, "sync_all", return_value=({"gmail": []}, {})), \
                mock.patch.object(chats, "fetch_signal", side_effect=chats.NotReady("signal-cli not linked")), \
                mock.patch.object(claude_notes, "read", return_value=[]), \
                mock.patch.object(luma, "fetch_luma", return_value=None), \
                mock.patch.object(keep, "fetch_keep", return_value=None), \
                mock.patch.object(discover, "fetch", return_value=([], [])):
            items, errors = sources.sync_all(T)
        self.assertEqual(items, {"gmail": [], "claude": [], "potential": []})
        self.assertEqual(errors, {"signal": "signal-cli not linked"})

    def test_chat_ranking(self):
        direct = Item("s", "signal", "can you send the deck?", tags=["direct"])
        group = Item("w", "signal", "lol", tags=["group"])
        urgent = Item("w2", "signal", "interview tomorrow, urgent", tags=["group"])
        self.assertEqual(bucket(score(direct, T)), SOON)
        self.assertEqual(bucket(score(group, T)), LATER)
        self.assertEqual(bucket(score(urgent, T)), SOON)
        self.assertEqual(bucket(score(group, T, "high")), NOW)


class GoogleClient(unittest.TestCase):
    def test_import_client_validates_and_is_private(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            bad = Path(home) / "bad.json"
            bad.write_text("{}")
            with self.assertRaises(ValueError):
                google.import_client(bad)
            good = Path(home) / "good.json"
            good.write_text(json.dumps({"installed": {"client_id": "abc", "client_secret": "s"}}))
            google.import_client(good)
            saved = Path(home) / "nudgy/google-client.json"
            self.assertEqual(oct(saved.stat().st_mode & 0o777), "0o600")
            (Path(home) / "nudgy/token.json").write_text("{}")
            google.sign_out()
            self.assertFalse((Path(home) / "nudgy/token.json").exists())


if __name__ == "__main__":
    unittest.main()
