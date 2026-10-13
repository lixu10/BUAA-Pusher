import unittest

from app.services.source_health import source_transition_message


class SourceHealthTest(unittest.TestCase):
    def test_notifies_failure_once_and_recovery(self):
        healthy = {"id": "spoc", "label": "SPOC", "status": "healthy", "enabled": True}
        failed = {
            "id": "spoc", "label": "SPOC", "status": "login_required",
            "detail": "token expired", "enabled": True,
        }
        title, body = source_transition_message(healthy, failed)
        self.assertEqual("SPOC需要重新登录", title)
        self.assertIn("原因：token expired", body)
        self.assertIn("缓存已保留", body)
        self.assertIsNone(source_transition_message(failed, failed))
        title, body = source_transition_message(failed, healthy)
        self.assertEqual("SPOC已恢复", title)
        self.assertIn("数据同步已恢复正常", body)

    def test_initial_and_disabled_sources_stay_quiet(self):
        current = {"id": "judge", "label": "JUDGE", "status": "error", "enabled": True}
        self.assertIsNone(source_transition_message(None, current))
        current["enabled"] = False
        self.assertIsNone(source_transition_message({"status": "healthy"}, current))


if __name__ == "__main__":
    unittest.main()
