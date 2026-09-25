"""Team approval aggregate backed by the existing core requirements and tasks.

All public operations run under the runtime's lock. The HTTP adapter resolves the
actor from authentication, never from the request body. Core execution is exposed
only by the version-bound execution adapter, not by the generic personal API.
"""
from __future__ import annotations

from core.service.domain import insert_relation

import copy
import hashlib
import json
import re
import secrets
import time
import uuid

from core.team_schema import SCHEMA
from .server import CloudTaskboardService
from .team_directory import denied, required_text, dump, version
from .team_execution import TeamExecutionMixin
from .team_workspace import TeamWorkspaceMixin
from .team_changes import TeamChangesMixin
from .task_source import TaskSourceMixin
from .team_attachments import TeamAttachmentsMixin, decode_attachments, ATTACHMENT_POLICY


def strings(value, name, *, nonempty=False):
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in value):
        raise ValueError(f'{name} 必须为文本数组')
    if nonempty and not value:
        raise ValueError(f'{name} 不能为空')
    return list(dict.fromkeys(x.strip() for x in value))


def modules(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 30:
        raise ValueError('需要 1–30 个模块')
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('模块必须为对象')
        spec = {k: required_text(item.get(k), k, 2000) for k in ('key', 'title', 'goal')}
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', spec['key']):
            raise ValueError('模块键必须为字母、数字、短横线或下划线')
        for field in ('scope', 'out_of_scope', 'acceptance_criteria', 'interfaces', 'depends_on'):
            spec[field] = strings(item.get(field), field, nonempty=field in {'scope', 'acceptance_criteria'})
        result.append(spec)
    keys = {s['key'] for s in result}
    if len(keys) != len(result):
        raise ValueError('模块键不能重复')
    graph = {s['key']: s['depends_on'] for s in result}
    visiting, visited = set(), set()
    def visit(key):
        if key not in keys or key in visiting:
            raise ValueError('模块依赖不存在或形成循环')
        if key in visited:
            return
        visiting.add(key)
        for child in graph[key]:
            visit(child)
        visiting.remove(key)
        visited.add(key)
    for key in keys:
        visit(key)
    return result


class TeamService(TaskSourceMixin, TeamAttachmentsMixin, TeamChangesMixin, TeamExecutionMixin, TeamWorkspaceMixin, CloudTaskboardService):
    def __init__(self, home, public_url, team_id, directory):
        super().__init__(home, public_url)
        self.team_id, self.directory, self.actor = team_id, directory, ''
        with self.db.connection() as db:
            db.executescript(SCHEMA)
            db.commit()

    def for_actor(self, actor):
        self.directory.role(self.team_id, actor)
        service = copy.copy(self)
        service.actor = actor
        return service

    def role(self):
        return self.directory.role(self.team_id, self.actor)

    def project(self, project_id, db=None):
        self.role()
        if db is None:
            with self.db.connection() as connection:
                return self.project(project_id, connection)
        row = db.execute('SELECT * FROM team_projects WHERE id=?', (project_id,)).fetchone()
        grant = db.execute('SELECT * FROM team_project_members WHERE project_id=? AND account_id=?', (project_id, self.actor)).fetchone()
        if not row or not grant:
            denied()
        return dict(row)

    def requirement(self, requirement_id, db):
        row = db.execute('''SELECT r.*, m.project_id, m.product_id, m.coordinator_id,
            m.version, m.submitted_version, m.accepted_version, m.integration
            FROM requirements r JOIN team_requirements m ON m.requirement_id=r.id WHERE r.id=?''', (requirement_id,)).fetchone()
        if not row:
            denied()
        self.project(row['project_id'], db)
        return dict(row)

    def task(self, task_id, db):
        row = db.execute('''SELECT t.*, m.revision, m.requirement_version, m.owner_account_id,
            m.confirmed_revision, m.delivered_revision, m.handling, m.interfaces, m.analysis
            FROM tasks t JOIN team_tasks m ON m.task_id=t.id WHERE t.id=?''', (task_id,)).fetchone()
        if not row:
            denied()
        self.requirement(row['requirement_id'], db)
        return dict(row)

    def notify(self, db, actor, requirement_id, message):
        db.execute('INSERT INTO team_notifications(id,account_id,requirement_id,message) VALUES(?,?,?,?)',
                   (uuid.uuid4().hex, actor, requirement_id, message))

    def queue_analysis(self, db, req, task=None, *, manual=False):
        actor = task['owner_account_id'] if task else req['product_id']
        settings = db.execute('SELECT * FROM team_project_members WHERE project_id=? AND account_id=?', (req['project_id'], actor)).fetchone()
        if not settings:
            denied()
        job_id = uuid.uuid4().hex
        db.execute('''INSERT OR IGNORE INTO team_jobs(id,requirement_id,task_id,actor_id,kind,version,status,token_budget)
            VALUES(?,?,?,?,?,?,?,?)''', (job_id, req['id'], task['id'] if task else None, actor,
            'ownership_analysis' if task else 'requirement_analysis', task['revision'] if task else req['version'],
            'queued' if manual or settings['auto_analysis'] else 'awaiting_permission', settings['token_budget']))
        self.notify(db, actor, req['id'], '任务已分配，请分析并确认需求' if task else '需求已保存，等待本地分析')

    def snapshot(self):
        with self.db.transaction() as db:
            db.execute("UPDATE team_jobs SET status='uncertain',error='运行租约过期，需核对本地会话' WHERE status='running' AND expires_at<?", (time.time(),))
        with self.db.connection() as db:
            projects = [dict(r) for r in db.execute('''SELECT p.*, g.modules, g.capacity, g.auto_analysis, g.token_budget
                FROM team_projects p JOIN team_project_members g ON g.project_id=p.id WHERE g.account_id=?''', (self.actor,))]
            allowed = {p['id'] for p in projects}
            sources = self.task_sources(db, allowed)
            reqs = [self.requirement(r['requirement_id'], db) for r in db.execute('SELECT * FROM team_requirements') if r['project_id'] in allowed]
            req_ids = {r['id'] for r in reqs}
            for req in reqs:
                req['attachments']=self.attachments(req['id'],db)
            tasks = [self.task(t['id'], db) for t in db.execute('SELECT id,requirement_id FROM tasks') if t['requirement_id'] in req_ids]
            for task in tasks:
                if task['handling']:
                    phase = task['handling']
                elif not task['owner_account_id']:
                    phase = 'awaiting_assignment'
                elif task['confirmed_revision'] != task['revision']:
                    phase = 'awaiting_owner_confirmation' if json.loads(task['analysis']) else 'assigned'
                elif task['status'] == 'done' and task['delivered_revision'] != task['revision']:
                    phase = 'awaiting_delivery_confirmation'
                else:
                    phase = task['status']
                task['phase'] = phase
                delivery=db.execute('SELECT id,output_revision,artifact_sha256,verification_result,delivery_summary FROM task_runs WHERE id=?',(task['primary_run_id'],)).fetchone()
                task['delivery']=dict(delivery) if delivery else None
            questions = [dict(r) for r in db.execute('SELECT * FROM team_questions ORDER BY created_at') if r['requirement_id'] in req_ids]
            jobs = [{k: r[k] for k in ('id','requirement_id','task_id','actor_id','kind','version','status','thread_id','result','error')}
                    for r in db.execute('SELECT * FROM team_jobs ORDER BY created_at') if r['requirement_id'] in req_ids]
            notifications = [dict(r) for r in db.execute('SELECT * FROM team_notifications WHERE account_id=? ORDER BY created_at DESC', (self.actor,)) if r['requirement_id'] in req_ids]
        return {'task_sources': sources, 'attachment_policy': ATTACHMENT_POLICY, 'team_id': self.team_id, 'actor_id': self.actor, 'role': self.role(), 'projects': projects,
                'members': self.directory.members(self.team_id, self.actor), 'requirements': reqs,
                'tasks': tasks, 'questions': questions, 'jobs': jobs, 'notifications': notifications}

    def create_project(self, p):
        if self.role() != 'admin':
            denied()
        name = required_text(p.get('name'), '项目名称', 120)
        repository = required_text(p.get('repository'), '仓库来源', 1000)
        baseline = required_text(p.get('baseline'), '基线', 64)
        if not re.fullmatch(r'[0-9a-f]{40,64}', baseline):
            raise ValueError('基线必须为完整 Git revision')
        pid = uuid.uuid4().hex
        with self.db.transaction() as db:
            db.execute('INSERT INTO team_projects(id,name,repository,baseline) VALUES(?,?,?,?)', (pid,name,repository,baseline))
            db.execute('INSERT INTO team_project_members(project_id,account_id) VALUES(?,?)', (pid,self.actor))
        return {'id': pid}

    def grant_project(self, p):
        if self.role() != 'admin':
            denied()
        self.project(p.get('project_id'))
        self.directory.role(self.team_id, p.get('account_id'))
        skills = strings(p.get('modules', []), '模块能力')
        capacity = p.get('capacity', 1)
        if type(capacity) is not int or not 1 <= capacity <= 10:
            raise ValueError('承接容量必须为 1–10')
        with self.db.transaction() as db:
            db.execute('''INSERT INTO team_project_members(project_id,account_id,modules,capacity) VALUES(?,?,?,?)
                ON CONFLICT(project_id,account_id) DO UPDATE SET modules=excluded.modules, capacity=excluded.capacity''',
                (p['project_id'],p['account_id'],dump(skills),capacity))
        return {'ok': True}

    def revoke_project(self,p):
        if self.role()!='admin' or p.get('account_id')==self.actor:
            denied()
        project=self.project(p.get('project_id'))
        with self.db.transaction() as db:
            account=p.get('account_id')
            if db.execute("SELECT 1 FROM team_requirements m JOIN requirements r ON r.id=m.requirement_id WHERE m.project_id=? AND (m.product_id=? OR m.coordinator_id=?) AND r.status!='done'",(project['id'],account,account)).fetchone():
                raise ValueError('该成员仍负责产品确认或集成，请先完成需求')
            tasks=[self.task(row['id'],db) for row in db.execute('''SELECT t.id FROM tasks t JOIN team_tasks m ON m.task_id=t.id
                JOIN team_requirements r ON r.requirement_id=t.requirement_id WHERE r.project_id=? AND m.owner_account_id=?''',(project['id'],account))]
            if any(t['active_run_id'] or t['handling']=='awaiting_stop' for t in tasks):
                raise ValueError('请先请求停止并由负责人确认本地运行已结束')
            db.execute('DELETE FROM team_project_members WHERE project_id=? AND account_id=?',(project['id'],account))
            for task in tasks:
                if task['delivered_revision']==task['revision'] or task['handling']=='retired':
                    continue
                db.execute("UPDATE team_tasks SET confirmed_revision=NULL,handling='awaiting_reassignment' WHERE task_id=?",(task['id'],))
                db.execute('UPDATE tasks SET auto_dispatch=0 WHERE id=?',(task['id'],))
                db.execute("UPDATE team_jobs SET status='superseded' WHERE task_id=? AND status!='completed'",(task['id'],))
        return {'ok':True}

    def preferences(self, p):
        self.project(p.get('project_id'))
        if type(p.get('auto_analysis')) is not bool or type(p.get('token_budget')) is not int or not 1000 <= p['token_budget'] <= 60000:
            raise ValueError('需明确自动分析开关和 1000–60000 的分析预算')
        with self.db.transaction() as db:
            db.execute('UPDATE team_project_members SET auto_analysis=?,token_budget=? WHERE project_id=? AND account_id=?',
                       (int(p['auto_analysis']),p['token_budget'],p['project_id'],self.actor))
        return {'ok': True}

    def upload_requirement(self, p):
        if self.role() not in {'admin','product'}:
            denied()
        project = self.project(p.get('project_id'))
        attachments=decode_attachments(p.get('attachments',[]))
        title, content = required_text(p.get('title'), '标题', 120), required_text(p.get('content'), '需求原文', 30000)
        coordinator = p.get('coordinator_id')
        if self.directory.role(self.team_id, coordinator) not in {'admin','developer','coordinator'}:
            raise ValueError('请选择开发协调人')
        with self.db.transaction() as db:
            if not db.execute('SELECT 1 FROM team_project_members WHERE project_id=? AND account_id=?', (project['id'],coordinator)).fetchone():
                denied()
            rid = self.db.next_id(db, 'REQ')
            db.execute('''INSERT INTO requirements(id,title,original_content,goal,project,status,source_type,source_reference,auto_dispatch)
                VALUES(?,?,?,?,?,'draft','team',?,0)''', (rid,title,content,content,'/teams/'+self.team_id+'/'+project['id'],None))
            db.execute('INSERT INTO team_requirements(requirement_id,project_id,product_id,coordinator_id) VALUES(?,?,?,?)', (rid,project['id'],self.actor,coordinator))
            for attachment in attachments:
                db.execute('INSERT INTO team_attachments VALUES(?,?,?,?,?,?)',(attachment['id'],rid,attachment['name'],attachment['mime'],attachment['sha256'],attachment['content']))
            req = self.requirement(rid, db)
            self.queue_analysis(db, req)
            self._event(db,'requirement',rid,'team_uploaded',{'actor':self.actor,'version':1})
        return {'requirement_id': rid}

    def submit_requirement(self, p):
        specs = modules(p.get('modules'))
        with self.db.transaction() as db:
            req = self.requirement(p.get('requirement_id'), db)
            if req['product_id'] != self.actor:
                denied()
            version(req['version'], p.get('version'))
            if req['submitted_version'] == req['version']:
                raise ValueError('该版本已提交；请发布补充版本')
            if not db.execute("SELECT 1 FROM team_jobs WHERE requirement_id=? AND task_id IS NULL AND version=? AND status='completed'", (req['id'],req['version'])).fetchone():
                raise ValueError('请先完成产品需求分析')
            if db.execute("SELECT 1 FROM team_questions WHERE requirement_id=? AND answer=''", (req['id'],)).fetchone():
                raise ValueError('请先回答待决策问题')
            ids = {}
            for spec in specs:
                tid = self.db.next_id(db, 'TASK')
                db.execute('''INSERT INTO tasks(id,requirement_id,requirement_task_key,title,goal,project,modules,scope,out_of_scope,acceptance_criteria,status,auto_dispatch)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'draft',0)''',
                    (tid,req['id'],spec['key'],spec['title'],spec['goal'],req['project'],dump([spec['key']]),dump(spec['scope']),dump(spec['out_of_scope']),dump(spec['acceptance_criteria'])))
                db.execute('INSERT INTO team_tasks(task_id,requirement_version,interfaces) VALUES(?,?,?)', (tid,req['version'],dump(spec['interfaces'])))
                ids[spec['key']] = tid
            for spec in specs:
                for dependency in spec['depends_on']:
                    insert_relation(db, ids[spec['key']], ids[dependency], 'depends_on', '模块交付依赖')
            snapshot = {'content': req['original_content'], 'modules':specs,'coordinator_id':req['coordinator_id']}
            db.execute('INSERT INTO team_versions(requirement_id,version,snapshot,actor_id) VALUES(?,?,?,?)', (req['id'],req['version'],dump(snapshot),self.actor))
            db.execute('UPDATE requirements SET status=\'submitted\',decomposition_plan=? WHERE id=?', (dump(specs),req['id']))
            db.execute('UPDATE team_requirements SET submitted_version=version WHERE requirement_id=?', (req['id'],))
            self._event(db,'requirement',req['id'],'team_submitted',{'actor':self.actor,'version':req['version']})
        return {'task_ids': list(ids.values())}

    def candidates(self, p):
        with self.db.connection() as db:
            task = self.task(p.get('task_id'), db)
            req = self.requirement(task['requirement_id'], db)
            version(task['revision'], p.get('revision'))
            if task['handling']=='retired':
                raise ValueError('模块已从需求中移除')
            if self.actor not in {req['product_id'],req['coordinator_id']}:
                denied()
            members = {m['id']:m for m in self.directory.members(self.team_id,self.actor)}
            result = []
            for g in db.execute('SELECT * FROM team_project_members WHERE project_id=?', (req['project_id'],)):
                member = members.get(g['account_id'])
                skills = json.loads(g['modules'])
                if not member or member['role'] not in {'admin','developer','coordinator'} or task['requirement_task_key'] not in skills:
                    continue
                active = db.execute('''SELECT COUNT(*) FROM team_tasks m JOIN tasks t ON t.id=m.task_id
                    WHERE m.owner_account_id=? AND t.id!=? AND m.handling!='retired' AND (m.delivered_revision IS NULL OR m.delivered_revision!=m.revision)''', (g['account_id'],task['id'])).fetchone()[0]
                if active < g['capacity']:
                    result.append({'id':g['account_id'],'username':member['username'],'modules':skills,'active':active,'capacity':g['capacity']})
            generation = hashlib.sha256(dump({'revision':task['revision'],'candidates':result}).encode()).hexdigest()
        return {'task_id':task['id'],'revision':task['revision'],'candidate_version':generation,'candidates':result}

    def recommend(self, p):
        result = self.candidates(p)
        with self.db.connection() as db:
            task = self.task(p['task_id'],db)
        result['advice'] = self.decisions.recommend_assignment(task['goal'], result['revision'], result['candidate_version'], result['candidates'])
        return result

    def assign(self, p):
        eligible = self.candidates(p)
        if eligible['candidate_version'] != p.get('candidate_version'):
            raise ValueError('候选资格或容量发生变化，请重新推荐')
        owner = p.get('owner_account_id')
        if owner not in {x['id'] for x in eligible['candidates']}:
            raise ValueError('负责人不符合项目权限、模块能力或容量条件')
        with self.db.transaction() as db:
            task = self.task(p['task_id'], db)
            if task['active_run_id'] or task['handling']=='awaiting_stop':
                raise ValueError('请先停止并确认旧运行已结束')
            if task['owner_account_id'] and task['handling'] != 'awaiting_reassignment':
                raise ValueError('任务已派发，请先请求改派')
            if task['owner_account_id']:
                self.reset_execution_revision(db,task['id'])
            db.execute('''UPDATE team_tasks SET owner_account_id=?,confirmed_revision=NULL,
                delivered_revision=NULL,handling='',analysis='{}',revision=revision+1 WHERE task_id=?''', (owner,task['id']))
            req = self.requirement(task['requirement_id'],db)
            db.execute("UPDATE team_requirements SET integration='{}' WHERE requirement_id=?", (req['id'],))
            self.queue_analysis(db,req,self.task(task['id'],db))
            self._event(db,'task',task['id'],'team_assigned',{'actor':self.actor,'owner':owner})
        return {'ok': True}

    def ask(self, p):
        kind = p.get('kind')
        if kind not in {'awaiting_clarification','awaiting_reassignment','blocked'}:
            raise ValueError('问题类型无效')
        question = required_text(p.get('question'), '问题', 4000)
        with self.db.transaction() as db:
            task = self.task(p.get('task_id'), db)
            version(task['revision'],p.get('revision'))
            if task['owner_account_id'] != self.actor:
                denied()
            if task['handling']=='retired' or task['delivered_revision']==task['revision']:
                raise ValueError('已移除或已交付模块需要由产品发布补充版本')
            if task['active_run_id'] or task['handling']=='awaiting_stop':
                raise ValueError('请先暂停当前执行并确认停止')
            req = self.requirement(task['requirement_id'],db)
            qid = uuid.uuid4().hex
            db.execute('INSERT INTO team_questions(id,requirement_id,task_id,version,author_id,question,kind) VALUES(?,?,?,?,?,?,?)',
                       (qid,req['id'],task['id'],req['version'],self.actor,question,kind))
            db.execute('UPDATE team_tasks SET handling=?,confirmed_revision=NULL WHERE task_id=?', (kind,task['id']))
            db.execute('UPDATE tasks SET auto_dispatch=0 WHERE id=?', (task['id'],))
            self.notify(db,req['product_id'] if kind=='awaiting_clarification' else req['coordinator_id'],req['id'],question)
        return {'question_id':qid}

    def answer(self, p):
        answer = required_text(p.get('answer'), '回答', 8000)
        with self.db.transaction() as db:
            q = db.execute('SELECT * FROM team_questions WHERE id=?', (p.get('question_id'),)).fetchone()
            if not q:
                denied()
            req = self.requirement(q['requirement_id'],db)
            task = self.task(q['task_id'],db) if q['task_id'] else None
            responsible = req['coordinator_id'] if q['kind'] in {'blocked','awaiting_reassignment'} else req['product_id']
            if self.actor != responsible:
                denied()
            version(req['version'],p.get('version'))
            if q['answer']:
                raise ValueError('回答已经保存')
            if p.get('scope_changed') is not False:
                raise ValueError('范围改变必须发布补充版本；普通回答需明确 scope_changed=false')
            db.execute('UPDATE team_questions SET answer=?,answered_by=? WHERE id=?', (answer,self.actor,q['id']))
            if task and task['handling'] in {'awaiting_clarification','blocked'} and not db.execute(
                    "SELECT 1 FROM team_questions WHERE task_id=? AND answer=''",(task['id'],)).fetchone():
                db.execute("UPDATE team_tasks SET handling='' WHERE task_id=?", (task['id'],))
            self.notify(db,q['author_id'],req['id'],'需求问题已有答复，请重新确认')
        return {'ok': True}

    def start_analysis(self,p):
        with self.db.transaction() as db:
            job = db.execute('SELECT * FROM team_jobs WHERE id=?', (p.get('job_id'),)).fetchone()
            if not job or job['actor_id'] != self.actor:
                denied()
            self.requirement(job['requirement_id'],db)
            if job['status'] not in {'awaiting_permission','failed'}:
                raise ValueError('该分析不可重新启动；不确定运行需先核对会话')
            db.execute("UPDATE team_jobs SET status='queued',error='' WHERE id=?", (job['id'],))
        return {'ok': True}

    def resolve_analysis(self,p):
        reason=required_text(p.get('reason'),'会话核对记录',2000)
        with self.db.transaction() as db:
            job=db.execute('SELECT * FROM team_jobs WHERE id=?',(p.get('job_id'),)).fetchone()
            if not job or job['actor_id']!=self.actor:
                denied()
            req=self.requirement(job['requirement_id'],db)
            task=self.task(job['task_id'],db) if job['task_id'] else None
            version(task['revision'] if task else req['version'],job['version'])
            if job['status'] not in {'running','uncertain'} or p.get('local_session_stopped') is not True:
                raise ValueError('请核对并停止原本地会话后再恢复')
            db.execute("UPDATE team_jobs SET status='failed',lease='',error=? WHERE id=?",(reason,job['id']))
            self._event(db,'requirement',req['id'],'analysis_recovery_confirmed',{'job_id':job['id'],'actor':self.actor,'reason':reason})
        return {'ok':True}

    def claim_analysis(self):
        with self.db.transaction() as db:
            # An expired running job is uncertain, never automatically duplicated.
            db.execute("UPDATE team_jobs SET status='uncertain',error='运行租约过期，需核对本地会话' WHERE status='running' AND expires_at<?", (time.time(),))
            if db.execute("SELECT 1 FROM team_jobs WHERE actor_id=? AND status IN ('running','uncertain')", (self.actor,)).fetchone():
                return None
            for job in db.execute("SELECT * FROM team_jobs WHERE actor_id=? AND status='queued' ORDER BY created_at", (self.actor,)).fetchall():
                try:
                    req = self.requirement(job['requirement_id'],db)
                except Exception:
                    continue
                task = self.task(job['task_id'],db) if job['task_id'] else None
                current = task['revision'] if task else req['version']
                if job['version'] != current or task and task['owner_account_id'] != self.actor:
                    db.execute("UPDATE team_jobs SET status='superseded' WHERE id=?", (job['id'],))
                    continue
                lease = secrets.token_urlsafe(32)
                db.execute("UPDATE team_jobs SET status='running',lease=?,expires_at=? WHERE id=?", (lease,time.time()+1800,job['id']))
                project = self.project(req['project_id'],db)
                return {**dict(job),'status':'running','lease':lease,'team_id':self.team_id,
                        'requirement':req,'task':task,'project':project,
                        'attachments':self.attachments(req['id'],db,content=True),
                        'questions':[dict(q) for q in db.execute('SELECT * FROM team_questions WHERE requirement_id=?',(req['id'],))]}
        return None

    def analysis_result(self,p):
        with self.db.transaction() as db:
            job = db.execute('SELECT * FROM team_jobs WHERE id=?', (p.get('job_id'),)).fetchone()
            if not job or job['actor_id'] != self.actor or not secrets.compare_digest(job['lease'],str(p.get('lease',''))):
                denied()
            req = self.requirement(job['requirement_id'],db)
            task = self.task(job['task_id'],db) if job['task_id'] else None
            version(task['revision'] if task else req['version'],job['version'])
            if task and task['owner_account_id'] != self.actor:
                denied()
            result = p.get('result')
            if p.get('error'):
                if job['status']!='running' or job['expires_at']<time.time():
                    raise ValueError('分析租约已失效')
                db.execute("UPDATE team_jobs SET status='failed',error=? WHERE id=?", (required_text(p['error'],'错误',1000),job['id']))
                return {'ok':True}
            if not isinstance(result,dict):
                raise ValueError('分析结果必须为对象')
            risks, questions = strings(result.get('risks'),'风险'),strings(result.get('questions'),'问题')
            summary = required_text(result.get('summary'),'分析结论',8000)
            if not task:
                result = {'summary':summary,'risks':risks,'questions':questions,'modules':modules(result.get('modules'))}
            else:
                contract = result.get('contract')
                if not isinstance(contract,dict):
                    raise ValueError('承接分析需提供执行契约草稿')
                result = {'summary':summary,'risks':risks,'questions':questions,'contract':contract}
            if job['status']=='completed' and dump(result)==job['result']:
                return {'ok':True}
            if job['status']!='running' or job['expires_at']<time.time():
                raise ValueError('分析租约已失效')
            if task:
                db.execute('UPDATE team_tasks SET analysis=? WHERE task_id=?', (dump(result),task['id']))
            db.execute("UPDATE team_jobs SET status='completed',result=?,thread_id=? WHERE id=?", (dump(result),str(p.get('thread_id',''))[:128],job['id']))
            if not task:
                db.execute("UPDATE requirements SET status='awaiting_product_confirmation' WHERE id=?", (req['id'],))
            for question in questions:
                db.execute('INSERT INTO team_questions(id,requirement_id,task_id,version,author_id,question) VALUES(?,?,?,?,?,?)',
                           (uuid.uuid4().hex,req['id'],task['id'] if task else None,req['version'],self.actor,question))
            self.notify(db,self.actor,req['id'],'分析完成，请查看风险和问题')
        return {'ok':True}

    def mark_read(self,p):
        with self.db.transaction() as db:
            db.execute('UPDATE team_notifications SET read=1 WHERE id=? AND account_id=?',(p.get('notification_id'),self.actor))
        return {'ok':True}
