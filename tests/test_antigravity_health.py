"""Public capture diagnostics stay read-only and cannot turn tool health green."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.antigravity import AntigravityAdapter
from agent_learning.antigravity_health import capture_status
from agent_learning.runtime import Runtime, digest


class AntigravityHealthTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory(prefix="alp-ag-health-")
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.r = Runtime(self.root / "home", config={}, host="antigravity")
        self.addCleanup(self.r.close)
        self.p = self.r.project(self.workspace)
        self.c = self.r.host_conversation("chat")
        self.adapter = AntigravityAdapter(self.r, self.workspace)

    def stop(self, number=0, **fields):
        return self.adapter.handle("Stop", {"conversationId": "chat", "workspacePaths": [str(self.workspace)],
            "executionNum": number, "fullyIdle": True, "terminationReason": "model_stop", **fields})

    def test_no_stop_is_unknown_even_when_tools_or_handle_work(self):
        self.r.register_context(self.p, self.c)
        self.adapter.handle("PostToolUse", {"conversationId": "chat", "workspacePaths": [str(self.workspace)],
            "stepIdx": 1, "toolCall": {"name": "view_file", "args": {"AbsolutePath": str(self.workspace / "README.md")}}})
        view = capture_status(self.r, self.p)
        self.assertEqual(view["public_delivery"], "unknown")
        self.assertEqual(view["reason"], "not_observed")

    def test_missing_output_is_visible_instead_of_a_successful_hook(self):
        self.stop()
        view = capture_status(self.r, self.p)
        self.assertEqual(view["state"], "observed")
        self.assertEqual(view["public_delivery"], "unavailable")
        self.assertEqual(view["reason"], "public_output_missing")

    def test_empty_public_text_is_not_complete_coverage(self):
        self.stop(finalModelOutput="  \n ")
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "unavailable")

    def test_complete_output_reports_only_coverage_not_text_or_approval(self):
        self.stop(finalModelOutput="private-fixture-public-statement")
        view = capture_status(self.r, self.p)
        self.assertEqual(view["public_delivery"], "complete")
        encoded = json.dumps(view)
        self.assertNotIn("private-fixture", encoded)
        self.assertNotIn("approved", encoded)
        self.assertEqual(view["scope"], "project_recent_capture")

    def test_aborted_output_is_partial_even_when_text_is_present(self):
        self.stop(terminationReason="aborted", finalModelOutput="partial public text")
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "partial")

    def test_truncated_output_is_partial(self):
        self.stop(finalModelOutput="retained text", finalModelOutputTruncated=True)
        # Real coverage explicitly records the upstream truncation boundary.
        row = self.r.db.execute("SELECT id,payload FROM events WHERE type='AntigravityStop'").fetchone()
        payload = json.loads(row["payload"])
        payload["capture_coverage"]["finalModelOutput"].update(truncated=True, full_original_available=False)
        self.r.db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "partial")

    def test_new_missing_stop_does_not_reuse_an_older_complete_output(self):
        self.stop(finalModelOutput="old public report")
        self.stop(1)
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "unavailable")

    def test_unlinked_new_receipt_does_not_reuse_old_success(self):
        self.stop(finalModelOutput="old report")
        self.stop(1)
        row = self.r.db.execute("SELECT event_key FROM antigravity_receipts ORDER BY created DESC LIMIT 1").fetchone()
        event_id = digest([self.p, ["antigravity", self.c, row["event_key"]]])
        self.r.db.execute("DELETE FROM events WHERE id=?", (event_id,))
        self.assertEqual(capture_status(self.r, self.p)["state"], "unavailable")

    def public_response(self, conversation=None, *, state="complete", truncated=False, created=None):
        conversation = conversation or self.c
        trajectory = self.r.db.execute("SELECT generation FROM antigravity_trajectories "
            "WHERE project=? AND conversation=?", (self.p, conversation)).fetchone()
        generation = trajectory[0] if trajectory else 1
        self.r.db.execute("INSERT OR IGNORE INTO antigravity_trajectories(project,conversation) VALUES(?,?)",
                          (self.p, conversation))
        import time
        stamp = time.time() if created is None else created
        key = digest([conversation, stamp])
        self.r.db.execute("INSERT INTO antigravity_receipts(project,conversation,event_key,kind,host_epoch,"
            "generation,task_signature,state,created) VALUES(?,?,?,'PostInvocation',?,?,?,?,?)",
            (self.p, conversation, key, self.r.session_epoch(), generation, "fixture", state, stamp))
        self.r._event(self.p, conversation, "AntigravityPublicResponse", {
            "completion": "partial", "execution_completion": "unverified", "capture_coverage": {
                "workspace_association": "installed_workspace", "trajectory_identity": "current",
                "finalModelOutput": {"available": True, "truncated": truncated,
                    "full_original_available": not truncated, "source": "antigravity_transcript"}}},
            event_id=["antigravity-public-response", conversation, key])

    def test_latest_pending_stop_is_not_hidden_by_old_complete(self):
        self.stop(finalModelOutput="old report")
        self.stop(1)
        self.r.db.execute("UPDATE antigravity_receipts SET state='pending' WHERE event_key="
            "(SELECT event_key FROM antigravity_receipts ORDER BY created DESC LIMIT 1)")
        view = capture_status(self.r, self.p)
        self.assertEqual(view["state"], "pending")
        self.assertEqual(view["public_delivery"], "unknown")

    def test_public_response_is_searchable_observation_not_delivery(self):
        self.public_response()
        view = capture_status(self.r, self.p)
        self.assertEqual(view["public_response"], "available")
        self.assertEqual(view["public_delivery"], "unknown")
        self.assertEqual(view["reason"], "execution_completion_unverified")

    def test_new_public_response_does_not_reuse_older_complete_stop(self):
        self.stop(finalModelOutput="prior round")
        self.public_response()
        view = capture_status(self.r, self.p)
        self.assertEqual(view["public_delivery"], "unknown")
        self.assertEqual(view["public_response"], "available")

    def test_public_response_and_stop_of_different_chats_are_not_combined(self):
        self.public_response(self.r.host_conversation("other-chat"), truncated=True)
        self.stop(finalModelOutput="current report")
        view = capture_status(self.r, self.p)
        self.assertEqual(view["public_delivery"], "complete")
        self.assertEqual(view["public_response"], "unknown")

    def test_pending_response_cannot_reuse_old_response(self):
        self.public_response()
        self.public_response(state="pending")
        view = capture_status(self.r, self.p)
        self.assertEqual(view["state"], "pending")
        self.assertEqual(view["public_response"], "unknown")

    def test_no_tool_call_direct_completion_alias(self):
        self.stop(terminationReason="NO_TOOL_CALL", finalModelOutput="public report")
        self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "complete")

    def test_many_non_stop_receipts_do_not_expand_query_work(self):
        self.stop(finalModelOutput="report")
        epoch = self.r.session_epoch()
        with self.r.transaction():
            self.r.db.executemany("INSERT INTO antigravity_receipts(project,conversation,event_key,kind,"
                "host_epoch,generation,task_signature,state,created) VALUES(?,?,?,'PostToolUse',?,1,'','complete',?)",
                ((self.p, self.c, f"fixture-{i}", epoch, i) for i in range(50000)))
        ticks = []
        self.r.db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
        try:
            view = capture_status(self.r, self.p)
        finally:
            self.r.db.set_progress_handler(None, 0)
        self.assertEqual(view["public_delivery"], "complete")
        self.assertLess(len(ticks) * 100, 10000)

    def test_missing_optional_index_is_unknown_without_writes(self):
        self.stop(finalModelOutput="report")
        self.r.db.execute("DROP INDEX antigravity_receipt_recency")
        changes = self.r.db.total_changes
        self.assertEqual(capture_status(self.r, self.p)["reason"], "query_unavailable")
        self.assertEqual(self.r.db.total_changes, changes)

    def test_exhausted_candidate_window_does_not_scan_for_old_success(self):
        self.stop(finalModelOutput="old current instance")
        import time
        stamp = time.time()
        with self.r.transaction():
            self.r.db.executemany("INSERT INTO antigravity_receipts(project,conversation,event_key,kind,"
                "host_epoch,generation,task_signature,state,created) VALUES(?,?,?,'Stop',?,1,'','complete',?)",
                ((self.p, "antigravity:other:chat", f"other-{i}", self.r.session_epoch(), stamp + i)
                 for i in range(33)))
        self.assertEqual(capture_status(self.r, self.p)["state"], "unobserved")

    def test_old_host_epoch_cannot_look_current(self):
        self.stop(finalModelOutput="old login report")
        self.r.account_changed("new-fixture-account")
        self.assertEqual(capture_status(self.r, self.p)["state"], "unobserved")

    def test_reset_or_ambiguous_trajectory_cannot_look_current(self):
        self.stop(finalModelOutput="old trajectory report")
        self.r.db.execute("UPDATE antigravity_trajectories SET generation=generation+1 WHERE project=?", (self.p,))
        self.assertEqual(capture_status(self.r, self.p)["state"], "unobserved")
        self.r.db.execute("UPDATE antigravity_trajectories SET generation=generation-1,ambiguous=1 WHERE project=?", (self.p,))
        self.assertEqual(capture_status(self.r, self.p)["state"], "unobserved")

    def test_other_project_or_host_instance_cannot_supply_coverage(self):
        self.stop(finalModelOutput="one project report")
        other_project = self.r.project(self.root / "other")
        self.assertEqual(capture_status(self.r, other_project)["state"], "unobserved")
        other = Runtime(self.root / "home", config={}, host="antigravity", host_instance="other")
        self.addCleanup(other.close)
        self.assertEqual(capture_status(other, self.p)["state"], "unobserved")

    def test_diagnostics_do_not_open_files_launch_processes_or_write_state(self):
        self.stop(finalModelOutput="report")
        changes = self.r.db.total_changes
        with patch.object(Path, "open", side_effect=AssertionError("no transcript re-read")), \
                patch.object(subprocess, "run", side_effect=AssertionError("no CLI probe")):
            self.assertEqual(capture_status(self.r, self.p)["public_delivery"], "complete")
        self.assertEqual(self.r.db.total_changes, changes)

    def test_bad_optional_coverage_does_not_break_status_or_echo_data(self):
        self.stop()
        self.r.db.execute("UPDATE events SET payload='not-json-private' WHERE type='AntigravityStop'")
        view = capture_status(self.r, self.p)
        self.assertEqual(view["reason"], "query_unavailable")
        self.assertNotIn("private", json.dumps(view))

    def test_runtime_status_exposes_the_same_safe_observation(self):
        self.stop()
        changes = self.r.db.total_changes
        with patch.object(self.r, "_launch_admin", side_effect=AssertionError("no administrator")):
            view = self.r.status(self.p)["host_capture"]
        self.assertEqual(view["public_delivery"], "unavailable")
        self.assertEqual(self.r.db.total_changes, changes)


@unittest.skipUnless(shutil.which("node"), "Node.js required for the native renderer")
class AntigravityHealthRenderingTests(unittest.TestCase):
    def test_localized_capture_and_executor_layers_with_old_server_compatibility(self):
        html = (Path(__file__).resolve().parents[1] / "host_resources/antigravity/sidecars/control/index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>([\s\S]*)</script>", html).group(1)
        harness = r"""
const {script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm'),assert=require('node:assert/strict'),nodes=new Map();
const element=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',hidden:false,children:[],listeners:{},dataset:{},replaceChildren(){this.children=[];},append(...v){this.children.push(...v);},addEventListener(k,v){this.listeners[k]=v;}});return nodes.get(id);};
const window={parent:{},addEventListener(){}},document={documentElement:{},getElementById:element,createElement:()=>({dataset:{},addEventListener(){},append(){}}),querySelectorAll:()=>[]};
const at=script.indexOf('  render(); hide("waiting");');assert(at>0);
const context={window,document,navigator:{language:'en'}};vm.createContext(context);
vm.runInContext(script.slice(0,at)+'\n globalThis.show=(s)=>{summary=s;capability="cap";render();};})();',context);
for(const language of ['en','zh-CN']){
 const local=()=>{element('language').value=language;element('language').listeners.change();return element('admin').textContent;};
 const base={project_id:'p',host_scope:'antigravity:default',counts:{}};
 context.show(base);assert(!local().includes('undefined'));assert(!local().includes(language==='en'?'Recent public text capture':'近期公开文本采集'));
 for(const [state,label] of [['complete',language==='en'?'Complete public text captured':'完整公开文本已采集'],['partial',language==='en'?'Only partial public text':'仅采集到部分公开文本'],['unavailable',language==='en'?'Public delivery text was unavailable':'未取得公开交付文本']]){
  context.show({...base,host_capture:{version:1,host_scope:base.host_scope,scope:'project_recent_capture',state:'observed',public_delivery:state,privateText:'must-not-render'}});
  const text=local();assert(text.includes(label));assert(!text.includes('must-not-render'));
 }
 context.show({...base,host_capture:{version:1,host_scope:'antigravity:other',scope:'project_recent_capture',public_delivery:'complete'}});assert(!local().includes(language==='en'?'Complete public text captured':'完整公开文本已采集'));
 context.show({...base,host_capture:{version:1,host_scope:base.host_scope,scope:'project_recent_capture',state:'observed',public_response:'available',public_delivery:'unknown'}});assert(local().includes(language==='en'?'A public response was retained':'公开回复已保留'));
 context.show({...base,host_capture:{version:1,host_scope:base.host_scope,scope:'project_recent_capture',state:'pending',public_response:'available',public_delivery:'complete'}});assert(!local().includes(language==='en'?'Complete public text captured':'完整公开文本已采集'));
 for(const [view,label] of [[{launcher_available:false},language==='en'?'standalone agy CLI was not found':'未找到独立 agy CLI'],[{launcher_available:true,capability_checked:false},language==='en'?'protocol not yet checked':'协议尚未验证'],[{launcher_available:true,capability_checked:true,capability_ready:false},language==='en'?'has not passed validation':'尚未通过验证'],[{launcher_available:true,capability_checked:true,capability_ready:true},language==='en'?'platform login and model availability remain unverified':'平台登录和模型可用性仍未验证']]){
  context.show({...base,administrator_readiness:{...view,provider:'antigravity'}});assert(local().includes(label));
 }
 context.show({...base,administrator_readiness:{provider:'codex',launcher_available:false}});assert(!local().includes(language==='en'?'standalone agy CLI':'独立 agy CLI'));
}
"""
        result = subprocess.run([shutil.which("node"), "-e", harness], input=json.dumps({"script": script}),
                                text=True, encoding="utf-8", capture_output=True, timeout=15, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
