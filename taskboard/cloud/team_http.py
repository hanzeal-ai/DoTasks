"""Explicit team routes; no delegation to the unrestricted personal tool router."""
import re
import threading
import hashlib
import json

from taskboard.http_security import HTTPRequestError
from .team_directory import TeamDirectory, denied, required_text, dump
from .team_service import TeamService


BROWSER_ACTIONS = {
    'create-project':'create_project', 'grant-project':'grant_project', 'preferences':'preferences',
    'revoke-project':'revoke_project',
    'upload-requirement':'upload_requirement', 'submit-requirement':'submit_requirement',
    'recommend':'recommend', 'assign':'assign', 'ask':'ask', 'answer':'answer',
    'start-analysis':'start_analysis', 'confirm-owner':'confirm_owner',
    'resolve-analysis':'resolve_analysis',
    'read-attachment':'read_attachment',
    'confirm-delivery':'confirm_delivery', 'confirm-integration':'confirm_integration',
    'accept-requirement':'accept_requirement', 'mark-read':'mark_read',
    'request-stop':'request_stop', 'confirm-stopped':'confirm_stopped', 'publish-revision':'publish_revision',
}
AGENT_ACTIONS = {'analysis-claim':'claim_analysis', 'analysis-result':'analysis_result',
                 'execution-claim':'execution_claim', 'execution-tool':'execution_tool'}


class TeamRegistry:
    def __init__(self, server):
        self.server = server
        self.directory = TeamDirectory(server.accounts)
        self.lock = threading.Lock()
        self.runtimes = {}

    def runtime(self, team, actor):
        self.directory.role(team,actor)
        with self.lock:
            if team not in self.runtimes:
                service = TeamService(self.server.home/'teams'/team, self.server.base_config.server.public_url,team,self.directory)
                service.server = self.server
                self.runtimes[team] = (threading.RLock(), service)
            lock, service = self.runtimes[team]
        return lock,service.for_actor(actor)


def handle_team_request(handler, method, path):
    browser = path == '/api/teams' or path.startswith('/api/teams/')
    agent = path == '/_agent/v1/teams' or path.startswith('/_agent/v1/teams/')
    if not browser and not agent:
        return False
    if (browser and handler.identity_kind != 'browser') or (agent and handler.identity_kind != 'agent'):
        denied()
    if browser and method != 'GET' and handler.headers.get('Origin') not in handler._trusted_origins():
        raise HTTPRequestError(403,'Untrusted request origin')
    registry, actor = handler.server.teams, handler.account['id']
    base = '/api/teams' if browser else '/_agent/v1/teams'
    if path == base:
        if method == 'GET':
            result = {'teams':registry.directory.list(actor)}
        elif browser and method == 'POST':
            payload=handler._read_json()
            result = registry.directory.create(actor,payload.get('name'),payload.get('request_id'))
        else:
            raise HTTPRequestError(405,'Method not allowed')
    else:
        match = re.fullmatch(re.escape(base)+r'/([0-9a-f]{32})(?:/([a-z-]+))?',path)
        if not match:
            raise HTTPRequestError(404,'Route not found')
        team, action = match.groups()
        lock, service = registry.runtime(team,actor)
        with lock:
            if method == 'GET' and action is None:
                result = service.snapshot()
            elif method == 'POST':
                payload = handler._read_json()
                if action == 'members' and browser:
                    result = registry.directory.add(team,actor,payload.get('username'),payload.get('role'))
                elif action=='remove-member' and browser:
                    if service.role()!='admin':
                        denied()
                    with service.db.connection() as db:
                        if db.execute('SELECT 1 FROM team_project_members WHERE account_id=?',(payload.get('account_id'),)).fetchone():
                            raise ValueError('请先撤销该成员的全部项目授权')
                    result=registry.directory.remove(team,actor,payload.get('account_id'))
                else:
                    target = (BROWSER_ACTIONS if browser else AGENT_ACTIONS).get(action)
                    if not target:
                        raise HTTPRequestError(404,'Route not found')
                    with service.db.atomic() as db:
                        request_id = required_text(payload.pop('request_id', None),'request_id',128) if browser else None
                        digest = hashlib.sha256(dump({'action':action,'payload':payload}).encode()).hexdigest()
                        cached = db.execute('SELECT * FROM team_requests WHERE actor_id=? AND request_id=?',(actor,request_id)).fetchone() if request_id else None
                        if cached:
                            if cached['digest']!=digest:
                                raise HTTPRequestError(409,'同一请求标识不能用于不同操作')
                            result = json.loads(cached['result'])
                        else:
                            result = getattr(service,target)() if action=='analysis-claim' else getattr(service,target)(payload)
                            if request_id:
                                db.execute('INSERT INTO team_requests VALUES(?,?,?,?)',(actor,request_id,digest,dump(result)))
            else:
                raise HTTPRequestError(405,'Method not allowed')
        if method == 'POST' and action not in {'analysis-claim','execution-claim'} and not (
                action=='execution-tool' and payload.get('name') in {'get_dispatch_status','get_native_dispatch','get_task','get_run_context','get_run','renew_dispatch_lease'}):
            for member in registry.directory.members(team,actor):
                handler.server.notify_agent(member['id'],'state_changed')
    handler._json(200,result)
    return True
