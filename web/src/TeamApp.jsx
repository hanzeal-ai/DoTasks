import { TaskSources } from './TaskSources';
import { useEffect, useState, useRef } from 'react';
import { Button } from './components/ui/button';
import { Input } from './components/ui/input';
import { Textarea } from './components/ui/textarea';
import { NativeSelect } from './components/ui/native-select';
import { requestJson } from './ui-core';
import './team.css';
import { subscribeTeamBoard } from './team-board-sync';

const parse = (value, fallback = []) => { try { return typeof value === 'string' ? JSON.parse(value) : value ?? fallback; } catch { return fallback; } };
const lines = value => value.split('\n').map(x => x.trim()).filter(Boolean);
const labels = {draft:'待分析',submitted:'待分配',in_delivery:'交付中',awaiting_product_confirmation:'待产品确认',awaiting_acceptance:'待产品验收',
  awaiting_assignment:'待分配',assigned:'等待本地分析',awaiting_owner_confirmation:'待承接确认',ready:'等待开发',claimed:'已领取',
  implementing:'开发中',investigating:'分析中',code_review:'代码审查',awaiting_delivery_confirmation:'待交付确认',done:'已完成',
  awaiting_clarification:'待补充需求',awaiting_reassignment:'待改派',blocked:'依赖阻塞',paused:'已暂停',stopped:'已确认停止',
  awaiting_stop:'等待本地停止确认',queued:'等待本地领取',awaiting_permission:'等待授权分析',running:'正在分析',completed:'分析完成',
  failed:'失败',uncertain:'需核对本地会话',superseded:'版本已更新',rework:'返工中',cancelled:'已取消',retired:'已移出需求'};
const api = (path, payload) => requestJson(fetch,path,payload === undefined ? undefined : {method:'POST',body:JSON.stringify(payload)});

function Field({label, name, multiline=false, ...props}) {
  return <label className="team-field"><span>{label}</span>{multiline ? <Textarea name={name} rows={3} {...props}/> : <Input name={name} {...props}/>}</label>;
}
function Select({label,name,items,...props}) {
  return <label className="team-field"><span>{label}</span><NativeSelect name={name} {...props}>{items.map(x=><option key={x.id} value={x.id}>{x.name || x.username || x.id}</option>)}</NativeSelect></label>;
}
function Form({onSubmit, children, label, busy}) {
  return <form className="team-form" onSubmit={event=>{event.preventDefault(); if (!busy) onSubmit(Object.fromEntries(new FormData(event.currentTarget)));}}>
    {children}<Button disabled={busy} type="submit">{label}</Button></form>;
}
function Recovery({job,mutate,busy}) {
  if(!job || !['running','uncertain'].includes(job.status))return null;
  return <details><summary>核对并恢复本地分析</summary><Form busy={busy} label="确认原会话已停止" onSubmit={p=>mutate('resolve-analysis',{...p,job_id:job.id,local_session_stopped:true})}>
    <p>先在本机核对分析收据中的会话并停止运行；确认后可重新开始分析。</p><Field label="已核对的会话及停止结果" name="reason" required multiline/>
  </Form></details>;
}
function RequirementUpload({board,mutate,busy}) {
  const [attachments,setAttachments]=useState([]),[error,setError]=useState(''),[reading,setReading]=useState(false);
  const selection = useRef(0);
  const policy = board.attachment_policy;
  async function select(event){
    const current = ++selection.current;
    setReading(true); setError(''); setAttachments([]);
    try {
      if (!policy) throw new Error('服务暂未提供附件限制，请刷新后重试');
      const files=Array.from(event.target.files);
      if (files.length>policy.max_count || files.some(f=>!f.size || f.size>policy.max_file_bytes) || files.reduce((n,f)=>n+f.size,0)>policy.max_total_bytes)
        throw new Error('最多 4 个附件，每个 2 MiB，合计 4 MiB');
      const results=await Promise.all(files.map(file=>new Promise((resolve,reject)=>{
        const mime=file.name.toLowerCase().endsWith('.md')?'text/markdown':file.type || 'text/plain';
        if (!policy.mime_types.includes(mime) || (mime.startsWith('text/') && file.size>policy.max_text_bytes)) { reject(new Error('支持 PNG、JPEG、TXT、Markdown；文本最大 256 KiB')); return; }
        const reader=new FileReader();reader.onerror=()=>reject(new Error('附件读取失败'));reader.onload=()=>resolve({name:file.name,mime,content_base64:String(reader.result).split(',')[1]});reader.readAsDataURL(file);
      })));
      if(current===selection.current) setAttachments(results);
    }catch(e){if(current===selection.current)setError(e.message || '附件读取失败');}
    finally {if(current===selection.current)setReading(false);}
  }
  return <details className="team-panel"><summary>上传需求</summary><Form label="保存需求并通知本地" busy={busy || reading || Boolean(error)} onSubmit={p=>mutate('upload-requirement',{...p,attachments})}>
    <Select label="项目" name="project_id" items={board.projects}/><Select label="开发协调人" name="coordinator_id" items={board.members.filter(m=>['admin','developer','coordinator'].includes(m.role))}/><Field label="需求标题" name="title" required maxLength={120}/><Field label="需求原文" name="content" multiline required maxLength={30000}/>
    <label className="team-field">附件（PNG、JPEG、TXT、Markdown；最多 4 个，各 2 MiB，合计 4 MiB；文本各 256 KiB）<input type="file" multiple accept=".png,.jpg,.jpeg,.txt,.md" onChange={select}/></label>{error && <p role="alert">{error}</p>}
  </Form></details>;
}
async function downloadAttachment(mutate,id){
  const result=await mutate('read-attachment',{attachment_id:id});if(!result)return;
  const bytes=Uint8Array.from(atob(result.content_base64),c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([bytes],{type:result.mime}));
  const a=document.createElement('a');a.href=url;a.download=result.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function ModuleEditor({value,onChange}) {
  const fields={title:'模块标题',goal:'目标',scope:'范围（每行一项）',out_of_scope:'不包含内容',acceptance_criteria:'验收标准（每行一项）',interfaces:'接口及数据约定（无则留空）',depends_on:'依赖模块键（每行一个，无则留空）'};
  const change=(index,key,newValue)=>onChange(value.map((m,i)=>i===index?{...m,[key]:newValue}:m));
  return <div className="team-modules">{value.map((m,index)=><fieldset key={index}><legend>模块 {index+1}</legend>
    <Field label="模块键" value={m.key} required onChange={e=>change(index,'key',e.target.value)} />
    {Object.entries(fields).map(([key,label])=><Field key={key} label={label} multiline value={Array.isArray(m[key])?m[key].join('\n'):m[key] || ''}
      required={['title','goal','scope','acceptance_criteria'].includes(key)} onChange={e=>change(index,key,['title','goal'].includes(key)?e.target.value:lines(e.target.value))}/>)}
    <Button type="button" variant="outline" onClick={()=>onChange(value.filter((_,i)=>i!==index))}>移除模块</Button>
  </fieldset>)}<Button type="button" variant="outline" disabled={value.length>=30} onClick={()=>onChange([...value,{key:'',title:'',goal:'',scope:[],out_of_scope:[],acceptance_criteria:[],interfaces:[],depends_on:[]}])}>添加模块</Button></div>;
}

function RequirementEditor({requirement,job,mutate,busy}) {
  const initial=parse(requirement.decomposition_plan);
  const [specs,setSpecs]=useState(initial.length?initial:parse(job?.result,{}).modules || []);
  const [content,setContent]=useState(requirement.original_content);
  const [reason,setReason]=useState('');
  const submitted=Boolean(requirement.submitted_version);
  return <details><summary>{submitted?'补充需求 / 验收返工':'确认模块拆分并提交'}</summary><form className="team-form" onSubmit={e=>{e.preventDefault();mutate(submitted?'publish-revision':'submit-requirement',{
    requirement_id:requirement.id,version:requirement.version,modules:specs,...(submitted?{content,reason}:{})});}}>
    {submitted && <><Field label="完整需求" multiline required value={content} onChange={e=>setContent(e.target.value)}/><Field label="变更或返工原因" multiline required value={reason} onChange={e=>setReason(e.target.value)}/></>}
    <ModuleEditor value={specs} onChange={setSpecs}/>
    <Button disabled={busy || !specs.length || (!submitted && job?.status!=='completed')}>{submitted?'发布补充版本':'确认并正式提交'}</Button>
  </form></details>;
}

function Assignment({task,mutate,busy}) {
  const [recommendation,setRecommendation]=useState(null);
  return <div className="team-actions"><Button variant="outline" disabled={busy} onClick={async()=>{const r=await mutate('recommend',{task_id:task.id,revision:task.revision});if(r)setRecommendation(r);}}>推荐负责人</Button>
    {recommendation && <Form busy={busy} label="确认派发" onSubmit={p=>mutate('assign',{...p,task_id:task.id,revision:task.revision,candidate_version:recommendation.candidate_version})}>
      <p>{recommendation.advice?'Laya 已给出适配建议，请确认负责人。':'模型暂无建议，可从合格成员中选择。'}</p>
      {!recommendation.candidates.length ? <p>没有满足模块能力和容量条件的成员，请调整项目授权。</p> : <Select label="负责人" name="owner_account_id" required items={[{id:'',name:'请选择'},...recommendation.candidates.map(c=>({...c,name:`${c.username} · 已承接 ${c.active}/${c.capacity}${recommendation.advice ? ' · 适配 '+(recommendation.advice.candidates.find(x=>x.id===c.id)?.relevance ?? '未知') : ''}`}))]}/>}
    </Form>}
  </div>;
}

function TaskCard({task,req,board,mutate,busy}) {
  const mine=task.owner_account_id===board.actor_id;
  const manager=[req.product_id,req.coordinator_id].includes(board.actor_id);
  const analysis=parse(task.analysis,{});
  const job=board.jobs.findLast(j=>j.task_id===task.id && j.version===task.revision);
  const owner=board.members.find(m=>m.id===task.owner_account_id)?.username || '未分配';
  const payload={task_id:task.id,revision:task.revision};
  return <section className="team-task"><div className="team-heading"><h3>{task.title}</h3><span>{labels[task.phase] || task.phase}</span></div>
    <p>{owner} · 任务版本 {task.revision}</p><p>{task.goal}</p>
    <ul>{parse(task.acceptance_criteria).map((c,i)=><li key={i}>{c}</li>)}</ul>
    {job && <p>本地分析：{labels[job.status] || job.status}{job.error && ` · ${job.error}`}</p>}
    {mine && <Recovery job={job} mutate={mutate} busy={busy}/>}
    {mine && job && ['awaiting_permission','failed'].includes(job.status) && <Button disabled={busy} onClick={()=>mutate('start-analysis',{job_id:job.id})}>开始只读分析</Button>}
    {analysis.summary && <><p>{analysis.summary}</p><ul>{analysis.risks?.map((r,i)=><li key={i}>风险：{r}</li>)}</ul></>}
    {analysis.contract && <details><summary>将执行的开发与验证范围</summary><ul>{analysis.contract.targets?.map((t,i)=><li key={i}>{t.file} · {t.mode} · {t.reason}</li>)}</ul>{analysis.contract.acceptance_plan?.map((a,i)=><section key={i}><p>{a.criterion}：{a.method} → {a.expected}</p>{a.command && <pre>{a.command}</pre>}</section>)}<p>代码审查：{analysis.contract.quality_gates?.code_review?.required?'需要':'不需要'} · {analysis.contract.quality_gates?.code_review?.reason}</p></details>}
    {manager && ['awaiting_assignment','awaiting_reassignment'].includes(task.phase) && <Assignment task={task} mutate={mutate} busy={busy}/>}
    {mine && ['awaiting_owner_confirmation','stopped'].includes(task.phase) && <Button disabled={busy} onClick={()=>mutate('confirm-owner',payload)}>确认此版本并开始开发</Button>}
    {mine && !['done','awaiting_stop','cancelled'].includes(task.phase) && <details><summary>提问、退回补充或请求改派</summary><Form busy={busy} label="提交问题" onSubmit={p=>mutate('ask',{...payload,...p})}>
      <Select label="处理方式" name="kind" items={[{id:'awaiting_clarification',name:'请产品补充需求'},{id:'awaiting_reassignment',name:'请求改派'},{id:'blocked',name:'技术依赖阻塞'}]}/>
      <Field label="问题与需要的决定" name="question" multiline required/></Form></details>}
    {(mine || manager) && task.active_run_id && <Button variant="outline" disabled={busy} onClick={()=>mutate('request-stop',payload)}>请求停止当前运行</Button>}
    {mine && task.phase==='awaiting_stop' && <Button disabled={busy} onClick={()=>mutate('confirm-stopped',{...payload,local_session_stopped:true})}>已检查本地会话并确认停止</Button>}
    {task.delivery && <details><summary>交付证据</summary><p>{task.delivery.delivery_summary}</p><pre>{task.delivery.verification_result}</pre><code>{task.delivery.output_revision || task.delivery.artifact_sha256}</code></details>}
    {mine && task.phase==='awaiting_delivery_confirmation' && <Button disabled={busy} onClick={()=>mutate('confirm-delivery',payload)}>确认模块交付</Button>}
  </section>;
}

export default function TeamApp() {
  const [teams,setTeams]=useState([]),[team,setTeam]=useState(''),[board,setBoard]=useState(null);
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true);
  const requests=useRef(new Map());
  const boardSync=useRef(null);
  const refresh=async()=>boardSync.current?.refresh();
  useEffect(()=>{let live=true;api('/api/teams').then(r=>{if(live){setTeams(r.teams);setTeam(r.teams[0]?.id || '');}}).catch(e=>setError(e.message)).finally(()=>setLoading(false));return()=>{live=false;};},[]);
  useEffect(()=>{
    setBoard(null); setError('');
    if(!team)return;
    const subscription=subscribeTeamBoard({url:'/api/teams/'+team+'/events',load:()=>api('/api/teams/'+team),onData:data=>{setBoard(data);setError('');},onError:e=>setError(e.message)});
    boardSync.current=subscription;
    return()=>{subscription.close();if(boardSync.current===subscription)boardSync.current=null;};
  },[team]);
  async function mutate(action,payload) {
    if(busy)return;
    setBusy(true);setError('');
    const key=JSON.stringify([team,action,payload]);
    if(!requests.current.has(key))requests.current.set(key,crypto.randomUUID());
    try {const result=await api('/api/teams/'+team+'/'+action,{...payload,request_id:requests.current.get(key)});await refresh();if(action!=='upload-requirement')requests.current.delete(key);return result;}
    catch(e){setError(e.message);return null;}finally{setBusy(false);}
  }
  async function createTeam(p) {
    setBusy(true);setError('');const key=JSON.stringify(['create-team',p]);if(!requests.current.has(key))requests.current.set(key,crypto.randomUUID());try {const created=await api('/api/teams',{...p,request_id:requests.current.get(key)});setTeams((await api('/api/teams')).teams);setTeam(created.id);}catch(e){setError(e.message);}finally{setBusy(false);}
  }
  return <section className="team-shell" aria-label="团队协作"><div className="team-content"><div className="team-toolbar"><Select label="团队" items={[{id:'',name:'选择团队'},...teams]} value={team} onChange={e=>setTeam(e.target.value)}/>
      <details><summary>创建团队</summary><Form label="创建" busy={busy} onSubmit={createTeam}><Field label="团队名称" name="name" required maxLength={120}/></Form></details>
      {team && <Button variant="outline" disabled={busy} onClick={()=>refresh().catch(e=>setError(e.message))}>刷新</Button>}</div>
      {error && <p role="alert" className="team-error">{error}</p>}
      {loading && <p role="status">正在加载团队…</p>}
      {!loading && !teams.length && <p>尚未加入团队。可创建团队，或请管理员添加你的账号。</p>}
      {board && <>
        <TaskSources key={team} board={board} mutate={mutate} busy={busy}/>
        <details className="team-panel"><summary>成员与项目设置</summary>
          {board.role==='admin' && <div className="team-grid">
            <Form label="添加成员" busy={busy} onSubmit={p=>mutate('members',p)}><Field label="已注册账号" name="username" required/><Select label="角色" name="role" items={[{id:'product',name:'产品'},{id:'developer',name:'开发'},{id:'coordinator',name:'开发协调人'}]}/></Form>
            <Form label="创建项目" busy={busy} onSubmit={p=>mutate('create-project',p)}><Field label="项目名称" name="name" required/><Field label="仓库 origin 地址" name="repository" required/><Field label="允许基线（完整 Git revision）" name="baseline" required pattern="[0-9a-f]{40,64}"/></Form>
            {board.projects.length>0 && <Form label="保存项目授权" busy={busy} onSubmit={p=>mutate('grant-project',{...p,modules:lines(p.modules),capacity:Number(p.capacity)})}>
              <Select label="项目" name="project_id" items={board.projects}/><Select label="成员" name="account_id" items={board.members}/><Field label="可负责的模块键（每行一个）" name="modules" multiline/><Field label="承接容量" name="capacity" type="number" min={1} max={10} defaultValue={1}/></Form>}
          </div>}
          {board.role==='admin' && <details><summary>撤销授权或移除成员</summary><Form label="撤销项目授权" busy={busy} onSubmit={p=>mutate('revoke-project',p)}><Select label="项目" name="project_id" items={board.projects}/><Select label="成员" name="account_id" items={board.members.filter(m=>m.id!==board.actor_id)}/></Form><Form label="移除已无项目授权的成员" busy={busy} onSubmit={p=>mutate('remove-member',p)}><Select label="成员" name="account_id" items={board.members.filter(m=>m.id!==board.actor_id)}/></Form></details>}
          {board.projects.map(project=><section key={project.id} className="team-task"><h3>{project.name}</h3><p>本机先执行以下命令绑定仓库，将路径替换为实际目录：</p><code className="team-command">dotasks team-bind --team {team} --project {project.id} --path /本地项目路径</code>
            <Form label="保存我的分析授权" busy={busy} onSubmit={p=>mutate('preferences',{project_id:project.id,auto_analysis:p.auto_analysis==='true',token_budget:Number(p.token_budget)})}>
              <Select label="自动只读分析" name="auto_analysis" defaultValue={String(Boolean(project.auto_analysis))} items={[{id:'false',name:'每次手动开始'},{id:'true',name:'收到任务后自动分析'}]}/><Field label="单次分析 Token 预算" name="token_budget" type="number" min={1000} max={60000} defaultValue={project.token_budget}/>
            </Form></section>)}
        </details>
        <details className="team-panel"><summary>通知（{board.notifications.filter(n=>!n.read).length} 条未读）</summary>{board.notifications.length?board.notifications.map(n=><div className="team-notice" key={n.id}><span>{n.message}</span>{!n.read && <Button variant="outline" disabled={busy} onClick={()=>mutate('mark-read',{notification_id:n.id})}>标记已读</Button>}</div>):<p>暂无通知</p>}</details>
        {['admin','product'].includes(board.role) && board.projects.length>0 && <RequirementUpload board={board} mutate={mutate} busy={busy}/>}
        {!board.requirements.length && <p className="team-empty">当前项目暂无共享需求。</p>}
        {board.requirements.map(req=>{
          const job=board.jobs.findLast(j=>j.requirement_id===req.id && !j.task_id && j.version===req.version);
          const result=parse(job?.result,{});const product=req.product_id===board.actor_id;
          return <article className="team-panel" key={req.id}><div className="team-heading"><h2>{req.title}</h2><span>{labels[req.status] || req.status} · v{req.version}</span></div><p className="team-original">{req.original_content}</p>{req.source_reference && <p><a href={req.source_reference} target="_blank" rel="noopener noreferrer">查看来源记录 ↗</a></p>}
            {job && <p>产品分析：{labels[job.status] || job.status}{job.error && ` · ${job.error}`}</p>}
            {req.attachments?.map(a=><Button key={a.id} variant="outline" disabled={busy} onClick={()=>downloadAttachment(mutate,a.id)}>{a.name}</Button>)}
            {product && <Recovery job={job} mutate={mutate} busy={busy}/>}
            {product && job && ['awaiting_permission','failed'].includes(job.status) && <Button disabled={busy} onClick={()=>mutate('start-analysis',{job_id:job.id})}>开始需求分析</Button>}
            {result.summary && <p>{result.summary}</p>}{result.risks?.length>0 && <ul>{result.risks.map((r,i)=><li key={i}>风险：{r}</li>)}</ul>}
            {board.questions.filter(q=>q.requirement_id===req.id).map(q=>{const task=board.tasks.find(t=>t.id===q.task_id);const responsible=['blocked','awaiting_reassignment'].includes(q.kind)?req.coordinator_id:req.product_id;return <section className="team-question" key={q.id}><strong>{q.question}</strong>{q.answer?<p>{q.answer}</p>:responsible===board.actor_id?<Form label="保存答复" busy={busy} onSubmit={p=>mutate('answer',{...p,question_id:q.id,version:req.version,scope_changed:false})}><Field label="按原范围解释；改变范围请发布补充版本" name="answer" multiline required/></Form>:<p>等待负责人答复</p>}</section>;})}
            {product && <RequirementEditor key={req.id+':'+req.version+':'+job?.status} requirement={req} job={job} mutate={mutate} busy={busy}/>}
            <div className="team-grid">{board.tasks.filter(t=>t.requirement_id===req.id).map(task=><TaskCard key={task.id+':'+task.revision} task={task} req={req} board={board} mutate={mutate} busy={busy}/>)}</div>
            {req.coordinator_id===board.actor_id && <details><summary>确认整体集成</summary><Form label="确认集成结果通过" busy={busy} onSubmit={p=>mutate('confirm-integration',{...p,requirement_id:req.id,version:req.version})}><Field label="集成 revision / 产物版本" name="integration_revision" required/><Field label="在协调人本机执行的集成验证命令" name="command" required/><Field label="对应全部模块版本的验证证据" name="evidence" multiline required/></Form></details>}
            {product && req.status==='awaiting_acceptance' && <Button disabled={busy} onClick={()=>mutate('accept-requirement',{requirement_id:req.id,version:req.version})}>验收通过，完成需求</Button>}
          </article>;
        })}
      </>}
    </div></section>;
}
