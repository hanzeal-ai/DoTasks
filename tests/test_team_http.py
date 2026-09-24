import secrets
import unittest
from tests import test_accounts as accounts_tests


class TeamHTTPTest(unittest.TestCase):
    setUp=accounts_tests.AccountHTTPTest.setUp
    tearDown=accounts_tests.AccountHTTPTest.tearDown
    request=accounts_tests.AccountHTTPTest.request
    register=accounts_tests.AccountHTTPTest.register
    login=accounts_tests.AccountHTTPTest.login

    def prepare(self):
        alice=self.register()
        bob=self.register('bob','b'*32)
        ca,cb=self.login('alice'),self.login('bob')
        headers={'Cookie':ca,'Origin':'https://dotasks.test'}
        status,_,team=self.request('POST','/api/teams',{'name':'团队'},**headers)
        self.assertEqual(200,status,team)
        base='/api/teams/'+team['id']
        status,_,project=self.request('POST',base+'/create-project',{'name':'网站','repository':'https://example.test/repo.git','baseline':'a'*40,'request_id':secrets.token_hex(16)},**headers)
        self.assertEqual(200,status,project)
        return alice,bob,headers,cb,team,project

    def test_team_actor_and_project_authorization_and_direct_tool_bypass(self):
        alice,bob,headers,cb,team,project=self.prepare()
        base='/api/teams/'+team['id']
        self.assertEqual(403,self.request('GET',base,Cookie=cb)[0])
        self.assertEqual(401,self.request('GET',base,Authorization='Bearer '+alice['agent_token'])[0])
        status,_,_=self.request('POST',base+'/members',{'username':'bob','role':'developer'},**headers)
        self.assertEqual(200,status)
        self.assertEqual([],self.request('GET',base,Cookie=cb)[2]['projects'])
        status,_,body=self.request('POST',base+'/upload-requirement',{'project_id':project['id'],'title':'越权','content':'越权','coordinator_id':bob['agent_id'],'request_id':secrets.token_hex(16)},Cookie=cb,Origin='https://dotasks.test')
        self.assertEqual(403,status,body)
        agent='/_agent/v1/teams/'+team['id']
        self.assertEqual(404,self.request('POST',agent+'/assign',{},Authorization='Bearer '+alice['agent_token'])[0])
        self.assertEqual(403,self.request('POST',agent+'/execution-tool',{'task_id':'TASK-0001','revision':1,'name':'finalize_task_intake','arguments':{}},Authorization='Bearer '+alice['agent_token'])[0])
        self.assertEqual(404,self.request('POST',base+'/task-intakes/finalize',{},**headers)[0])

    def test_same_request_is_atomic_and_idempotent_and_csrf_is_rejected(self):
        alice,bob,headers,cb,team,project=self.prepare()
        path='/api/teams/'+team['id']+'/upload-requirement'
        payload={'project_id':project['id'],'title':'需求','content':'目标','coordinator_id':alice['agent_id'],'request_id':'upload-once'}
        self.assertEqual(403,self.request('POST',path,payload,Cookie=headers['Cookie'])[0])
        first=self.request('POST',path,payload,**headers)
        second=self.request('POST',path,payload,**headers)
        self.assertEqual(200,first[0],first)
        self.assertEqual(first[2],second[2])
        snapshot=self.request('GET','/api/teams/'+team['id'],Cookie=headers['Cookie'])[2]
        self.assertEqual(1,len(snapshot['requirements']))
        self.assertEqual(1,len(snapshot['jobs']))
        self.assertEqual(409,self.request('POST',path,{**payload,'title':'different'},**headers)[0])


if __name__=='__main__':
    unittest.main()
