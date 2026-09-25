import { test } from 'node:test';
import assert from 'node:assert/strict';
import { subscribeTeamBoard } from './team-board-sync.js';

class Events extends EventTarget { hidden=false; }
class Source extends EventTarget {
  static instances=[];
  readyState=0; closed=false;
  constructor(url){super();this.url=url;Source.instances.push(this);}
  close(){this.closed=true;this.readyState=2;}
}
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup(load) {
  Source.instances=[];
  const document=new Events(),data=[],errors=[],timers=new Map();let timer=0;
  const sync=subscribeTeamBoard({url:'/events',load,onData:x=>data.push(x),onError:x=>errors.push(x),document,EventSource:Source,
    setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id)});
  return {sync,document,data,errors,timers};
}
test('serializes event/manual refresh and delivers the final snapshot',async()=>{
  const releases=[];let active=0,max=0;
  const f=setup(()=>new Promise(resolve=>{max=Math.max(max,++active);releases.push(x=>{active--;resolve(x);});}));
  Source.instances[0].dispatchEvent(new Event('board_changed'));
  const manual=f.sync.refresh();assert.equal(releases.length,1);
  releases[0]({revision:1});await tick();assert.equal(releases.length,2);
  releases[1]({revision:2});await manual;
  assert.equal(max,1);assert.equal(f.data.at(-1).revision,2);f.sync.close();
});
test('hidden pages close streams; resume reads fresh data; closed subscribers ignore old reads',async()=>{
  let count=0;const f=setup(async()=>++count);await tick();
  f.document.hidden=true;f.document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(Source.instances[0].closed,true);assert.equal(f.timers.size,0);
  f.document.hidden=false;f.document.dispatchEvent(new Event('visibilitychange'));await tick();
  assert.equal(Source.instances.length,2);assert.equal(count,2);
  f.sync.close();assert.equal(Source.instances[1].closed,true);
  let resolve;const pending=setup(()=>new Promise(r=>{resolve=r;}));pending.sync.close();resolve('old');await tick();assert.deepEqual(pending.data,[]);
});
test('failed streams schedule bounded fallback and changes after reconnect refresh state',async()=>{
  let count=0;const f=setup(async()=>++count);await tick();
  Source.instances[0].dispatchEvent(new Event('error'));assert.equal(f.timers.size,1);
  const callback=[...f.timers.values()][0];callback();await tick();assert.equal(count,2);
  Source.instances[0].readyState=1;Source.instances[0].dispatchEvent(new Event('open'));assert.equal(f.timers.size,0);
  Source.instances[0].dispatchEvent(new Event('board_changed'));await tick();assert.equal(count,3);f.sync.close();
});
