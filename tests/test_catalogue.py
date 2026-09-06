"""Catalogue search, shared chat until edit, and settings keys."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-fernet-derivation")

import catalogue  # noqa: E402
import storage  # noqa: E402


class CatalogueSearchAndChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        catalogue.CATALOGUE_DIR = root / "catalogue"
        storage.USERS_DATA_DIR = root / "users"
        storage.DATA_DIR = root
        storage.PROMPTS_DIR = root / "prompts"
        catalogue.PROMPTS_DIR = root / "prompts"
        catalogue.CATALOGUE_DIR.mkdir(parents=True, exist_ok=True)
        storage.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

    def test_search_by_owner_and_pagination(self):
        catalogue.create_prompt("ali", "related chats", "Classify {Messages}.", "Conversation:\n{Messages}")
        catalogue.create_prompt("sara", "visa notes", "Score {Notes}.", "{Notes}")
        everyone = catalogue.create_prompt("ali", "public eval", "Look at {Messages}.", "{Messages}", visibility="everyone")
        found = catalogue.search_visible("sara", owner="ali")
        ids = [i["id"] for i in found["items"]]
        self.assertIn(everyone["id"], ids)
        self.assertTrue(all(i["owner"] == "ali" for i in found["items"]))
        paged = catalogue.search_visible("ali", q="related", page=1, page_size=1)
        self.assertEqual(paged["total"], 1)
        self.assertEqual(paged["pages"], 1)

    def test_your_conversation_follows_you_to_your_clone(self):
        """Sara works something out on Ali's prompt, then clones it to make the
        change. Her side of the conversation comes with her; Ali's does not
        move and is not touched."""
        src = catalogue.create_prompt("ali", "source", "Read {Messages}.", "{Messages}", visibility="everyone")
        catalogue.append_chat(src["id"], "ali", "user", "ali's own question")
        catalogue.append_chat(src["id"], "sara", "user", "why does it miss refunds?")
        catalogue.append_chat(src["id"], "sara", "assistant", "the rule only covers cancellations")

        self.assertEqual(len(catalogue.load_chat(src["id"], "sara")), 2)
        self.assertEqual(len(catalogue.load_chat(src["id"], "ali")), 1)

        clone = catalogue.clone_prompt(src["id"], "sara")
        carried = catalogue.load_chat(clone["id"], "sara")
        self.assertEqual(len(carried), 2)
        self.assertEqual(carried[0]["text"], "why does it miss refunds?")
        # Ali keeps his, and does not inherit hers on the copy he cannot see.
        self.assertEqual(len(catalogue.load_chat(src["id"], "ali")), 1)
        self.assertEqual(catalogue.load_chat(clone["id"], "ali"), [])

        # Editing the copy leaves every conversation on the original intact.
        catalogue.update_prompt(clone["id"], "sara", prompt="Read {Messages}. Be strict.")
        self.assertEqual(len(catalogue.load_chat(src["id"], "sara")), 2)
        self.assertEqual(len(catalogue.load_chat(src["id"], "ali")), 1)

    def test_editing_snapshots_the_previous_text(self):
        src = catalogue.create_prompt("ali", "shared", "Version one {Messages}.", "{Messages}")
        catalogue.update_prompt(src["id"], "ali", prompt="Version two {Messages}.")
        versions = catalogue.list_versions(src["id"], "ali")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["prompt"], "Version one {Messages}.")
        restored = catalogue.restore_version(src["id"], "ali", 0)
        self.assertEqual(restored["prompt"], "Version one {Messages}.")
        self.assertEqual(len(catalogue.list_versions(src["id"], "ali")), 2)

    def test_publish_version_updates_for_editors_and_copies_for_viewers(self):
        src = catalogue.create_prompt(
            "ali", "agent_eval", "Old {Messages}.", "{Messages}", visibility="everyone"
        )
        updated = catalogue.publish_version(src["id"], "ali", "agent_eval.v2", "New {Messages}.")
        self.assertEqual(updated["mode"], "updated")
        self.assertEqual(catalogue.load_body(src["id"])["prompt"], "New {Messages}.")

        made = catalogue.publish_version(src["id"], "sara", "agent_eval.v3", "Sara {Messages}.")
        self.assertEqual(made["mode"], "created")
        self.assertNotEqual(made["published_to"], src["id"])
        self.assertEqual(catalogue.load_body(made["published_to"])["prompt"], "Sara {Messages}.")
        # A viewer's version keeps the reach of the prompt it came from.
        self.assertEqual(made["visibility"], "everyone")
        self.assertEqual(catalogue.load_body(src["id"])["prompt"], "New {Messages}.")

    def test_delete_removes_the_conversation(self):
        src = catalogue.create_prompt("ali", "temp", "Read {Messages}.", "{Messages}", visibility="everyone")
        catalogue.append_chat(src["id"], "ali", "user", "internal context")
        catalogue.append_chat(src["id"], "sara", "user", "sara's side")
        conv = catalogue._conversation_id_for(src["id"])
        paths = [catalogue._conversation_path(conv, "ali"), catalogue._conversation_path(conv, "sara")]
        self.assertTrue(all(p.is_file() for p in paths))
        catalogue.delete_prompt(src["id"], "ali")
        # Every participant's side goes, not just the owner's.
        self.assertFalse(any(p.is_file() for p in paths))

    def test_viewer_cannot_edit(self):
        src = catalogue.create_prompt("ali", "locked", "Read {Messages}.", "{Messages}", visibility="everyone")
        with self.assertRaises(PermissionError):
            catalogue.update_prompt(src["id"], "sara", prompt="changed")
        card = catalogue.request_edit(src["id"], "sara")
        self.assertIn("sara", card.get("edit_requests") or [])
        granted = catalogue.resolve_edit_request(src["id"], "ali", "sara", grant=True)
        self.assertIn("sara", granted.get("editors") or [])
        catalogue.update_prompt(src["id"], "sara", prompt="Read {Messages}. Updated.")

    def test_change_log_stored(self):
        src = catalogue.create_prompt("ali", "logged", "Read {Messages}.", "{Messages}")
        catalogue.append_change(src["id"], "ali", "Do not treat Tadbeer alone as related", "Related-chat classifier")
        ctx = catalogue.change_context(src["id"])
        self.assertEqual(ctx["purpose"], "Related-chat classifier")
        self.assertEqual(ctx["change_log"][0]["summary"], "Do not treat Tadbeer alone as related")


class SettingsAndNamedKeysTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        storage.USERS_DATA_DIR = Path(self.tmp.name) / "users"

    def test_named_keys_and_defaults(self):
        card = storage.save_named_key("ali", "Work LangCC", "langcc", "sk-work")
        self.assertEqual(storage.load_named_key("ali", card["id"]), "sk-work")
        storage.save_user_settings("ali", {
            "default_provider": "langcc",
            "default_model": "gpt-5-mini",
            "default_key_id": card["id"],
            "last_prompt_id": "abc",
            "last_prompt_name": "related",
        })
        settings = storage.load_user_settings("ali")
        self.assertEqual(settings["last_prompt_name"], "related")
        self.assertEqual(storage.tool_defaults("ali", "runner")["model"], "gpt-5-mini")
        self.assertEqual(storage.resolve_user_key("ali", "langcc", card["id"]), "sk-work")


if __name__ == "__main__":
    unittest.main()
