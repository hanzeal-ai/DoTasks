"""Read/verify RPCs for the authoritative core against a member's local worktree."""
import json
import subprocess


class TeamWorkspaceMixin:
    def _relay_workspace(self,payload):
        store,_=self.server.runtime(self.actor)
        command_id=store.enqueue(self.actor,'POST','/api/agent/team-workspace',
            {'Content-Type':'application/json'},json.dumps(payload).encode())
        self.server.notify_agent(self.actor,'command_available')
        result=store.wait_result(command_id,70)
        if not result:
            store.cancel_queued(command_id)
            raise ValueError('本地工作区校验未返回，请确认 Agent 在线')
        if result['status']!=200:
            raise ValueError('本地项目身份、产物版本或验证未通过')
        response=json.loads(result['body'])
        if response.get('binding')!={k:payload[k] for k in ('team_id','task_id','revision','project')}:
            raise ValueError('工作区证据的版本或项目不匹配')
        return response['result']

    def _integration_rpc(self,req,project,members,revision,command):
        return self._relay_workspace({'team_id':self.team_id,'task_id':req['id'],'revision':req['version'],
            'project':project,'action':'integration','arguments':{'members':members,'revision':revision,'command':command}})

    def _workspace_rpc(self, action, **arguments):
        task_id = getattr(self,'execution_scope_task_id',None)
        if not task_id:
            raise ValueError('工作区访问必须绑定团队任务')
        with self.db.connection() as db:
            task = self.task(task_id,db)
            req = self.requirement(task['requirement_id'],db)
            project = self.project(req['project_id'],db)
            if task['owner_account_id'] != self.actor:
                raise ValueError('工作区负责人不匹配')
        payload={'team_id':self.team_id,'task_id':task_id,'revision':task['revision'],
                 'project':project,'action':action,'arguments':arguments}
        return self._relay_workspace(payload)

    def _workspace_state(self, project):
        if not getattr(self,'execution_scope_task_id',None):
            return {'available':False,'reason':'workspace_requires_bound_execution','project':project}
        return self._workspace_rpc('state')

    def _workspace_diff(self, project, baseline_revision, paths):
        return self._workspace_rpc('diff',baseline_revision=baseline_revision,paths=paths)

    def _git(self, project, *arguments):
        result=self._workspace_rpc('git',args=list(arguments))
        return subprocess.CompletedProcess(['git',*arguments],result['returncode'],result['stdout'],result['stderr'])

    def _run_project_command(self, command, project, timeout_seconds):
        result=self._workspace_rpc('verify',command=command,timeout_seconds=min(60,timeout_seconds))
        return result['status'],result['exit_code'],result['output'],result['duration_ms']
