"""Event-driven team work on the member's machine, with durable analysis receipts."""
from __future__ import annotations

import json
import base64
import ipaddress
from pathlib import Path
import threading
import sys
import urllib.request
import urllib.error
from urllib.parse import urlsplit

from .http_client import NoRedirect
from .local_executor import LocalCodexExecutor
from .remote_service import RemoteTaskboardService, RemoteToolClient
from .team_local import ReadOnlyAnalysisClient, save_json, verify_mapping, task_workspace, attachment_inputs


class TeamRequestError(RuntimeError):
    def __init__(self,status):
        self.status=status
        super().__init__('团队请求失败：'+str(status))


class TeamClient(RemoteToolClient):
    def __init__(self,*args,team_id='',task_id='',revision=0,**kwargs):
        super().__init__(*args,**kwargs)
        self.team_id,self.task_id,self.revision=team_id,task_id,revision

    def request(self,path,payload=None):
        parsed=urlsplit(self.cloud_url)
        try:
            loopback=ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback=parsed.hostname=='localhost'
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path or (parsed.scheme=='http' and not loopback):
            raise ValueError('团队服务需要 HTTPS；HTTP 仅允许本机回环地址')
        request=urllib.request.Request(self.cloud_url+path,
            data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+self.agent_token,'Content-Type':'application/json'},
            method='GET' if payload is None else 'POST')
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=600) as response:
                body=response.read(8*1024*1024+1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise TeamRequestError(exc.code) from exc
        if len(body)>8*1024*1024:
            raise ValueError('Team response too large')
        return json.loads(body)

    def action(self,name,payload):
        return self.request('/_agent/v1/teams/'+self.team_id+'/'+name,payload)

    def call(self,name,arguments):
        return self.action('execution-tool',{'task_id':self.task_id,'revision':self.revision,'name':name,'arguments':arguments})


class TeamExecutor(LocalCodexExecutor):
    def __init__(self,*args,on_finished,**kwargs):
        super().__init__(*args,**kwargs)
        self.on_finished=on_finished

    def wake(self):
        self.on_finished()


class TeamAgent:
    def __init__(self,config):
        self.config=config
        self.home=Path(config.data_home)
        self.client=TeamClient(config.cloud_url,config.agent_id,config.agent_token)
        self.event=threading.Event()
        self.stopped=threading.Event()
        self.thread=None
        self.executors={}
        self.analysis_client=None
        self.analysis_thread=None
        self.usage_outboxes={}

    def start(self):
        self.thread=threading.Thread(target=self.loop,name='dotasks-team-agent',daemon=True)
        self.thread.start()
        self.wake()

    def wake(self):
        self.event.set()

    def stop(self):
        self.stopped.set()
        self.event.set()
        if self.analysis_client:
            self.analysis_client.stop()
        for executor in list(self.executors.values()):
            executor.stop()

    def loop(self):
        while not self.stopped.is_set():
            self.event.wait(30)
            self.event.clear()
            if self.stopped.is_set():
                return
            try:
                self.drain()
            except Exception as exc:
                print('[dotasks-team] '+str(exc),file=sys.stderr)

    def drain(self):
        teams=self.client.request('/_agent/v1/teams')['teams']
        active_teams={t['id'] for t in teams}
        for key,executor in list(self.executors.items()):
            if key[0] not in active_teams:
                executor.stop()
        for team in teams:
            if self.stopped.is_set():
                return
            client=TeamClient(self.config.cloud_url,self.config.agent_id,self.config.agent_token,team_id=team['id'])
            from .execution_usage import UsageOutbox
            for path in (self.home/'team-workers'/team['id']).glob('*/cloud-agent.json'):
                stored = json.loads(path.read_text())
                context = stored.get('team_context') or {}
                if stored.get('agent_id') != self.config.agent_id or stored.get('cloud_url') != self.config.cloud_url or context.get('team_id') != team['id']:
                    continue
                key = str(path.parent)
                if key not in self.usage_outboxes:
                    scoped = TeamClient(self.config.cloud_url, self.config.agent_id, self.config.agent_token, **context)
                    self.usage_outboxes[key] = UsageOutbox(path.parent, RemoteTaskboardService(scoped))
                self.usage_outboxes[key].flush_async()

            # Resend a persisted completed response before claiming any new work.
            receipts=self.home/'team-analysis'/team['id']
            if receipts.exists():
                for path in receipts.glob('*/*/receipt.json'):
                    record=json.loads(path.read_text())
                    if record.get('state')=='result':
                        try:
                            client.action('analysis-result',record['response'])
                            save_json(path,{'state':'acknowledged'})
                        except TeamRequestError as exc:
                            if exc.status not in {400,403,404,409}:
                                raise
                            save_json(path,{**record,'state':'rejected','status':exc.status})
            if not self.analysis_thread or not self.analysis_thread.is_alive():
                job=client.action('analysis-claim',{})
                if job and not self.stopped.is_set():
                    self.analysis_thread=threading.Thread(target=self._run_analysis,args=(client,job),
                        name='dotasks-team-analysis',daemon=True)
                    self.analysis_thread.start()
            board=client.request('/_agent/v1/teams/'+team['id'])
            permitted={(team['id'],t['id'],t['revision']) for t in board['tasks'] if t['owner_account_id']==board['actor_id'] and t['confirmed_revision']==t['revision'] and not t['handling']}
            for key,executor in list(self.executors.items()):
                if key[0]==team['id'] and key not in permitted:
                    executor.stop()
            for task in board['tasks']:
                if task['handling']=='awaiting_stop':
                    for key, executor in list(self.executors.items()):
                        if key[:2]==(team['id'],task['id']):
                            executor.stop()
                    continue
                if task['owner_account_id']!=board['actor_id'] or task['confirmed_revision']!=task['revision'] or task['handling']:
                    continue
                if task['status'] not in {'ready','rework','code_review','claimed','implementing'}:
                    continue
                key=(team['id'],task['id'],task['revision'])
                existing=self.executors.get(key)
                if existing and existing.active_runs:
                    continue
                project=next(p for p in board['projects'] if p['id']==next(r['project_id'] for r in board['requirements'] if r['id']==task['requirement_id']))
                workspace=task_workspace(self.home,{'team_id':team['id'],'task_id':task['id'],'revision':task['revision'],'project':project})
                dispatch=client.action('execution-claim',{'task_id':task['id'],'revision':task['revision']})
                if not dispatch:
                    continue
                scoped=TeamClient(self.config.cloud_url,self.config.agent_id,self.config.agent_token,
                                  team_id=team['id'],task_id=task['id'],revision=task['revision'])
                worker_home=self.home/'team-workers'/team['id']/(task['id']+'-v'+str(task['revision']))
                save_json(worker_home/'cloud-agent.json',{'cloud_url':self.config.cloud_url,'agent_id':self.config.agent_id,
                    'agent_token':self.config.agent_token,'team_context':{'team_id':team['id'],'task_id':task['id'],'revision':task['revision']}})
                executor=TeamExecutor(worker_home,service=RemoteTaskboardService(scoped),on_finished=self.wake)
                outbox_key = str(worker_home)
                if outbox_key in self.usage_outboxes:
                    executor._usage_outbox = self.usage_outboxes[outbox_key]
                else:
                    self.usage_outboxes[outbox_key] = executor._usage_outbox
                self.executors[key]=executor
                dispatch['project_path']=workspace
                dispatch['dispatch_prompt']=dispatch['dispatch_prompt'].replace(task['project'],workspace)
                dispatch['dispatch_prompt']+='\n这是用户已确认的团队任务。仅在当前隔离工作区开发、验证并提交本模块变更；不推送、不发布。代码交付必须为干净的 Git revision。生命周期回调不传 workspace_path，服务端已绑定工作区。'
                dispatch=executor._localize_dispatch_prompt(dispatch)
                for index,item in enumerate(attachment_inputs(dispatch.get('attachments',[]))):
                    if item['type']=='text':
                        dispatch['dispatch_prompt']+='\n'+item['text']
                    else:
                        path=worker_home/('attachment-'+str(index)+'.'+('png' if item['url'].startswith('data:image/png') else 'jpg'))
                        path.write_bytes(base64.b64decode(item['url'].split(',',1)[1]))
                        path.chmod(0o600)
                        dispatch.setdefault('input_image_paths',[]).append(str(path))
                executor._launch(dispatch)

    def _run_analysis(self,client,job):
        try:
            if not self.stopped.is_set():
                self.analyze(client,job)
        except Exception as exc:
            print('[dotasks-team] '+str(exc),file=sys.stderr)
        finally:
            self.wake()

    def analyze(self,client,job):
        home=self.home/'team-analysis'/job['team_id']/job['id']/job['lease']
        receipt=home/'receipt.json'
        if receipt.exists() and json.loads(receipt.read_text()).get('state') not in {'acknowledged'}:
            # Creation may have succeeded before a crash. Never duplicate it.
            raise RuntimeError('存在未确认分析会话，请核对本地收据：'+str(receipt))
        analyzer=ReadOnlyAnalysisClient(home,Path(__file__).resolve().parents[1])
        self.analysis_client=analyzer
        try:
            root=verify_mapping(self.home,job['team_id'],job['project'])
            thread,result=analyzer.analyze(root,job,receipt,cancelled=self.stopped.is_set)
            response={'job_id':job['id'],'lease':job['lease'],'thread_id':thread,'result':result}
            save_json(receipt,{'state':'result','response':response})
            client.action('analysis-result',response)
            save_json(receipt,{'state':'acknowledged'})
        except Exception:
            # If the request result is persisted, transport retry is safe. For an
            # uncertain thread/turn retain the receipt and let the lease expire.
            if not receipt.exists():
                client.action('analysis-result',{'job_id':job['id'],'lease':job['lease'],'error':'本地分析准备失败，请检查项目映射、Codex 和只读沙箱'})
            raise
        finally:
            analyzer.stop()
            self.analysis_client=None
