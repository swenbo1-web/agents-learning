"""Run the shipped detail, approval and receipt UI without models or a user database."""
import json
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import HTML, JAVASCRIPT


HARNESS = r'''
const {script,scenario}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict');
const elements=new Map(),all=[],requests=[];let context,serial=0,published=false,failRefresh=false,failDetail=false;
class Element{
 constructor(id,tag='div'){this.id=id;this.tagName=tag.toUpperCase();this.children=[];this.textContent='';this.value='';this.hidden=false;this.disabled=false;this.open=false;this.scrollTop=0;this.dataset={};this.listeners={};this.isConnected=true;this.parentElement=null;this.classes=new Set();this.classList={toggle:(key,value)=>{value??=!this.classes.has(key);value?this.classes.add(key):this.classes.delete(key);},contains:key=>this.classes.has(key)};all.push(this);}
 addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);} setAttribute(name,value){this[name]=value;}
 append(...items){for(const item of items){if(item&&typeof item==='object'){if(item.parentElement)item.parentElement.children=item.parentElement.children.filter(x=>x!==item);item.parentElement=this;item.isConnected=this.isConnected;}this.children.push(item);}}
 replaceChildren(...items){for(const child of this.children)if(child&&typeof child==='object'){child.parentElement=null;child.isConnected=false;}this.children=[];this.append(...items);}
 focus(){context.document.activeElement=this;} showModal(){this.open=true;this.focus();}
 close(){if(!this.open)return;this.open=false;queueMicrotask(()=>{for(const fn of this.listeners.close||[])fn({});});}
 cancel(){let prevented=false;for(const fn of this.listeners.cancel||[])fn({preventDefault:()=>{prevented=true;}});if(!prevented)this.close();return prevented;}
}
const get=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
const queries=selector=>selector==='[data-detail-key]'?all.filter(x=>x.isConnected&&x.dataset.detailKey):selector==='[data-candidate-check]'?all.filter(x=>x.isConnected&&x.dataset.candidateCheck):[];
const win={scrollX:0,scrollY:1480,scrollTo:options=>{win.scrollX=options.left;win.scrollY=options.top;}};
const candidate=()=>({candidate_id:'c1',status:published?'published':'candidate',body:{title:'Review content',content:'Source',metadata:{}},problem:{id:'p1',revision:1,summary:'Observed source'},field:{text:'Content page',offset:0,total_chars:10000,next_offset:8000},evidence:{items:[],total:0,offset:0,next_offset:null},preflight:{publishable:true,issues:[],warnings:[]}});
const fetch=async(url,options)=>{
 requests.push({url,options});let data;
 if(url.startsWith('/api/candidate?')){if(failDetail)throw new Error('offline detail');data=candidate();}
 else if(url.startsWith('/api/candidates?')){if(failRefresh)throw new Error('offline refresh');data={items:[],total:0,offset:20,next_offset:null};}
 else if(url==='/api/stats')data={project_id:'test',knowledge_active:1,workload:{stages:{pending_review:0}}};
 else if(url.startsWith('/api/review/prepare')){const body=JSON.parse(options.body);data={plan_id:'plan',expires_at:Date.now()/1000+3600,plan:{plan_digest:'digest',action:body.action,ready:true,items:body.candidate_ids.map(id=>({candidate_id:id,title:id,status:'candidate',preflight:{publishable:true,issues:[],warnings:[]}}))}};}
 else if(url.startsWith('/api/review/commit')){published=true;data={request_id:JSON.parse(options.body).request_id,action:'approve',status:'committed',results:[{candidate_id:'c1',status:'published',knowledge_id:'knowledge',revision:1}]};}
 else if(url.startsWith('/api/review-receipt'))data={found:false};
 else if(url.startsWith('/api/revisions')||url.startsWith('/api/actions')||url.startsWith('/api/events'))data={items:[],total:0,next_offset:null};
 else throw new Error('unexpected route '+url);
 return {ok:true,json:async()=>data};
};
context=vm.createContext({window:win,document:{getElementById:get,createElement:tag=>new Element('node-'+ ++serial,tag),querySelectorAll:queries,activeElement:get('opener'),documentElement:new Element('html'),body:{setAttribute(){}},addEventListener(){}},navigator:{language:'en'},location:{hash:''},URLSearchParams,crypto:{randomUUID:()=> 'request-'+ ++serial},fetch,setTimeout(){},clearTimeout(){},console});
vm.runInContext(script,context);
get('receipt-dialog').append(get('receipt'));get('receipt').hidden=true;
const run=code=>vm.runInContext(code,context),flush=()=>new Promise(resolve=>setImmediate(resolve));
run(`state.view='candidates';state.session={review_enabled:true};state.reviewToken='review';state.token='read';state.offset=20;state.selected.set('c1',{});state.selected.set('c2',{});`);
const opener=get('opener');opener.dataset.detailKey='candidate:c1';
(async()=>{
 if(scenario==='detail'||scenario==='cancel'||scenario==='disconnect'||scenario==='single'||scenario==='refresh_failure'||scenario==='rapid_reopen'){
  await run(`showDetail('candidate','c1',undefined,document.getElementById('opener'))`);
  assert.equal(get('detail-dialog').open,true);assert.equal(win.scrollY,1480);
  assert.equal(context.document.documentElement.classList.contains('reading-open'),true);
 }
 if(scenario==='detail'){
  get('detail-dialog').scrollTop=340;await run('state.fieldOffset=8000;loadField()');
  assert.equal(get('detail-dialog').scrollTop,340);assert.equal(win.scrollY,1480);
  await run('state.evidenceOffset=10;loadField()');assert.equal(get('detail-dialog').scrollTop,340);
  run('closeDetail()');await flush();assert.equal(get('detail-dialog').open,false);assert.equal(win.scrollY,1480);
  assert.equal(context.document.activeElement,opener);assert.equal(run('state.offset'),20);assert.equal(run('state.selected.size'),2);
  assert.equal(requests.filter(x=>x.options.method==='POST').length,0);
 }else if(scenario==='rapid_reopen'){
  run("closeDetail();globalThis.reopened=showDetail('candidate','c1',undefined,document.getElementById('opener'))");
  await run('reopened');await flush();assert.equal(get('detail-dialog').open,true);assert.equal(run('state.detail.id'),'c1');
  assert.equal(win.scrollY,1480);run('closeDetail()');
 }else if(scenario==='cancel'){
  run('state.busy=true');assert.equal(get('detail-dialog').cancel(),true);assert.equal(get('detail-dialog').open,true);
  run('state.busy=false');assert.equal(get('detail-dialog').cancel(),false);await flush();
  assert.equal(run('state.detail'),null);assert.equal(win.scrollY,1480);assert.equal(context.document.documentElement.classList.contains('reading-open'),false);
 }else if(scenario==='single'||scenario==='refresh_failure'){
  get('detail-dialog').scrollTop=310;await run(`prepare(['c1'],'approve')`);assert.equal(get('review-dialog').open,true);
  if(scenario==='refresh_failure')failRefresh=true;
  await run('run(commit)');await flush();
  assert.equal(requests.filter(x=>x.url==='/api/review/commit').length,1);
  assert.equal(get('review-dialog').open,false);assert.equal(get('detail-dialog').open,true);
  assert.equal(get('receipt').parentElement,get('detail-receipt-host'));assert.equal(get('receipt-dialog').open,false);
  assert.equal(get('receipt').hidden,false);assert.equal(win.scrollY,1480);
  assert.equal(run("state.selected.has('c2')"),true);assert.equal(run("state.selected.has('c1')"),false);
  assert.equal(get('view-receipt').hidden,false);
  if(scenario==='refresh_failure')assert(get('detail-error').textContent.includes('offline')||get('detail-error').textContent.length>0);
  else assert.equal(run('state.detailData.status'),'published');
  run('closeReceipt();closeDetail()');await flush();assert.equal(win.scrollY,1480);
 }else if(scenario==='batch'){
  await run(`prepare(['c1'],'approve',document.getElementById('opener'))`);await run('run(commit)');await flush();
  assert.equal(get('receipt-dialog').open,true);assert.equal(win.scrollY,1480);
  run('closeReceipt()');await flush();assert.equal(win.scrollY,1480);assert.equal(context.document.activeElement,opener);
  assert.equal(context.document.documentElement.classList.contains('reading-open'),false);
  run('showReceipt()');assert.equal(get('receipt-dialog').open,true);run('closeReceipt()');
 }else if(scenario==='disconnect'){
  run('state.receipt={action:"approve",results:[]};showReceipt();disconnect()');await flush();
  assert.equal(run('state.detail'),null);assert.equal(run('state.receipt'),null);assert.equal(get('detail-dialog').open,false);
  assert.equal(get('receipt-dialog').open,false);assert.equal(get('receipt').hidden,true);assert.equal(get('view-receipt').hidden,true);
  assert.equal(context.document.documentElement.classList.contains('reading-open'),false);
 }else if(scenario==='detail_failure'){
  failDetail=true;await run(`run(()=>showDetail('candidate','c1',undefined,document.getElementById('opener')))`);
  assert.equal(get('detail-dialog').open,false);assert.equal(run('state.detail'),null);assert.equal(win.scrollY,1480);
 }else if(scenario==='unknown'){
  await run(`prepare(['c1'],'approve',document.getElementById('opener'))`);
  const request=run('state.planRequest');run('state.uncertain=true;closePlan()');await flush();
  assert.equal(run('state.planRequest'),request);assert(run('state.plan'));
  await run(`prepare(['c2'],'reject',document.getElementById('opener'))`);
  assert.equal(run('state.planRequest'),request);assert.equal(requests.filter(x=>x.url==='/api/review/prepare').length,1);
 }
 run("state.language='zh-CN';staticLanguage()");assert.equal(get('language').value,'zh-CN');
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class BrowserReaderUITests(unittest.TestCase):
    def test_template_has_named_dialogs_and_no_forced_bottom_scroll(self):
        self.assertIn('<dialog id="detail-dialog" aria-labelledby="detail-title">', HTML)
        self.assertIn('<dialog id="receipt-dialog" aria-labelledby="receipt-heading">', HTML)
        self.assertNotIn("scrollIntoView", JAVASCRIPT)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the shipped UI")
    def test_reading_approval_recovery_and_scroll_preservation(self):
        for scenario in ("detail", "cancel", "single", "batch", "refresh_failure", "disconnect", "detail_failure", "unknown", "rapid_reopen"):
            with self.subTest(scenario=scenario):
                result = subprocess.run([shutil.which("node"), "-e", HARNESS],
                                        input=json.dumps({"script": JAVASCRIPT, "scenario": scenario}),
                                        capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ok", result.stdout)
