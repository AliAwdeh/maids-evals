"""Isolation checks for per-user encrypted API keys. Run: python tests/test_credentials.py"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-fernet-derivation")

import storage  # noqa: E402


class SecretIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        storage.USERS_DATA_DIR = Path(self.tmp.name) / "users"

    def test_round_trip_per_user_and_provider(self):
        storage.save_user_secret("alice", "openai", "sk-alice-openai")
        storage.save_user_secret("alice", "gemini", "sk-alice-gemini")
        storage.save_user_secret("bob", "openai", "sk-bob-openai")
        self.assertEqual(storage.load_user_secret("alice", "openai"), "sk-alice-openai")
        self.assertEqual(storage.load_user_secret("alice", "gemini"), "sk-alice-gemini")
        self.assertEqual(storage.load_user_secret("bob", "openai"), "sk-bob-openai")
        self.assertIsNone(storage.load_user_secret("bob", "gemini"))
        self.assertIsNone(storage.load_user_secret("alice", "langcc"))

    def test_files_stay_under_that_user(self):
        storage.save_user_secret("alice", "openai", "sk-only-alice")
        alice = storage.secrets_path("alice")
        bob = storage.secrets_path("bob")
        self.assertTrue(alice.is_file())
        self.assertFalse(bob.is_file())
        raw = alice.read_text(encoding="utf-8")
        self.assertNotIn("sk-only-alice", raw)
        self.assertIn("openai", raw)

    def test_forget_only_own_provider(self):
        storage.save_user_secret("alice", "openai", "sk-a")
        storage.save_user_secret("alice", "langcc", "sk-b")
        self.assertTrue(storage.delete_user_secret("alice", "openai"))
        self.assertIsNone(storage.load_user_secret("alice", "openai"))
        self.assertEqual(storage.load_user_secret("alice", "langcc"), "sk-b")

    def test_ollama_rejected(self):
        with self.assertRaises(ValueError):
            storage.save_user_secret("alice", "ollama", "nope")
        self.assertIsNone(storage.load_user_secret("alice", "ollama"))

    def test_not_in_users_json_shape(self):
        storage.save_user_secret("alice", "openai", "sk-hidden")
        self.assertTrue(storage.secrets_path("alice").is_file())
        self.assertNotEqual(storage.secrets_path("alice").name, "users.json")


if __name__ == "__main__":
    unittest.main()
