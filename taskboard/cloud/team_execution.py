"""Version-bound bridge into core execution; no second execution state machine."""
import json
import hashlib

from .team_directory import denied, required_text, dump, version


class TeamExecutionMixin:
    def _execution_admission(self, task_id, stage, run_id=None):
        if not task_id or task_id != getattr(self, 'execution_scope_task_id', None):
            denied()
        with self.db.connection() as db:
            task = self.task(task_id, db)
            req = self.requirement(task['requirement_id'], db)
            if (task['owner_account_id'] != self.actor or task['confirmed_revision'] != task['revision']
                    or not req['submitted_version'] or task['handling']):
                raise ValueError('当前任务版本尚未获得负责人确认')
            if run_id:
                binding = db.execute('SELECT * FROM team_run_versions WHERE run_id=?', (run_id,)).fetchone()
                if not binding or binding['actor_id'] != self.actor or binding['revision'] != task['revision'] or binding['task_id'] != task_id:
                    raise ValueError('运行身份或版本已失效')
                run = db.execute('SELECT *, datetime(lease_expires_at)>CURRENT_TIMESTAMP AS valid_lease FROM task_runs WHERE id=?',(run_id,)).fetchone()
                if not run or not run['valid_lease'] or run['status'] in {'interrupted','failed','cancelled'}:
                    raise ValueError('运行租约已失效')

    def _claim_requirement_decomposition(self, *args, **kwargs):
        return None

    def confirm_owner(self, p):
        with self.db.connection() as db:
            task = self.task(p.get('task_id'), db)
            req = self.requirement(task['requirement_id'], db)
            version(task['revision'], p.get('revision'))
            if task['owner_account_id'] != self.actor:
                denied()
            if task['handling'] not in {'','stopped'} or db.execute("SELECT 1 FROM team_questions WHERE requirement_id=? AND answer='' AND (task_id=? OR task_id IS NULL)", (req['id'],task['id'])).fetchone():
                raise ValueError('请先解决需求问题或阻塞')
            analysis = json.loads(task['analysis'])
            if not analysis:
                raise ValueError('请先完成承接分析')
            if task['confirmed_revision'] == task['revision']:
                return {'ok':True}
        self.execution_scope_task_id = task['id']
        with self.db.transaction() as db:
            db.execute("UPDATE team_tasks SET confirmed_revision=revision,handling='' WHERE task_id=?", (task['id'],))
        try:
            if task['status']=='paused':
                self.resume_task(task['id'])
                with self.db.connection() as db:
                    task=self.task(task['id'],db)
            if task['status'] == 'draft':
                contract = analysis['contract']
                allowed = {'location_evidence','targets','acceptance_plan','quality_gates','review_checks','location_summary'}
                payload = {k:v for k,v in contract.items() if k in allowed}
                if isinstance(payload.get('location_evidence'),dict):
                    payload['location_evidence']={**payload['location_evidence'],'project_path':task['project']}
                payload.update({k: task[k] for k in ('title','goal','project','priority','type')})
                payload.update({k:json.loads(task[k]) for k in ('modules','scope','out_of_scope')})
                plan = payload.get('acceptance_plan',[])
                if sorted(x.get('criterion','') for x in plan) != sorted(json.loads(task['acceptance_criteria'])):
                    raise ValueError('执行验收计划必须覆盖已确认的模块验收标准')
                location = self.prepare_location_analysis(payload)
                payload.update({'analysis_id':location['analysis_id'],'intake_kind':'task','auto_dispatch':False,
                                'dependency_analysis':{'decision':'independent'}})
                self.finalize_task_intake(payload,existing_task_id=task['id'])
            with self.db.transaction() as db:
                db.execute('UPDATE tasks SET auto_dispatch=1 WHERE id=?',(task['id'],))
                db.execute("UPDATE requirements SET status='in_delivery' WHERE id=?",(req['id'],))
                self._event(db,'task',task['id'],'owner_confirmed',{'actor':self.actor,'revision':task['revision']})
            self.set_dispatcher_enabled(True)
        except Exception:
            with self.db.transaction() as db:
                db.execute('UPDATE team_tasks SET confirmed_revision=NULL WHERE task_id=?',(task['id'],))
                db.execute('UPDATE tasks SET auto_dispatch=0 WHERE id=?',(task['id'],))
            raise
        return {'ok':True}

    def submit_delivery(self,run_id,delivery_summary,verification_result,changed_locations,acceptance_evidence,token_used=0,**kwargs):
        run=self.get_run(run_id)
        self._execution_admission(run['task_id'],'delivery',run_id)
        task=self.get_task(run['task_id'])
        state=self._workspace_state(task['project'])
        required=task['review_contract']['quality_gates']['code_review']['required']
        if not state.get('available') or required and state.get('files'):
            raise ValueError('代码交付必须提交为干净的 Git revision')
        result=super().submit_delivery(run_id,delivery_summary,verification_result,changed_locations,acceptance_evidence,token_used,**kwargs)
        after=self._workspace_state(task['project'])
        if after.get('revision')!=state.get('revision') or required and after.get('files'):
            raise ValueError('验证过程改变了交付产物，请清理并重新交付')
        baseline=(run.get('context_snapshot') or {}).get('workspace_baseline') or {}
        artifact=self._workspace_diff(task['project'],baseline.get('revision',''),[item['file'] for item in changed_locations])
        with self.db.transaction() as db:
            db.execute("UPDATE task_runs SET output_revision=?,artifact_sha256=CASE WHEN artifact_sha256='' THEN ? ELSE artifact_sha256 END WHERE id=?",
                (state.get('revision',''),artifact.get('sha256') or hashlib.sha256(dump({'revision':state.get('revision'),'summary':delivery_summary,'evidence':acceptance_evidence}).encode()).hexdigest(),run_id))
        return result

    def execution_claim(self, p):
        with self.db.connection() as db:
            task = self.task(p.get('task_id'),db)
            version(task['revision'],p.get('revision'))
        self.execution_scope_task_id = task['id']
        self._execution_admission(task['id'],'claim')
        stage = 'code_review' if task['status']=='code_review' else 'development'
        dispatch = self._claim_next_native_dispatch_locked(self.actor+':'+task['id'],task['project'],1800,stage)
        if not dispatch:
            return None
        with self.db.transaction() as db:
            req = self.requirement(task['requirement_id'],db)
            db.execute('INSERT OR IGNORE INTO team_run_versions VALUES(?,?,?,?,?)', (dispatch['run_id'],task['id'],task['revision'],self.actor,req['project_id']))
            project = self.project(req['project_id'],db)
            attachments=self.attachments(req['id'],db,content=True)
        return {**dispatch,'team_id':self.team_id,'task_revision':task['revision'],'team_project':project,'attachments':attachments}

    def execution_tool(self,p):
        from taskboard.mcp_server import tool_handlers_for
        name, arguments = p.get('name'), p.get('arguments',{})
        allowed = {'get_task_details','get_dispatch_status','bind_native_dispatch',
                   'renew_dispatch_lease','report_dispatch_failed','report_run_blocked','submit_task_delivery',
                   'review_code'}
        if name not in allowed or not isinstance(arguments,dict):
            denied()
        if arguments.get('workspace_path'):
            raise ValueError('团队执行工作区由服务端绑定，不能由回调覆盖')
        self.execution_scope_task_id = p.get('task_id')
        with self.db.connection() as db:
            task = self.task(p.get('task_id'),db)
            version(task['revision'],p.get('revision'))
            if task['owner_account_id'] != self.actor:
                denied()
            for key in ('task_id','entity_id'):
                if key in arguments and arguments[key] != task['id']:
                    denied()
            run_id = arguments.get('run_id')
            if run_id:
                binding = db.execute('SELECT * FROM team_run_versions WHERE run_id=?',(run_id,)).fetchone()
                if not binding or binding['task_id']!=task['id'] or binding['revision']!=task['revision'] or binding['actor_id']!=self.actor:
                    denied()
                if name=='bind_native_dispatch':
                    run=db.execute('SELECT run_type FROM task_runs WHERE id=?',(run_id,)).fetchone()
                    if run['run_type']=='code_review' and db.execute('''SELECT 1 FROM task_run_conversations c
                        JOIN task_runs r ON r.id=c.run_id WHERE r.task_id=? AND r.run_type!='code_review' AND c.thread_id=?''',
                        (task['id'],arguments.get('thread_id'))).fetchone():
                        raise ValueError('代码审查必须使用独立于开发的会话')
        if name not in {'get_task_details','get_dispatch_status'}:
            self._execution_admission(task['id'],'callback',run_id)
        handler = tool_handlers_for(self).get(name)
        if handler is None:
            raise ValueError('不支持的执行回调')
        return handler(arguments)

    def confirm_delivery(self,p):
        with self.db.transaction() as db:
            task = self.task(p.get('task_id'),db)
            version(task['revision'],p.get('revision'))
            if task['owner_account_id']!=self.actor:
                denied()
            if task['status']!='done' or task['confirmed_revision']!=task['revision'] or task['handling']:
                raise ValueError('当前版本尚未通过执行和审查门禁')
            run = db.execute('SELECT * FROM task_runs WHERE id=?',(task['primary_run_id'],)).fetchone()
            if not run or not run['verification_result'] or not run['delivery_summary']:
                raise ValueError('缺少可核验的交付记录')
            db.execute('UPDATE team_tasks SET delivered_revision=revision WHERE task_id=?',(task['id'],))
            req = self.requirement(task['requirement_id'],db)
            self.notify(db,req['coordinator_id'],req['id'],'模块已交付，请检查整体集成')
        return {'ok':True}

    def integration_members(self, req, db):
        tasks = db.execute('''SELECT t.id,t.primary_run_id,t.status,m.revision,m.delivered_revision,m.confirmed_revision,m.handling,
            r.output_revision,r.artifact_sha256,r.integration_revision FROM tasks t
            JOIN team_tasks m ON m.task_id=t.id LEFT JOIN task_runs r ON r.id=t.primary_run_id
            WHERE t.requirement_id=? AND m.handling!='retired' ''',(req['id'],)).fetchall()
        if not tasks or any(t['status']!='done' or t['revision']!=t['delivered_revision'] or t['confirmed_revision']!=t['revision'] or t['handling'] for t in tasks):
            raise ValueError('还有未交付的模块')
        if db.execute("SELECT 1 FROM team_questions WHERE requirement_id=? AND answer=''",(req['id'],)).fetchone():
            raise ValueError('还有未解决的问题')
        return [dict(t) for t in tasks]

    def confirm_integration(self,p):
        evidence = required_text(p.get('evidence'),'集成验证证据',16000)
        revision = required_text(p.get('integration_revision'),'集成产物版本',128)
        command = required_text(p.get('command'),'集成验证命令',10000)
        with self.db.transaction() as db:
            req = self.requirement(p.get('requirement_id'),db)
            version(req['version'],p.get('version'))
            if self.actor!=req['coordinator_id']:
                denied()
            members = self.integration_members(req,db)
            verification=self._integration_rpc(req,self.project(req['project_id'],db),members,revision,command)
            if verification.get('status')!='passed' or verification.get('revision')!=revision or verification.get('members')!=members:
                raise ValueError('集成验证证据不匹配')
            record = {'version':req['version'],'members':members,'integration_revision':revision,'evidence':evidence,'verification':verification,'actor':self.actor}
            db.execute('UPDATE team_requirements SET integration=? WHERE requirement_id=?',(dump(record),req['id']))
            db.execute("UPDATE requirements SET status='awaiting_acceptance' WHERE id=?",(req['id'],))
            self.notify(db,req['product_id'],req['id'],'集成已确认，请验收需求')
        return {'ok':True}

    def accept_requirement(self,p):
        with self.db.transaction() as db:
            req = self.requirement(p.get('requirement_id'),db)
            version(req['version'],p.get('version'))
            if self.actor!=req['product_id']:
                denied()
            integration = json.loads(req['integration'])
            if req['status']!='awaiting_acceptance' or integration.get('version')!=req['version'] or integration.get('members')!=self.integration_members(req,db):
                raise ValueError('集成证据与当前模块版本不一致')
            db.execute('UPDATE team_requirements SET accepted_version=version WHERE requirement_id=?',(req['id'],))
            db.execute("UPDATE requirements SET status='done' WHERE id=?",(req['id'],))
            self._event(db,'requirement',req['id'],'product_accepted',{'actor':self.actor,'version':req['version']})
        return {'ok':True}
