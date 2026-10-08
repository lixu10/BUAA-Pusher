import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.main as main
from app.db import Database
from app.features import ACTIVE_SOURCES, event_available, rule_available
from app.services.aggregator import Aggregator
from app.services.event_state import public_event, reminder_matches


class CourseReminderTest(unittest.IsolatedAsyncioTestCase):
    async def test_internal_and_bulk_sync_never_create_iclass_client(self):
        with patch.object(main.school_sessions, "get", new=AsyncMock()) as get_session, \
                patch.object(main, "IClassAdapter", side_effect=AssertionError("iClass forbidden")):
            self.assertEqual(0, await main.sync_one(1, "iclass"))
            self.assertEqual({}, main.start_sync(1, {"iclass"}))
            get_session.assert_not_awaited()
        self.assertNotIn("iclass", ACTIVE_SOURCES)
        self.assertTrue(event_available({"source": "byxt", "kind": "course"}))
        self.assertFalse(event_available({"source": "byxt", "kind": "exam"}))
        self.assertFalse(event_available({"source": "iclass", "kind": "signin"}))
        self.assertFalse(rule_available({"profile": "signin", "scope": {"kinds": ["signin"], "sources": ["iclass"]}}))

    async def test_course_sync_preserves_parked_exam_and_signin_history(self):
        db = Database(":memory:"); db.init()
        uid = db.create_user("course@example.com", "Course", "hash")["id"]
        for source, key, kind in [("byxt", "exam-old", "exam"), ("byxt", "course-old", "course"), ("iclass", "signin-old", "signin")]:
            db.upsert_event(uid, {"source": source, "external_id": key, "kind": kind, "title": key})
        adapter = SimpleNamespace(id="byxt", label="课程提醒", cleanup_kinds=("course",),
            sync=AsyncMock(return_value=[{"source": "byxt", "external_id": "course-new", "kind": "course", "title": "新课程"}]))
        await Aggregator(db, uid, [adapter]).sync()
        self.assertEqual({"exam-old", "signin-old", "course-new"}, {e["external_id"] for e in db.list_events(uid)})
        # Empty successful course results also leave parked kinds untouched.
        adapter.sync.return_value = []
        await Aggregator(db, uid, [adapter]).sync()
        self.assertEqual({"exam-old", "signin-old"}, {e["external_id"] for e in db.list_events(uid)})
        db.close()

    def test_course_status_never_claims_attendance_or_submission(self):
        now = datetime.now(UTC)
        upcoming = {"kind": "course", "status": "upcoming", "starts_at": (now+timedelta(hours=1)).isoformat(), "ends_at": (now+timedelta(hours=2)).isoformat()}
        self.assertEqual("待上课", public_event(upcoming)["status_label"])
        self.assertEqual("not_applicable", public_event(upcoming)["completion_state"])
        ongoing = {**upcoming, "starts_at": (now-timedelta(minutes=1)).isoformat()}
        self.assertEqual("上课中", public_event(ongoing)["status_label"])
        ended = {**ongoing, "ends_at": (now-timedelta(seconds=1)).isoformat()}
        self.assertEqual("已结束", public_event(ended)["status_label"])
        self.assertFalse(reminder_matches({}, ended, now))
        self.assertTrue(reminder_matches({}, upcoming, now))
        self.assertFalse(reminder_matches({}, {**upcoming, "ignored": True}, now))
