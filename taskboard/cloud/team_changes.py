from core.service.domain import insert_relation
"""Human-authored revisions invalidate only affected approvals and their dependants."""
import json

from .team_directory import denied, dump, required_text, version


class TeamChangesMixin:
    def reset_execution_revision(self,db,task_id):
        db.execute("""UPDATE tasks SET status='draft',auto_dispatch=0,active_run_id=NULL,primary_run_id=NULL,
            codex_thread_id=NULL,assigned_to=NULL,implementation_contract='{}',review_contract='{}',
            location_context='{}',acceptance_plan='[]',dependency_analysis='{}',delivery_summary='',verification_result='',
            paused_from_status=NULL,blocked_from_status=NULL,retry_required=0,retry_run_type=NULL,
            review_retry_after=NULL,dispatch_retry_after=NULL,last_failure_reason='',last_dispatch_error='',
            review_rework_count=0,review_interrupt_count=0,dispatch_failure_count=0,
            context_version=context_version+1 WHERE id=?""",(task_id,))
        db.execute("UPDATE team_jobs SET status='superseded' WHERE task_id=? AND status!='completed'",(task_id,))
        db.execute("UPDATE task_runs SET status='interrupted' WHERE task_id=? AND status IN ('awaiting_thread','running','waiting_review')",(task_id,))
        db.execute("UPDATE native_dispatches SET status='failed' WHERE entity_type='task' AND entity_id=? AND status IN ('claimed','bound','pending_thread')",(task_id,))
        db.execute('DELETE FROM task_targets WHERE task_id=?',(task_id,))

    def request_stop(self,p):
        with self.db.connection() as db:
            task=self.task(p.get('task_id'),db)
            req=self.requirement(task['requirement_id'],db)
            version(task['revision'],p.get('revision'))
            if self.actor not in {task['owner_account_id'],req['product_id'],req['coordinator_id']}:
                denied()
        with self.db.transaction() as db:
            db.execute("UPDATE team_tasks SET handling='awaiting_stop',confirmed_revision=NULL WHERE task_id=?",(task['id'],))
            db.execute('UPDATE tasks SET auto_dispatch=0 WHERE id=?',(task['id'],))
            db.execute("UPDATE team_requirements SET integration='{}' WHERE requirement_id=?",(req['id'],))
            self.notify(db,task['owner_account_id'],req['id'],'请停止当前本地会话并确认，云端已禁止继续交付')
        if task['status'] not in {'done','cancelled','paused'}:
            self.transition_task(task['id'],'paused','团队要求停止并确认本地会话')
        return {'ok':True}

    def confirm_stopped(self,p):
        with self.db.transaction() as db:
            task=self.task(p.get('task_id'),db)
            version(task['revision'],p.get('revision'))
            if task['owner_account_id']!=self.actor:
                denied()
            if task['handling']!='awaiting_stop' or p.get('local_session_stopped') is not True:
                raise ValueError('需确认本地会话已经停止')
            db.execute("UPDATE team_tasks SET handling='stopped' WHERE task_id=?",(task['id'],))
            self._event(db,'task',task['id'],'local_stop_confirmed',{'actor':self.actor,'revision':task['revision']})
        return {'ok':True}

    def publish_revision(self,p):
        from .team_service import modules
        specs=modules(p.get('modules'))
        content=required_text(p.get('content'),'补充后的完整需求',30000)
        reason=required_text(p.get('reason'),'变更原因',4000)
        with self.db.transaction() as db:
            req=self.requirement(p.get('requirement_id'),db)
            version(req['version'],p.get('version'))
            if self.actor!=req['product_id']:
                denied()
            if not req['submitted_version']:
                raise ValueError('草稿请先完成需求分析和正式提交')
            old={s['key']:s for s in json.loads(req['decomposition_plan'])}
            new={s['key']:s for s in specs}
            affected={key for key in old.keys()|new.keys() if old.get(key)!=new.get(key)}
            if content!=req['original_content'] and not affected:
                affected=set(old)  # Unstructured scope change: do not guess unaffected tasks.
            if not affected:
                raise ValueError('没有需求变更')
            while True:
                extended=affected|{key for key in old.keys()|new.keys() if set(old.get(key,{}).get('depends_on',[])+new.get(key,{}).get('depends_on',[]))&affected}
                if extended==affected:
                    break
                affected=extended
            tasks={t['requirement_task_key']:dict(t) for t in db.execute('SELECT * FROM tasks WHERE requirement_id=?',(req['id'],))}
            for key in affected:
                if key not in tasks:
                    continue
                task=self.task(tasks[key]['id'],db)
                if task['active_run_id'] or task['handling']=='awaiting_stop':
                    raise ValueError('受影响任务需先停止并确认本地会话')
            next_version=req['version']+1
            snapshot={'content':content,'modules':specs,'coordinator_id':req['coordinator_id'],'reason':reason,'affected':sorted(affected)}
            db.execute('INSERT INTO team_versions(requirement_id,version,snapshot,actor_id) VALUES(?,?,?,?)',(req['id'],next_version,dump(snapshot),self.actor))
            db.execute("UPDATE requirements SET original_content=?,goal=?,decomposition_plan=?,status='in_delivery' WHERE id=?",(content,content,dump(specs),req['id']))
            db.execute("UPDATE team_requirements SET version=?,submitted_version=?,accepted_version=NULL,integration='{}' WHERE requirement_id=?",(next_version,next_version,req['id']))
            req=self.requirement(req['id'],db)
            for key in new.keys()-old.keys():
                if key in tasks:
                    continue
                spec=new[key]
                tid=self.db.next_id(db,'TASK')
                db.execute('''INSERT INTO tasks(id,requirement_id,requirement_task_key,title,goal,project,modules,scope,out_of_scope,acceptance_criteria,status,auto_dispatch)
                    VALUES(?,?,?,?,?,?,?,?,?,?,'draft',0)''',
                    (tid,req['id'],key,spec['title'],spec['goal'],req['project'],dump([key]),dump(spec['scope']),dump(spec['out_of_scope']),dump(spec['acceptance_criteria'])))
                db.execute('INSERT INTO team_tasks(task_id,requirement_version) VALUES(?,?)',(tid,next_version))
                tasks[key]={'id':tid}
            for key in affected:
                tid=tasks[key]['id']
                if key not in new:
                    db.execute("UPDATE tasks SET status='cancelled',auto_dispatch=0 WHERE id=?",(tid,))
                    db.execute("UPDATE team_tasks SET handling='retired',confirmed_revision=NULL,delivered_revision=NULL,revision=revision+1 WHERE task_id=?",(tid,))
                    db.execute("UPDATE team_jobs SET status='superseded' WHERE task_id=? AND status!='completed'",(tid,))
                    db.execute("UPDATE task_runs SET status='interrupted' WHERE task_id=? AND status IN ('awaiting_thread','running','waiting_review')",(tid,))
                    db.execute("UPDATE native_dispatches SET status='failed' WHERE entity_type='task' AND entity_id=? AND status IN ('claimed','bound','pending_thread')",(tid,))
                    continue
                spec=new[key]
                # This is a new revision, not a normal transition of the previous
                # execution. Old runs and immutable version snapshots are retained.
                self.reset_execution_revision(db,tid)
                db.execute("""UPDATE tasks SET status='draft',auto_dispatch=0,title=?,goal=?,scope=?,out_of_scope=?,
                    acceptance_criteria=?,active_run_id=NULL,primary_run_id=NULL,codex_thread_id=NULL,
                    implementation_contract='{}',review_contract='{}',location_context='{}',acceptance_plan='[]',
                    delivery_summary='',verification_result='' WHERE id=?""",
                    (spec['title'],spec['goal'],dump(spec['scope']),dump(spec['out_of_scope']),dump(spec['acceptance_criteria']),tid))
                db.execute("""UPDATE team_tasks SET revision=revision+1,requirement_version=?,confirmed_revision=NULL,
                    delivered_revision=NULL,handling='',analysis='{}',interfaces=? WHERE task_id=?""",(next_version,dump(spec['interfaces']),tid))
                db.execute("UPDATE team_jobs SET status='superseded' WHERE task_id=? AND status!='completed'",(tid,))
                db.execute("UPDATE task_runs SET status='interrupted' WHERE task_id=? AND status IN ('awaiting_thread','running','waiting_review')",(tid,))
                db.execute("UPDATE native_dispatches SET status='failed' WHERE entity_type='task' AND entity_id=? AND status IN ('claimed','bound','pending_thread')",(tid,))
                db.execute('DELETE FROM task_targets WHERE task_id=?',(tid,))
                db.execute("DELETE FROM task_relations WHERE source_task_id=? AND relation_type='depends_on'",(tid,))
                for dependency in spec['depends_on']:
                    insert_relation(db,tid,tasks[dependency]['id'],'depends_on','模块交付依赖')
                updated=self.task(tid,db)
                if updated['owner_account_id']:
                    self.queue_analysis(db,req,updated)
                self._event(db,'task',tid,'team_revision_published',{'actor':self.actor,'revision':updated['revision'],'reason':reason})
        return {'version':next_version,'affected_modules':sorted(affected)}
