import subprocess,json,re
from pathlib import Path
from datetime import datetime,timezone,timedelta

def out(*args):return subprocess.check_output(args,text=True,timeout=30)
ids=out('docker','image','ls','-q','--no-trunc').split()
images=json.loads(out('docker','image','inspect',*sorted(set(ids))))
containers=out('docker','ps','-aq').split()
protected=set()
if containers:
 protected.update(x['Image'] for x in json.loads(out('docker','inspect',*containers)))
references=set()
for root in ['/home/admin/dotasks','/home/admin/markfix','/var/lib/hanzeal-ops']:
 for path in Path(root).rglob('*'):
  if not path.is_file() or path.is_symlink() or path.stat().st_size>131072:continue
  if path.name == 'image-cleanup-plan.json':continue
  text=path.read_text(errors='replace')
  references.update(re.findall(r'sha256:[a-f0-9]{64}',text))
  references.update(re.findall(r'(?:DOTASKS_IMAGE|API_IMAGE|DASHBOARD_IMAGE)=([^\s]+)',text))
for item in images:
 tags=(item.get('RepoTags') or [])+(item.get('RepoDigests') or [])
 if item['Id'] in references or any(tag in references for tag in tags):protected.add(item['Id'])
# Preserve at least the latest two images of every repository, plus all references.
by_repo={}
for item in sorted(images,key=lambda x:x['Created'],reverse=True):
 for tag in item.get('RepoTags') or []:
  repo=tag.rsplit(':',1)[0]
  values=by_repo.setdefault(repo,[])
  if item['Id'] not in values:values.append(item['Id'])
for values in by_repo.values():protected.update(values[:2])
cutoff=datetime.now(timezone.utc)-timedelta(days=7)
candidates=[]
for item in images:
 created=datetime.fromisoformat(item['Created'].replace('Z','+00:00'))
 tags=item.get('RepoTags') or []
 if item['Id'] not in protected and created<cutoff and tags and all(t.rsplit(':',1)[0] in {'ghcr.io/hanzeal-ai/dotasks','dotasks-cloud'} for t in tags):
  candidates.append({'id':item['Id'],'tags':tags,'created':item['Created'],'size':item['Size']})
plan={'protected_count':len(protected),'candidate_count':len(candidates),'candidates':candidates}
Path('/var/lib/hanzeal-ops/image-cleanup-plan.json').write_text(json.dumps(plan,indent=2))
print(json.dumps(plan,indent=2))
