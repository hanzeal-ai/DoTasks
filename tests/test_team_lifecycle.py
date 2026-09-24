import json
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_team_collaboration as fixtures
from taskboard.team_local import bind_project, task_workspace, workspace_request


class TeamLifecycleTest(unittest.TestCase):
    tearDown=fixtures.TeamTest.tearDown
    upload=fixtures.TeamTest.upload
    analyzed=fixtures.TeamTest.analyzed
    submitted=fixtures.TeamTest.submitted
    assigned=fixtures.TeamTest.assigned
    def setUp(self):
        fixtures.TeamTest.setUp(self)
        self.repo=self.home/'repo'
        self.repo.mkdir()
        (self.repo/'feature.py').write_text('def feature(): return False\n')
        self.git('init','-q')
        self.git('config','user.email','test@example.invalid')
        self.git('config','user.name','Test')
        self.git('remote','add','origin','https://example.test/project.git')
        self.git('add','.')
        self.git('commit','-qm','initial')
        baseline=self.git('rev-parse','HEAD').strip()
        with self.a.db.transaction() as db:
            db.execute('UPDATE team_projects SET baseline=?',(baseline,))
        self.project=self.b.project(self.pid)
        bind_project(self.home,self.team,self.project,self.repo)
        for service in (self.a,self.b):
            service._workspace_rpc=lambda action,_service=service,**args: workspace_request(self.home,{
                'team_id':self.team,'project':self.project,'task_id':_service.execution_scope_task_id,
                'revision':2,
                'action':action,'arguments':args})['result']

            service._integration_rpc=lambda req,project,members,revision,command: workspace_request(self.home,{'team_id':self.team,'task_id':req['id'],'revision':req['version'],'project':project,'action':'integration','arguments':{'members':members,'revision':revision,'command':command}})['result']

    def git(self,*args,root=None):
        return subprocess.run(['git','-C',str(root or self.repo),*args],capture_output=True,text=True,check=True).stdout

    def analyzed_owner(self):
        rid,tid=self.assigned()
        job=self.b.claim_analysis()
        contract={
            'location_evidence':{'tool':'source_match','project_path':'/teams/'+self.team+'/'+self.pid,'commands':['rg -n feature feature.py'],'query':'feature','files':['feature.py'],'symbols':['feature']},
            'targets':[{'file':'feature.py','mode':'modify','symbols':['feature'],'reason':'提供功能','tasks':[{'symbol':'feature','action':'实现功能'}]}],
            'acceptance_plan':[{'criterion':'可以使用 api','file':'feature.py','symbol':'feature','method':'运行断言','expected':'成功','check_type':'automated','command':'python3 -B -c "from feature import feature; assert feature()"'}],
            'quality_gates':{'code_review':{'required':True,'reason':'生产代码'}},
            'review_checks':[{'id':'correctness','description':'代码正确','kind':'code'}]}
        self.b.analysis_result({'job_id':job['id'],'lease':job['lease'],'result':{
            'summary':'实现功能','risks':[],'questions':[],'contract':contract}})
        self.b.confirm_owner({'task_id':tid,'revision':2})
        return rid,tid

    def tool(self,tid,name,**args):
        return self.b.execution_tool({'task_id':tid,'revision':2,'name':name,'arguments':args})

    def test_complete_delivery_uses_real_git_and_verification(self):
        rid,tid=self.analyzed_owner()
        dispatch=self.b.execution_claim({'task_id':tid,'revision':2})
        self.assertIsNotNone(dispatch)
        self.tool(tid,'bind_native_dispatch',run_id=dispatch['run_id'],thread_id='dev-thread',dispatch_attempt_id=dispatch['dispatch_attempt_id'])
        workspace=Path(task_workspace(self.home,{'team_id':self.team,'project':self.project,'task_id':tid,'revision':2}))
        (workspace/'feature.py').write_text('def feature(): return True\n')
        self.git('add','.',root=workspace)
        self.git('commit','-qm','implementation',root=workspace)
        output=self.git('rev-parse','HEAD',root=workspace).strip()
        self.tool(tid,'submit_task_delivery',run_id=dispatch['run_id'],delivery_summary='已实现',verification_result='聚焦断言通过',changed_locations=[{'file':'feature.py','symbols':['feature']}],acceptance_evidence=[{'criterion':'可以使用 api','evidence':'实际执行断言'}])
        review=self.b.execution_claim({'task_id':tid,'revision':2})
        self.assertEqual('code_review',review['role'])
        self.tool(tid,'bind_native_dispatch',run_id=review['run_id'],thread_id='independent-review',dispatch_attempt_id=review['dispatch_attempt_id'])
        self.tool(tid,'review_code',task_id=tid,run_id=review['run_id'],verdict='pass',passed_items=['correctness'])
        self.b.confirm_delivery({'task_id':tid,'revision':2})
        self.git('merge','--ff-only',output)
        self.b.confirm_integration({'command':'python3 -B -c "from feature import feature; assert feature()"','requirement_id':rid,'version':1,'integration_revision':output,'evidence':'全部模块断言已通过'})
        self.a.accept_requirement({'requirement_id':rid,'version':1})
        self.assertEqual('done',self.a.snapshot()['requirements'][0]['status'])
        self.assertEqual(output,self.a.snapshot()['tasks'][0]['delivery']['output_revision'])

    def test_code_review_cannot_bind_the_development_session(self):
        rid,tid=self.analyzed_owner()
        dispatch=self.b.execution_claim({'task_id':tid,'revision':2})
        self.tool(tid,'bind_native_dispatch',run_id=dispatch['run_id'],thread_id='dev-only',dispatch_attempt_id=dispatch['dispatch_attempt_id'])
        workspace=Path(task_workspace(self.home,{'team_id':self.team,'project':self.project,'task_id':tid,'revision':2}))
        (workspace/'feature.py').write_text('def feature(): return True\n')
        self.git('add','.',root=workspace);self.git('commit','-qm','implementation',root=workspace)
        self.tool(tid,'submit_task_delivery',run_id=dispatch['run_id'],delivery_summary='已实现',verification_result='断言通过',changed_locations=[{'file':'feature.py','symbols':['feature']}],acceptance_evidence=[{'criterion':'可以使用 api','evidence':'断言'}])
        review=self.b.execution_claim({'task_id':tid,'revision':2})
        with self.assertRaisesRegex(ValueError,'独立'):
            self.tool(tid,'bind_native_dispatch',run_id=review['run_id'],thread_id='dev-only',dispatch_attempt_id=review['dispatch_attempt_id'])

    def test_stop_requires_local_confirmation_and_old_run_cannot_deliver(self):
        rid,tid=self.analyzed_owner()
        dispatch=self.b.execution_claim({'task_id':tid,'revision':2})
        self.tool(tid,'bind_native_dispatch',run_id=dispatch['run_id'],thread_id='dev-stop',dispatch_attempt_id=dispatch['dispatch_attempt_id'])
        self.a.request_stop({'task_id':tid,'revision':2})
        with self.assertRaises(ValueError):
            self.b.confirm_owner({'task_id':tid,'revision':2})
        self.b.confirm_stopped({'task_id':tid,'revision':2,'local_session_stopped':True})
        self.b.confirm_owner({'task_id':tid,'revision':2})
        with self.assertRaises(ValueError):
            self.tool(tid,'renew_dispatch_lease',run_id=dispatch['run_id'])
        self.assertEqual('ready',self.b.snapshot()['tasks'][0]['status'])

    def test_reassignment_discards_old_contract_and_lease(self):
        rid,tid=self.analyzed_owner()
        self.b.ask({'task_id':tid,'revision':2,'kind':'awaiting_reassignment','question':'请求改派'})
        candidate=self.a.candidates({'task_id':tid,'revision':2})
        self.a.assign({**candidate,'owner_account_id':self.ids['bob']})
        task=self.b.snapshot()['tasks'][0]
        self.assertEqual('draft',task['status'])
        self.assertEqual('{}',task['implementation_contract'])
        self.assertIsNone(task['confirmed_revision'])
        self.assertEqual(3,task['revision'])
        with self.assertRaises(Exception):
            self.b.execution_claim({'task_id':tid,'revision':2})
