#!/usr/bin/env python3
"""Explicit live Codex analysis smoke test against disposable fixture data.

Uses the installed Codex and its configured model/account. It can consume model
usage; it does not start a development run or alter a real project.
"""
import json
from pathlib import Path
import tempfile
import time
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from taskboard.team_local import ReadOnlyAnalysisClient


def main():
    with tempfile.TemporaryDirectory(prefix='dotasks-analysis-check-') as directory:
        root=Path(directory);project=root/'project';project.mkdir()
        source=project/'feature.py';source.write_text('def feature(): return False\n')
        client=ReadOnlyAnalysisClient(root/'worker',Path(__file__).resolve().parents[1])
        job={'kind':'requirement_analysis','requirement':{'title':'新增问候接口','original_content':'新增 greeting(name)，返回 Hello name。仅新增接口，不改部署。'},'task':None,'questions':[],'token_budget':20000}
        started=time.monotonic()
        try:
            _,result=client.analyze(str(project),job,root/'receipt.json')
            unchanged=source.read_text()=='def feature(): return False\n' and sorted(p.name for p in project.iterdir())==['feature.py']
            if not unchanged:raise RuntimeError('Analysis changed its fixture project')
            print(json.dumps({'sandbox_probe':'passed','real_codex_analysis':'passed','project_unchanged':unchanged,'elapsed_seconds':round(time.monotonic()-started,2),'result':result},ensure_ascii=False,indent=2))
        finally:client.stop()


if __name__=='__main__':main()
