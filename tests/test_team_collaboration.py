from contextlib import closing
import json
from pathlib import Path
import secrets
import tempfile
import unittest

from taskboard.cloud.accounts import AccountStore
from taskboard.cloud.team_directory import TeamDirectory
from taskboard.cloud.team_service import TeamService
from taskboard.http_security import HTTPRequestError


def module(key='api', deps=None):
    return {'key':key,'title':key,'goal':'提供 '+key,'scope':['实现 '+key],
            'out_of_scope':[],'acceptance_criteria':['可以使用 '+key],
            'interfaces':[],'depends_on':deps or []}


class TeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        accounts = AccountStore(self.home)
        self.ids = {}
        self.tokens = {}
        for name in ('alice','bob','carol'):
            account,token = accounts.initialize(name,'correct horse battery staple',secrets.token_hex(16),secrets.token_urlsafe(32))
            self.ids[name]=account['id']
            self.tokens[name]=token
        self.directory=TeamDirectory(accounts)
        self.team=self.directory.create(self.ids['alice'],'项目组')['id']
        self.directory.add(self.team,self.ids['alice'],'bob','developer')
        root=TeamService(self.home/'teams'/self.team,'https://test.invalid',self.team,self.directory)
        self.a=root.for_actor(self.ids['alice'])
        self.b=root.for_actor(self.ids['bob'])
        self.pid=self.a.create_project({'name':'产品','repository':'https://example.test/project.git','baseline':'a'*40})['id']
        self.a.grant_project({'project_id':self.pid,'account_id':self.ids['bob'],'modules':['api','web'],'capacity':2})
        self.a.preferences({'project_id':self.pid,'auto_analysis':True,'token_budget':10000})
        self.b.preferences({'project_id':self.pid,'auto_analysis':True,'token_budget':10000})

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self):
        return self.a.upload_requirement({'project_id':self.pid,'title':'用户中心','content':'登录及个人资料','coordinator_id':self.ids['bob']})['requirement_id']

    def analyzed(self, specs=None):
        rid=self.upload()
        job=self.a.claim_analysis()
        self.a.analysis_result({'job_id':job['id'],'lease':job['lease'],'result':{
            'summary':'需要接口模块','risks':[],'questions':[],'modules':specs or [module()]}})
        return rid

    def submitted(self):
        rid=self.analyzed()
        tids=self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':[module()]})['task_ids']
        return rid,tids[0]

    def assigned(self):
        rid,tid=self.submitted()
        candidates=self.a.candidates({'task_id':tid,'revision':1})
        self.a.assign({**candidates,'owner_account_id':self.ids['bob']})
        return rid,tid

    def test_personal_data_is_not_in_team_runtime_and_nonmembers_denied(self):
        with self.assertRaises(HTTPRequestError):
            self.a.for_actor(self.ids['carol'])
        self.directory.add(self.team,self.ids['alice'],'carol','developer')
        c=self.a.for_actor(self.ids['carol'])
        rid=self.upload()
        self.assertEqual([],c.snapshot()['requirements'])
        with c.db.connection() as db, self.assertRaises(HTTPRequestError):
            c.requirement(rid,db)
        self.assertNotEqual(self.a.data_home,self.home/'tenants'/self.ids['alice'])

    def test_product_analysis_and_confirmation_precede_module_tasks(self):
        rid=self.upload()
        with self.assertRaises(ValueError):
            self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':[module()]})
        self.assertEqual([],self.a.snapshot()['tasks'])
        with self.assertRaises(HTTPRequestError):
            self.a.finalize_task_intake({'intake_kind':'task','auto_dispatch':True})
        with self.assertRaises(HTTPRequestError):
            self.a.create_task({'title':'bypass','project':'/tmp/x','status':'ready'})

    def test_module_only_submission_preserves_core_draft_and_dependency(self):
        specs=[module('api'),module('web',['api'])]
        rid=self.analyzed(specs)
        self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':specs})
        rows=self.a.snapshot()['tasks']
        self.assertEqual(2,len(rows))
        self.assertTrue(all(r['status']=='draft' and not r['auto_dispatch'] for r in rows))
        with self.a.db.connection() as db:
            self.assertEqual(1,db.execute('SELECT COUNT(*) FROM task_relations').fetchone()[0])
        with self.assertRaises(ValueError):
            self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':specs})
        self.assertEqual(2,len(self.a.snapshot()['tasks']))

    def test_invalid_dependency_rolls_back_entire_submission(self):
        rid=self.analyzed()
        with self.assertRaises(ValueError):
            self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':[module('api',['web']),module('web',['api'])]})
        self.assertEqual([],self.a.snapshot()['tasks'])

    def test_assignment_checks_capacity_generation_and_human_actor(self):
        rid,tid=self.submitted()
        result=self.a.candidates({'task_id':tid,'revision':1})
        self.assertEqual([self.ids['bob']],[x['id'] for x in result['candidates']])
        self.a.grant_project({'project_id':self.pid,'account_id':self.ids['bob'],'modules':['web'],'capacity':2})
        with self.assertRaises(ValueError):
            self.a.assign({**result,'owner_account_id':self.ids['bob']})
        self.assertIsNone(self.a.snapshot()['tasks'][0]['owner_account_id'])

    def test_receipt_is_not_owner_confirmation(self):
        rid,tid=self.assigned()
        job=self.b.claim_analysis()
        self.assertEqual(tid,job['task_id'])
        with self.assertRaises(ValueError):
            self.b.execution_claim({'task_id':tid,'revision':2})
        self.assertEqual('draft',self.b.snapshot()['tasks'][0]['status'])
        self.assertIsNone(self.b.claim_analysis())

    def test_analysis_callback_cannot_cross_identity_or_use_stale_lease(self):
        rid,tid=self.assigned()
        job=self.b.claim_analysis()
        args={'job_id':job['id'],'lease':job['lease'],'result':{'summary':'分析','risks':[],'questions':[],'contract':{}}}
        with self.assertRaises(HTTPRequestError):
            self.a.analysis_result(args)
        with self.assertRaises(HTTPRequestError):
            self.b.analysis_result({**args,'lease':'wrong'})
        self.b.analysis_result(args)
        self.assertEqual({'ok':True},self.b.analysis_result(args))
        self.assertEqual('awaiting_owner_confirmation',self.b.snapshot()['tasks'][0]['phase'])

    def test_questions_preserve_owner_and_block_confirmation(self):
        rid,tid=self.assigned()
        result=self.b.ask({'task_id':tid,'revision':2,'kind':'awaiting_clarification','question':'是否支持邮箱登录？'})
        with self.assertRaises(ValueError):
            self.b.confirm_owner({'task_id':tid,'revision':2})
        with self.assertRaises(HTTPRequestError):
            self.b.answer({'question_id':result['question_id'],'version':1,'answer':'支持','scope_changed':False})
        self.a.answer({'question_id':result['question_id'],'version':1,'answer':'按原文，只支持账号登录','scope_changed':False})
        task=self.b.snapshot()['tasks'][0]
        self.assertEqual(self.ids['bob'],task['owner_account_id'])
        self.assertIsNone(task['confirmed_revision'])

    def test_expired_analysis_is_uncertain_and_never_recreated(self):
        self.upload()
        job=self.a.claim_analysis()
        with self.a.db.transaction() as db:
            db.execute('UPDATE team_jobs SET expires_at=0 WHERE id=?',(job['id'],))
        self.assertIsNone(self.a.claim_analysis())
        self.assertEqual('uncertain',self.a.snapshot()['jobs'][0]['status'])
        with self.assertRaises(ValueError):
            self.a.start_analysis({'job_id':job['id']})

    def test_unfinished_tasks_and_missing_integration_cannot_complete_requirement(self):
        rid,tid=self.assigned()
        with self.assertRaises(ValueError):
            self.b.confirm_delivery({'task_id':tid,'revision':2})
        with self.assertRaises(ValueError):
            self.b.confirm_integration({'requirement_id':rid,'version':1,'integration_revision':'b'*40,'evidence':'通过'})
        with self.assertRaises(ValueError):
            self.a.accept_requirement({'requirement_id':rid,'version':1})

    def test_uncertain_recovery_requires_human_stop_and_rejects_old_lease(self):
        self.upload()
        old=self.a.claim_analysis()
        with self.assertRaises(ValueError):
            self.a.resolve_analysis({'job_id':old['id'],'reason':'retry'})
        self.a.resolve_analysis({'job_id':old['id'],'reason':'已核对本机会话并停止','local_session_stopped':True})
        self.a.start_analysis({'job_id':old['id']})
        new=self.a.claim_analysis()
        self.assertNotEqual(old['lease'],new['lease'])
        with self.assertRaises(HTTPRequestError):
            self.a.analysis_result({'job_id':old['id'],'lease':old['lease'],'error':'late'})

    def test_revision_invalidates_changed_modules_and_dependants_only(self):
        specs=[module('api'),module('web',['api']),module('docs')]
        rid=self.analyzed(specs)
        self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':specs})
        changed=[{**specs[0],'goal':'new api'},specs[1],specs[2]]
        result=self.a.publish_revision({'requirement_id':rid,'version':1,'content':'登录及个人资料','reason':'接口范围补充','modules':changed})
        self.assertEqual(['api','web'],result['affected_modules'])
        revisions={t['requirement_task_key']:t['revision'] for t in self.a.snapshot()['tasks']}
        self.assertEqual({'api':2,'web':2,'docs':1},revisions)
        with self.a.db.connection() as db:
            self.assertEqual(2,db.execute('SELECT COUNT(*) FROM team_versions').fetchone()[0])

    def test_removed_module_is_not_completed_and_can_be_restored(self):
        specs=[module('api'),module('web')]
        rid=self.analyzed(specs)
        self.a.submit_requirement({'requirement_id':rid,'version':1,'modules':specs})
        self.a.publish_revision({'requirement_id':rid,'version':1,'content':'登录及个人资料','reason':'移除web','modules':[specs[0]]})
        web=next(t for t in self.a.snapshot()['tasks'] if t['requirement_task_key']=='web')
        self.assertEqual('retired',web['handling'])
        self.assertEqual('cancelled',web['status'])
        self.a.publish_revision({'requirement_id':rid,'version':2,'content':'登录及个人资料','reason':'恢复web','modules':specs})
        restored=next(t for t in self.a.snapshot()['tasks'] if t['id']==web['id'])
        self.assertEqual('draft',restored['status'])
        self.assertEqual('',restored['handling'])

    def test_attachment_bytes_are_immutable_and_project_authorized(self):
        import base64
        rid=self.a.upload_requirement({'project_id':self.pid,'title':'附件需求','content':'目标','coordinator_id':self.ids['bob'],
            'attachments':[{'name':'../scope.md','mime':'text/markdown','content_base64':base64.b64encode(b'# Scope').decode()}]})['requirement_id']
        attachment=self.a.snapshot()['requirements'][0]['attachments'][0]
        self.assertEqual(b'# Scope',base64.b64decode(self.b.read_attachment({'attachment_id':attachment['id']})['content_base64']))
        self.directory.add(self.team,self.ids['alice'],'carol','developer')
        with self.assertRaises(HTTPRequestError):
            self.a.for_actor(self.ids['carol']).read_attachment({'attachment_id':attachment['id']})
        self.assertFalse((self.home/'scope.md').exists())
        job=self.a.claim_analysis()
        self.assertEqual(attachment['sha256'],job['attachments'][0]['sha256'])

    def test_removal_denies_existing_service_and_project_callbacks(self):
        self.directory.add(self.team,self.ids['alice'],'carol','developer')
        self.a.grant_project({'project_id':self.pid,'account_id':self.ids['carol'],'modules':['api']})
        c=self.a.for_actor(self.ids['carol'])
        self.a.revoke_project({'project_id':self.pid,'account_id':self.ids['carol']})
        with self.assertRaises(HTTPRequestError): c.project(self.pid)
        self.directory.remove(self.team,self.ids['alice'],self.ids['carol'])
        with self.assertRaises(HTTPRequestError): c.snapshot()

    def test_product_callback_replay_is_normalized_without_duplicate_questions(self):
        self.upload();job=self.a.claim_analysis()
        payload={'job_id':job['id'],'lease':job['lease'],'result':{'summary':'结论','risks':[],'questions':['确认边界'],'modules':[module()],'contract':{}}}
        self.a.analysis_result(payload)
        self.a.analysis_result(payload)
        self.assertEqual(1,len(self.a.snapshot()['questions']))

    def test_new_module_revision_is_atomic_and_not_executable(self):
        rid,tid=self.submitted()
        result=self.a.publish_revision({'requirement_id':rid,'version':1,'content':'登录及个人资料','reason':'新增界面模块','modules':[module(),module('web',['api'])]})
        self.assertEqual(['web'],result['affected_modules'])
        rows=self.a.snapshot()['tasks']
        self.assertEqual(2,len(rows))
        added=next(t for t in rows if t['requirement_task_key']=='web')
        self.assertEqual('draft',added['status'])
        self.assertIsNone(added['owner_account_id'])
        self.assertFalse(added['auto_dispatch'])
        with self.a.db.connection() as db:
            self.assertEqual(tid,db.execute("SELECT target_task_id FROM task_relations WHERE source_task_id=? AND relation_type='depends_on'",(added['id'],)).fetchone()[0])


if __name__=='__main__':
    unittest.main()
