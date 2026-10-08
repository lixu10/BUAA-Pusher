import unittest

from app.services.source_health import source_transition_message


class SourceHealthTest(unittest.TestCase):
    def test_notifies_failure_once_and_recovery(self):
        healthy = {"id": "spoc", "label": "SPOC", "status": "healthy", "enabled": True}
        failed = {
            "id": "spoc", "label": "SPOC", "status": "login_required",
            "detail": "token expired", "enabled": True,
        }
        self.assertEqual(("SPOC需要重新登录", "token expired"), source_transition_message(healthy, failed))
        self.assertIsNone(source_transition_message(failed, failed))
        self.assertEqual(
            ("SPOC已恢复", "数据同步已恢复正常"),
            source_transition_message(failed, healthy),
        )

    def test_initial_and_disabled_sources_stay_quiet(self):
        current = {"id": "judge", "label": "JUDGE", "status": "error", "enabled": True}
        self.assertIsNone(source_transition_message(None, current))
        current["enabled"] = False
        self.assertIsNone(source_transition_message({"status": "healthy"}, current))


if __name__ == "__main__":
    unittest.main()
