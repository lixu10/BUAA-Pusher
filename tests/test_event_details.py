import unittest
from unittest.mock import AsyncMock

from app.adapters.judge import JudgeAdapter
from app.adapters.spoc import SpocAdapter, DETAIL_URL, SUBMISSION_URL
from app.adapters.text import plain_text


class DetailTextTest(unittest.TestCase):
    def test_html_is_inert_and_paragraphs_and_table_cells_remain_readable(self):
        text = plain_text('<p>第一段 &amp; 内容<br>第二行</p><script>secret()</script>'
                          '<style>body{}</style><table><tr><td>题目</td><td>10</td></tr></table>'
                          '<img src="https://example.com/private">')
        self.assertIn('第一段 & 内容\n第二行', text)
        self.assertIn('题目 10', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('https:', text)
        self.assertNotIn('body{}', text)

    def test_plain_text_and_long_content_are_not_truncated(self):
        text = '说明\n' + '长正文' * 12000 + '\nEND'
        self.assertEqual(text, plain_text(text))
        self.assertEqual('if (x < 3 && y > 2)', plain_text('if (x < 3 &amp;&amp; y &gt; 2)'))
        self.assertIsNone(plain_text(None))

    def test_judge_retains_problem_scores_without_assuming_accepted_is_full_marks(self):
        event = JudgeAdapter(object())._event('1', '测试课程', '2', '测试作业', '''
            <p>作业时间：2030-09-01 08:00 至 2030-09-20 23:59</p>
            <table><tr><td>1</td><td>零分题</td><td>10</td><td>已提交 得分：0</td></tr>
            <tr><td>2</td><td>通过题</td><td>20</td><td>Accepted</td></tr>
            <tr><td>3</td><td>未知题</td><td>30</td><td>状态未知</td></tr></table>
            <script>not_a_description()</script>''')
        meta = event['metadata']
        self.assertEqual('0', meta['problems'][0]['earned_score'])
        self.assertIsNone(meta['problems'][1]['earned_score'])
        self.assertEqual('unknown', meta['problems'][2]['submission_status'])
        self.assertEqual(3, meta['total_problems'])
        self.assertEqual(2, meta['submitted_count'])
        self.assertEqual('partial', event['status'])
        self.assertNotIn('not_a_description', meta['description'])


class SpocDetailTest(unittest.IsolatedAsyncioTestCase):
    async def sync_fixture(self, detail, submission=None, listing=None):
        adapter = SpocAdapter(AsyncMock())
        adapter._post = AsyncMock(side_effect=[{'mrxq': 'term'}, {'list': [
            {'zyid': '1', 'zymc': '测试作业', 'tjzt': '1', **(listing or {})}]}])
        adapter._get = AsyncMock(return_value=[])
        adapter._get_object = AsyncMock(side_effect=[detail, submission])
        events = await adapter._sync_authenticated()
        self.assertEqual([DETAIL_URL, SUBMISSION_URL],
                         [call.args[0] for call in adapter._get_object.call_args_list])
        return events[0]['metadata']

    async def test_long_description_and_feedback_use_existing_read_only_requests(self):
        body = '正文' * 12000
        meta = await self.sync_fixture({'zynr': f'<p>{body}</p>', 'zyfs': 100},
                                      {'pf': 0, 'py': '<p>第一行</p><p>第二行</p><script>bad()</script>',
                                       'pyfs': '2', 'tjzt': '1'})
        self.assertEqual(body, meta['description'])
        self.assertEqual(0, meta['earned_score'])
        self.assertEqual(100, meta['max_score'])
        self.assertEqual('第一行\n\n第二行', meta['grade_comment'])

    async def test_missing_detail_body_preserves_list_body(self):
        meta = await self.sync_fixture({'zyfs': 100}, listing={'zynr': '<p>列表正文</p>'})
        self.assertEqual('列表正文', meta['description'])

    async def test_explicit_empty_detail_does_not_keep_stale_list_body(self):
        meta = await self.sync_fixture({'zynr': ''}, listing={'zynr': '旧正文'})
        self.assertIsNone(meta['description'])
