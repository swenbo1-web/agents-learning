"""Saved health reports stay project scoped, bounded and read only."""
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_learning.browser_ui import HTML, JAVASCRIPT
from agent_learning.knowledge_browser import make_browser_server
from agent_learning.knowledge_health import advance
from agent_learning.runtime import Runtime
from tests.test_browser_reader_ui import HARNESS as READER_HARNESS


class KnowledgeHealthBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="alp-health-browser-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.runtime = Runtime(root / "home", config={})
        self.addCleanup(self.runtime.close)
        self.project = self.runtime.project(root / "project")
        self.other = self.runtime.project(root / "other")
        for project, title in ((self.project, "Needs applicability"), (self.other, "Other private project")):
            body = {"title": title, "content": "An old project observation", "category": "unknown",
                    "knowledge_kind": "fact", "validation_level": "reported", "limitations": ""}
            self.runtime.db.execute("INSERT INTO knowledge VALUES(?,?,1,?,1)", (project, "legacy", json.dumps(body)))
            self.runtime.db.execute("INSERT INTO active VALUES(?,?,1)", (project, "legacy"))
            self.runtime.db.execute("DELETE FROM knowledge_health_items WHERE project=?", (project,))
            self.runtime.db.execute("DELETE FROM knowledge_health_runs WHERE project=?", (project,))
            advance(self.runtime, project, max_seconds=2)
        self.server = make_browser_server(root / "home" / "runtime.sqlite3", self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, *, authenticated=True, method="GET"):
        headers = {"Authorization": "Bearer " + self.server.access_token} if authenticated else {}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_report_is_project_scoped_and_does_not_reopen_or_advance_runtime(self):
        before = list(self.runtime.db.iterdump())
        with patch("agent_learning.knowledge_health.advance", side_effect=AssertionError("must not scan")), \
                patch("agent_learning.runtime.Runtime.__init__", side_effect=AssertionError("must not open")):
            status, data = self.request("/api/knowledge-health")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["project_id"], self.project)
        self.assertEqual(data["state"], "completed")
        self.assertEqual(data["scanned"], 1)
        self.assertEqual(data["items"][0]["title"], "Needs applicability")
        self.assertIn("applicability_unspecified", data["items"][0]["issues"])
        self.assertNotIn("Other private project", json.dumps(data))
        self.assertFalse(data["semantic_reclassified"])
        self.assertEqual(list(self.runtime.db.iterdump()), before)

    def test_authentication_page_bounds_and_write_denial(self):
        self.assertEqual(self.request("/api/knowledge-health", authenticated=False)[0], 401)
        for query in ("limit=51", "limit=-1", "offset=10001", "project=" + self.other,
                      "offset=0&offset=1", "limit=one"):
            with self.subTest(query=query):
                self.assertEqual(self.request("/api/knowledge-health?" + query)[0], 400)
        status, data = self.request("/api/knowledge-health?limit=0")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["affected"], 1)
        self.assertEqual(self.request("/api/knowledge-health", method="DELETE")[0], 405)

    def test_old_database_does_not_get_initialized_by_viewing(self):
        self.runtime.db.execute("DROP TABLE knowledge_health_items")
        self.runtime.db.execute("DROP TABLE knowledge_health_runs")
        before = list(self.runtime.db.iterdump())
        status, data = self.request("/api/knowledge-health")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["state"], "not_started")
        self.assertEqual(data["items"], [])
        self.assertEqual(list(self.runtime.db.iterdump()), before)

    def test_changed_project_and_revision_keep_snapshot_explicit(self):
        self.runtime.db.execute("INSERT INTO knowledge VALUES(?,?,2,?,2)",
                                (self.project, "legacy", json.dumps({"title": "Corrected"})))
        self.runtime.db.execute("UPDATE active SET revision=2 WHERE project=? AND id='legacy'", (self.project,))
        self.runtime.db.execute("UPDATE projects SET generation=generation+1 WHERE id=?", (self.project,))
        status, data = self.request("/api/knowledge-health")
        self.assertEqual(status, 200, data)
        self.assertFalse(data["snapshot_current"])
        self.assertEqual(data["items"][0]["revision"], 1)
        self.assertFalse(data["items"][0]["current"])


HEALTH_UI_HARNESS = READER_HARNESS.split("(async()=>{")[0] + r'''
const healthCalls=[],text=element=>element.textContent+' '+element.children.map(child=>typeof child==='object'?text(child):String(child)).join(' ');
let failHealth=false,healthGate=null;
context.fetch=async(url,options={})=>{
 healthCalls.push({url,options});if(healthGate)await healthGate;
 if(failHealth)return {ok:false,status:503,json:async()=>({reason:'query_budget_exceeded',error:'query budget exceeded'})};
 if(url==='/api/stats')return {ok:true,json:async()=>({project_id:'test',knowledge_active:1})};
 assert(url.startsWith('/api/knowledge-health'));
 const offset=Number(new URL(url,'http://local').searchParams.get('offset'));
 return {ok:true,json:async()=>({project_id:'test',state:'completed',scanned:21,affected:21,rule_version:1,
  snapshot_current:false,issue_counts:{category_unknown:21},next_offset:offset===0?20:null,
  items:[{knowledge_id:'legacy',revision:1,title:offset===0?'First finding':'Last finding',current:offset!==0,issues:['category_unknown','unresolved_source_failure'],checked:1}]})};
};
run("state.session={project:'test'};state.token='read';state.view='knowledge'");
(async()=>{
 assert.equal(healthCalls.length,0);await run('loadStats()');assert.equal(healthCalls.length,1);
 assert.equal(healthCalls.filter(call=>call.url.includes('knowledge-health')).length,0);
 await run('loadHealth()');assert(text(get('health-result')).includes('First finding'));
 assert(text(get('health-result')).includes('not a live status'));
 assert(text(get('health-result')).includes('unknown'));
 run("state.language='zh-CN';staticLanguage();renderHealth()");
 assert(text(get('health-result')).includes('分类仍待明确'));
 assert(text(get('health-result')).includes('未恢复的失败'));
 assert(text(get('health-result')).includes('不是实时状态'));
 assert(!text(get('health-result')).includes('Category remains unknown'));
 await run('loadHealth(20)');assert(text(get('health-result')).includes('Last finding'));
 assert.equal(get('health-next').disabled,true);assert.equal(get('health-previous').disabled,false);
 failHealth=true;await run('loadHealth(20)');assert(text(get('health-result')).includes('Last finding'));
 assert(get('health-error').textContent.includes('查询超限'));
 failHealth=false;let release;healthGate=new Promise(resolve=>{release=resolve;});
 const pending=run('loadHealth()');run('disconnect()');release();await pending;await flush();
 assert.equal(run('healthView.data'),null);assert.equal(get('knowledge-health').open,false);
 assert(!text(get('health-result')).includes('Last finding'));
 assert.equal(healthCalls.filter(call=>call.options.method==='POST').length,0);
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


class KnowledgeHealthBrowserUITests(unittest.TestCase):
    def test_template_explains_scope_and_has_no_eager_stats_query(self):
        self.assertIn('<details id="knowledge-health"', HTML)
        self.assertIn('Missing fields are suggestions', HTML)
        stats_function = JAVASCRIPT.split("async function loadStats(){", 1)[1].split("\n", 1)[0]
        self.assertNotIn("loadHealth", stats_function)
        self.assertIn("textContent", JAVASCRIPT)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the shipped UI")
    def test_on_demand_localization_paging_failure_and_disconnect(self):
        result = subprocess.run([shutil.which("node"), "-e", HEALTH_UI_HARNESS],
                                input=json.dumps({"script": JAVASCRIPT, "scenario": "health"}),
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)
