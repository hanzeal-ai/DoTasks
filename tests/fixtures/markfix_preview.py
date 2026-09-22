"""Isolated frontend fixture: python3 tests/fixtures/markfix_preview.py (port 5188)."""
import json, sys
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.workflow import workflow_metadata

base = dict(priority='P2', modules=['web'], auto_dispatch=0, created_at='2026-09-20T10:00:00Z', status_started_at='2026-09-20T10:00:00Z', token_budget=60000, token_used=1200, conversations=[])
tasks = [dict(base, id='TASK-FAIL', title='失败任务验证', goal='保留任务目标', status='failed', last_failure_reason='ERROR_SENTINEL <script>不执行</script>'), dict(base, id='TASK-DONE', title='完成任务验证', status='done', delivery_summary='已交付'), dict(base, id='TASK-READY', title='等待依赖验证', status='ready', dispatch_blockers=[{'message':'等待前置任务 TASK-OTHER','code':'dependency'}])]
events = [{'id':'evt-1','task_id':'TASK-FAIL','event_type':'execution_failed','created_at':'2026-09-20T10:01:00Z','payload':{'error':'ERROR_SENTINEL <script>不执行</script>'}}]
settings = {'task_token_budget':60000,'max_batch_appended_tasks':3,'parallel_development_enabled':False,'max_parallel_development':2}
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*a,**kw): super().__init__(*a,directory=str(ROOT / 'static'),**kw)
 def do_GET(self):
  path=self.path.split('?')[0]
  if not path.startswith('/api/'):
   if path=='/login': self.path='/index.html'
   return super().do_GET()
  if path=='/api/auth/status': result={'enabled':True,'authenticated':True,'username':'markfix-test-user'}
  elif path=='/api/workflow': result=workflow_metadata()
  elif path=='/api/board': result={'tasks':tasks,'requirements':[],'dispatcher':{'enabled':False},'execution_logs':events,'pending_task_changes':[]}
  elif path=='/api/settings': result=settings
  elif path=='/api/codex/projects': result={'projects':[]}
  elif path.endswith('/details'):
   task=next(t for t in tasks if t['id']==path.split('/')[3]); result={'task':task,'events':[e for e in events if e['task_id']==task['id']],'runs':[],'conversations':[],'relations':[],'reviews':[]}
  elif path=='/api/events/stream':
   self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers(); self.wfile.write(b'retry: 60000\n\n'); return
  else: result={}
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps(result).encode())
 def do_POST(self):
  value=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
  if self.path=='/api/settings': settings.update(value)
  self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(json.dumps(settings if self.path=='/api/settings' else {}).encode())
ThreadingHTTPServer(('127.0.0.1',5188),Handler).serve_forever()
