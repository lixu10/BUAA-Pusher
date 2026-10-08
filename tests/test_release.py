import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("privacy_audit", ROOT / "scripts" / "privacy_audit.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


class ReleaseTest(unittest.TestCase):
    def test_private_paths_blocked(self):
        for path in ["data/backup.db", "data/startup.stdout.log", ".env", ".env.compose", "data/.master_key"]:
            self.assertTrue(privacy.risks(path, b""), path)

    def test_empty_example_configuration_allowed(self):
        for path in [".env.example", ".env.compose.example"]:
            self.assertEqual([], privacy.risks(path, b"PUAA_MASTER_KEY=\n"))
            self.assertTrue(privacy.risks(path, b"PUAA_MASTER_KEY=not-a-real-key\n"))

    def test_private_values_and_personal_email_blocked(self):
        self.assertIn("known local private value", privacy.risks("file.py", b"private-fixture", {"private-fixture"}))
        self.assertEqual([], privacy.risks("file.py", b"student@example.com"))
        personal = "fixture" + "@" + "qq.com"
        self.assertIn("personal email", privacy.risks("file.py", personal.encode()))

    def test_credential_pattern_blocked_without_real_secret(self):
        fake = ("ghp" + "_" + "x" * 36).encode()
        self.assertIn("github credential", privacy.risks("file.py", fake))

    def test_api_paths_are_not_local_home_directories(self):
        endpoint = b"https://byxt.buaa.edu.cn/jwapp/sys/homeapp/api/home/student/schoolCalendars.do"
        self.assertEqual([], privacy.risks("file.py", endpoint))
        local_path = ("C" + ":/" + "Users/" + "fixture").encode()
        self.assertIn("local home path", privacy.risks("file.py", local_path))

    def test_compose_defaults_and_durable_storage(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("${PUAA_COMPOSE_PORT:-11451}:8000", compose)
        self.assertNotIn("${PUAA_PORT", compose)
        self.assertIn("puaa_data:/app/data", compose)
        self.assertNotIn("./data:", compose)
        self.assertIn("healthcheck:", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("127.0.0.1", compose)

    def test_docker_context_is_allowlist(self):
        ignore = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertEqual("**", ignore[0])
        self.assertIn("!app/**/*.py", ignore)
        self.assertIn("!web/**", ignore)
        self.assertNotIn("!data/", ignore)
        self.assertIn("USER puaa:puaa", (ROOT / "Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
