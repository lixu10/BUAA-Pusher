import unittest
from unittest.mock import Mock

import httpx

from app.adapters.judge import JudgeAdapter
from app.db import Database
from app.services.aggregator import Aggregator


class JudgeIntegrityTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_empty_response_does_not_delete_old_events_or_advance_baseline(self):
        for page in ('', 'upstream failure!', '<html><body>学校服务暂不可用</body></html>'):
            with self.subTest(page=page):
                db=Database(':memory:');db.init()
                uid=db.create_user('judge-integrity@example.com','Fixture','hash')['id']
                old={'source':'judge','external_id':'fixture','kind':'assignment','title':'已同步作业','due_at':'2030-10-30T22:00:00+08:00'}
                db.upsert_event(uid,old)
                def handler(request):
                    if request.url.host=='sso.buaa.edu.cn':
                        return httpx.Response(302,headers={'Location':'https://judge.buaa.edu.cn/'})
                    return httpx.Response(200,text=page)
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True)
                school=Mock(authenticated=True,client=client)
                try:
                    await Aggregator(db,uid,[JudgeAdapter(school)]).sync()
                    self.assertEqual('error',db.get_source(uid,'judge')['status'])
                    self.assertIn('旧数据已保留',db.get_source(uid,'judge')['detail'])
                    self.assertEqual(1,len(db.list_events(uid)))
                    self.assertIsNone(db.get_sync_snapshot(uid,'judge'))
                finally:
                    await client.aclose();db.close()

    async def test_known_empty_lists_and_nonempty_links_remain_valid(self):
        self.assertEqual([],JudgeAdapter._read_list('<p>暂无课程</p>','courseID','课程',exclude='0'))
        self.assertEqual([],JudgeAdapter._read_list('<p>教师尚未发布作业</p>','assignID','作业'))
        self.assertEqual([('1','课程')],JudgeAdapter._read_list('<a href="courselist.jsp?courseID=1">课程</a>','courseID','课程'))
        with self.assertRaisesRegex(RuntimeError,'作业列表'):
            JudgeAdapter._read_list('upstream failure!','assignID','作业')


if __name__=='__main__':
    unittest.main()
