import { useState } from 'react';
import { Button } from './components/ui/button';
import { Input } from './components/ui/input';
import { NativeSelect } from './components/ui/native-select';

function SourceProject({project, source, board, mutate, busy}) {
  const [url,setUrl]=useState(source?.url || '');
  const [enabled,setEnabled]=useState(source ? Boolean(source.enabled) : true);
  const coordinators=board.members.filter(m=>['admin','developer','coordinator'].includes(m.role));
  const [coordinator,setCoordinator]=useState(source?.coordinator_id || coordinators[0]?.id || '');
  const [result,setResult]=useState('');
  return <section className="team-task"><h3>{project.name}</h3>
    {board.role==='admin' ? <form className="team-form" onSubmit={async e=>{
      e.preventDefault();setResult('');
      await mutate('save-task-source',{project_id:project.id,url,coordinator_id:coordinator,enabled,version:source?.version || 0});
    }}>
      <label className="team-field">来源授权地址<Input type="url" required autoComplete="off" maxLength={6500} value={url} placeholder="粘贴来源生成的完整授权地址" onChange={e=>setUrl(e.target.value)}/></label>
      <label className="team-field">开发协调人<NativeSelect value={coordinator} onChange={e=>setCoordinator(e.target.value)}>{coordinators.map(m=><option key={m.id} value={m.id}>{m.username}</option>)}</NativeSelect></label>
      <label className="team-source-toggle"><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)}/> 启用来源</label>
      <Button disabled={busy || !coordinator}>保存来源</Button>
    </form> : <p>{source ? '已配置任务来源' : '请管理员配置任务来源'}</p>}
    {Boolean(source?.has_token) && <p>已保存读取授权。</p>}
    {Boolean(source?.enabled) && <Button className="mt-3" variant="outline" disabled={busy} onClick={async()=>{
      setResult('正在拉取…');const r=await mutate('pull-task-source',{project_id:project.id,version:source.version});
      setResult(r?`已新增 ${r.created} 条需求，跳过 ${r.skipped} 条已有记录。`:'拉取未完成，请查看上方提示后重试。');
    }}>拉取需求</Button>}
    {result && <p role="status">{result}</p>}
  </section>;
}

export function TaskSources({board,mutate,busy}) {
  if(!['admin','product'].includes(board.role) || !board.projects.length)return null;
  return <details className="team-panel"><summary>任务来源</summary>
    <p>从已配置的地址拉取新记录，保存为待分析需求；已有记录不会重复创建或覆盖。</p>
    <div className="team-grid">{board.projects.map(project=>{
      const source=board.task_sources?.find(s=>s.project_id===project.id);
      return <SourceProject key={project.id+':'+(source?.version || 0)} project={project} source={source} board={board} mutate={mutate} busy={busy}/>;
    })}</div>
  </details>;
}
