import os
from pathlib import Path
import subprocess
import sys
import tempfile

from cryptography.fernet import Fernet
from django.conf import settings
from django.test import SimpleTestCase
from dotenv import dotenv_values

from scripts.init_env import initialize_env


class EnvironmentSetupTest(SimpleTestCase):
    def test_copied_example_gets_valid_keys_without_changing_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text((settings.BASE_DIR / ".env.example").read_text())
            generated = initialize_env(path)
            values = dotenv_values(path)
            self.assertEqual(set(generated), {"DJANGO_SECRET_KEY", "OLT_CREDENTIAL_KEY"})
            self.assertGreaterEqual(len(values["DJANGO_SECRET_KEY"]), 64)
            Fernet(values["OLT_CREDENTIAL_KEY"].encode())
            self.assertEqual(values["DJANGO_DEBUG"], "0")
            self.assertEqual(values["ENABLE_LIVE_OLT"], "0")

    def test_existing_values_and_comments_are_preserved_when_repairing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            existing_key = Fernet.generate_key().decode()
            path.write_text(f"# Configuração personalizada\nDJANGO_SECRET_KEY=\nOLT_CREDENTIAL_KEY={existing_key}\nDJANGO_ALLOWED_HOSTS=erp.example\n")
            self.assertEqual(initialize_env(path), ["DJANGO_SECRET_KEY"])
            values = dotenv_values(path)
            self.assertEqual(values["OLT_CREDENTIAL_KEY"], existing_key)
            self.assertEqual(values["DJANGO_ALLOWED_HOSTS"], "erp.example")
            self.assertIn("# Configuração personalizada", path.read_text())

    def test_new_file_is_private_and_repeated_setup_does_not_rotate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            initialize_env(path)
            original = path.read_bytes()
            self.assertEqual(initialize_env(path), [])
            self.assertEqual(path.read_bytes(), original)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_and_whitespace_keys_are_filled(self):
        for content in ("", "DJANGO_SECRET_KEY='  '\nOLT_CREDENTIAL_KEY=\"\"\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".env"
                path.write_text(content)
                self.assertEqual(len(initialize_env(path)), 2)
                self.assertTrue(dotenv_values(path)["DJANGO_SECRET_KEY"])

    def test_empty_key_fails_at_startup_with_actionable_error(self):
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            cwd=settings.BASE_DIR,
            env=os.environ | {"DJANGO_SECRET_KEY": ""},
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY ausente ou vazia", result.stderr)
        self.assertIn("scripts/init_env.py", result.stderr)
