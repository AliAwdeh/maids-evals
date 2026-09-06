"""Field matches belong to the prompt that needs them, not to the session."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-column-map")
os.environ.setdefault("ADMIN_USERNAME", "mapuser")
os.environ.setdefault("ADMIN_TOKEN", "map-token")

import catalogue  # noqa: E402
import storage  # noqa: E402


class ColumnMapScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        catalogue.CATALOGUE_DIR = root / "catalogue"
        catalogue.PROMPTS_DIR = root / "prompts"
        storage.DATA_DIR = root
        storage.USERS_DATA_DIR = root / "users"
        storage.PROMPTS_DIR = root / "prompts"
        catalogue.CATALOGUE_DIR.mkdir(parents=True, exist_ok=True)
        storage.PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        (catalogue.CATALOGUE_DIR / ".legacy_migrated").write_text("1\n", encoding="utf-8")

        from fastapi.testclient import TestClient
        import app

        self.app = app
        self.client = TestClient(app.app)
        self.client.post(
            "/login",
            data={"username": "mapuser", "password": "map-token"},
            follow_redirects=False,
        )
        self.client.post(
            "/upload",
            files={"csv_file": ("d.csv", "Messages,Client Id\nhello,c1\n", "text/csv")},
            data={"next": "/"},
            follow_redirects=False,
        )

    def _load(self, prompt_id):
        return self.client.get("/prompts/get", params={"id": prompt_id}).json()

    def _map(self, mapping, prompt):
        self.client.post(
            "/mapping/save",
            json={"mapping": mapping, "prompt_template": prompt, "input_template": ""},
        )

    def test_switching_prompts_drops_the_previous_fields(self):
        a_text = "Judge this.\n{CHAT_TRANSCRIPT}"
        b_text = "Look for a passport.\n{FULL_CONVERSATION}"
        a = catalogue.create_prompt("mapuser", "agent_eval", a_text, "")
        b = catalogue.create_prompt("mapuser", "outside__passport", b_text, "")

        self._load(a["id"])
        self._map({"CHAT_TRANSCRIPT": "Messages"}, a_text)

        loaded = self._load(b["id"])
        # The whole bug: {CHAT_TRANSCRIPT} used to still be listed here.
        self.assertEqual(loaded["required_inputs"], ["FULL_CONVERSATION"])
        self.assertEqual(loaded["column_map"], {})
        self.assertEqual(loaded["mapping_needed"], ["FULL_CONVERSATION"])

    def test_each_prompt_remembers_its_own_matches(self):
        a_text = "Judge this.\n{CHAT_TRANSCRIPT}"
        b_text = "Look for a passport.\n{FULL_CONVERSATION}"
        a = catalogue.create_prompt("mapuser", "agent_eval", a_text, "")
        b = catalogue.create_prompt("mapuser", "outside__passport", b_text, "")

        self._load(a["id"])
        self._map({"CHAT_TRANSCRIPT": "Messages"}, a_text)
        self._load(b["id"])
        self._map({"FULL_CONVERSATION": "Messages"}, b_text)

        back = self._load(a["id"])
        self.assertEqual(back["column_map"], {"CHAT_TRANSCRIPT": "Messages"})
        self.assertEqual(back["mapping_needed"], [])

        forward = self._load(b["id"])
        self.assertEqual(forward["column_map"], {"FULL_CONVERSATION": "Messages"})
        self.assertEqual(forward["mapping_needed"], [])


if __name__ == "__main__":
    unittest.main()
