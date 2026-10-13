import unittest

from app.services.notification_content import message_body


class DetailedContentTest(unittest.TestCase):
    def test_assignment_dates_scores_comments_and_body_are_detailed_but_private_fields_omitted(self):
        event={"kind":"assignment","source":"spoc","title":"作业", "due_at":"2030-09-01T14:00:00+00:00","status":"done","metadata":{
            "course_name":"课程", "teacher":"教师", "assignment_type":"作业", "earned_score":0, "max_score":100,
            "graded_at":"2030-08-30T10:00:00+08:00", "grade_comment":"评语", "description":"正文"*1000,
            "password":"SECRET-FIXTURE", "token":"SECRET-FIXTURE", "studentNo":"SECRET-FIXTURE"}}
        body=message_body({"definition":{"profile":"spoc-assignment"}, "rule_name":"截止前两小时"},event)
        for part in ["2030-09-01 22:00:00", "课程：课程", "教师：教师", "已提交", "得分：0 / 100", "教师评语：评语", "完整内容见详情", "学校入口：https://spoc.buaa.edu.cn", "规则：截止前两小时"]:
            self.assertIn(part,body)
        self.assertNotIn("SECRET-FIXTURE",body)

    def test_judge_progress_and_known_problem_score_are_preserved(self):
        event={"kind":"assignment","source":"judge","title":"任务","status":"partial","metadata":{"submission_status":"partial","total_problems":4,"submitted_count":2,"score":30,"max_score":100}}
        body=message_body({"definition":{}},event)
        self.assertIn("部分提交",body); self.assertIn("提交进度：2 / 4 题",body); self.assertIn("得分：30 / 100",body)

    def test_boya_course_and_seat_notifications_include_actionable_data(self):
        event={"kind":"boya_catalog","source":"bykc","title":"美育","starts_at":"2030-09-01T15:00:00+08:00","ends_at":"2030-09-01T17:00:00+08:00", "location":"教室", "metadata":{
            "teacher":"教师", "category_names":["博雅","美育"], "campus_names":["学院路"], "sign_type":"2", "remaining":3,
            "current_count":7,"max_count":10,"selected":False,"enrollment_starts_at":"2030-08-31T12:00:00+08:00","enrollment_ends_at":"2030-09-01T12:00:00+08:00"}}
        body=message_body({"definition":{"profile":"boya-seats"},"network_mode":"webvpn"},event)
        for part in ["名额回流","分类：博雅 / 美育","适用校区：学院路","签到＋签退","余量：3","7 / 10","未报名","https://d.buaa.edu.cn/https/"]: self.assertIn(part,body)
        event["kind"]="boya";event["metadata"]["sign_windows"]={"signStartDate":"2030-09-01T14:50:00+08:00"}
        self.assertIn("签到开始：2030-09-01 14:50:00",message_body({"definition":{"profile":"boya-course"}},event))

    def test_course_change_explains_before_after_with_local_times(self):
        before={"kind":"course","source":"byxt","title":"课程","starts_at":"2030-09-01T08:00:00+08:00","location":"原教室","metadata":{"teacher":"原教师"}}
        after={**before,"starts_at":"2030-09-01T10:00:00+08:00","location":"新教室","metadata":{"teacher":"新教师"}}
        body=message_body({"definition":{"profile":"course-change"},"change":{"type":"course-change","categories":["time","location","teacher"],"before":before,"after":after}},after)
        self.assertIn("原时间：2030-09-01 08:00:00",body); self.assertIn("现时间：2030-09-01 10:00:00",body)
        self.assertIn("原教室 → 新教室",body);self.assertIn("原教师 → 新教师",body)
