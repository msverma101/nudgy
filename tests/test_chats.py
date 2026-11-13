import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from nudgy import chats, google, sources
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


class WhatsApp(unittest.TestCase):
    db = {"names": {"49111@s.whatsapp.net": "Ana", "g1@g.us": "Hack team"}, "chats": {
        "49111@s.whatsapp.net": {"unread": 2, "last": "call me", "ts": 1, "group": False},
        "g1@g.us": {"unread": 1, "last": "pizza?", "from": "Ben", "ts": 1, "group": True},
        "49222@s.whatsapp.net": {"unread": 0, "last": "read already", "ts": 1},
        "49333@s.whatsapp.net": {"unread": 4, "last": "muted", "ts": 1, "muted": -1},
        "49444@s.whatsapp.net": {"unread": 1, "last": "archived", "ts": 1, "archived": True},
        "49555@s.whatsapp.net": {"unread": 1, "last": "no name", "ts": 1},
    }}

    def test_only_unread_unmuted_unarchived(self):
        items = {i.id: i for i in chats.whatsapp_items(self.db, T)}
        self.assertEqual(sorted(items), ["whatsapp:49111@s.whatsapp.net", "whatsapp:49555@s.whatsapp.net",
                                         "whatsapp:g1@g.us"])
        self.assertEqual(items["whatsapp:49111@s.whatsapp.net"].detail, "Ana · 2 new")
        self.assertEqual(items["whatsapp:g1@g.us"].title, "Ben: pizza?")
        self.assertEqual(items["whatsapp:49555@s.whatsapp.net"].detail, "+49555")
        self.assertEqual(items["whatsapp:49111@s.whatsapp.net"].url, "https://web.whatsapp.com/send?phone=49111")
        self.assertEqual(items["whatsapp:g1@g.us"].url, "https://web.whatsapp.com/")

    def test_stale_bridge_is_reported(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"XDG_DATA_HOME": home}):
            self.assertIsNone(chats.fetch_whatsapp(T))           # not linked: skipped quietly
            auth = Path(home) / "nudgy/whatsapp-auth/creds.json"
            auth.parent.mkdir(parents=True)
            auth.write_text(json.dumps({"me": {"id": "49@s.whatsapp.net"}}))
            with self.assertRaises(chats.NotReady):
                chats.fetch_whatsapp(T)
            (Path(home) / "nudgy/whatsapp-chats.json").write_text(json.dumps({**self.db, "updated": time.time()}))
            self.assertEqual(len(chats.fetch_whatsapp(T)), 3)


class Combined(unittest.TestCase):
    def test_unlinked_chats_are_not_errors(self):
        with mock.patch.object(google, "sync_all", return_value=({"gmail": []}, {})), \
                mock.patch.object(chats, "fetch_signal", return_value=None), \
                mock.patch.object(sources.claude_notes, "read", return_value=[]), \
                mock.patch.object(sources.luma, "fetch_luma", return_value=None), \
                mock.patch.object(sources.keep, "fetch_keep", return_value=None), \
                mock.patch.object(sources.discover, "fetch", return_value=([], [])), \
                mock.patch.object(chats, "fetch_whatsapp", side_effect=chats.NotReady("bridge not running")):
            items, errors = sources.sync_all(T)
        self.assertEqual(items, {"gmail": [], "claude": [], "potential": []})
        self.assertEqual(errors, {"whatsapp": "bridge not running"})

    def test_chat_ranking(self):
        direct = Item("s", "signal", "can you send the deck?", tags=["direct"])
        group = Item("w", "whatsapp", "lol", tags=["group"])
        urgent = Item("w2", "whatsapp", "interview tomorrow, urgent", tags=["group"])
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
