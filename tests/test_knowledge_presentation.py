"""Offline behavior and accessibility contracts for both knowledge surfaces.

These are unit tests of the shipped resources, not screenshots or live-browser
visual acceptance. All HTTP/model boundaries are fixtures or remain read-only.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from agent_learning.browser_ui import CSS, HTML, JAVASCRIPT
from agent_learning.browser_theme import BROWSER_THEME_CSS
from tests.test_browser_reader_ui import HARNESS as READER_HARNESS
from tests.test_dsh_panel_status_ui import HARNESS as DSH_HARNESS
from tests.test_dsh_composer_scope import HARNESS as COMPOSER_HARNESS


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "host_resources" / "dsh" / "client" / "index.js"
NODE = shutil.which("node")


class _Document(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = set()
        self.fields = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "label" and attrs.get("for"):
            self.labels.add(attrs["for"])
        if tag in {"input", "select", "textarea"}:
            self.fields.add(attrs.get("id"))


class KnowledgePresentationTests(unittest.TestCase):
    def test_static_document_keeps_unique_ids_and_visible_filter_labels(self):
        doc = _Document()
        doc.feed(HTML)
        self.assertEqual(len(doc.ids), len(set(doc.ids)))
        for name in ("query", "state", "category"):
            self.assertIn(name, doc.labels)
            self.assertIn(name, doc.fields)
        self.assertIn('aria-labelledby="view-heading"', HTML)
        self.assertIn('id="clear-filters"', HTML)
        self.assertIn('id="health-state"', HTML)

    def test_theme_includes_dark_small_screen_focus_and_reduced_motion_styles(self):
        for fragment in ("prefers-color-scheme:dark", "prefers-reduced-motion:reduce",
                         "max-width:650px", ":focus-visible", "[hidden]", "--al-surface",
                         "#receipt-dialog .receipt-heading", ".health-stale"):
            self.assertIn(fragment, BROWSER_THEME_CSS)
        self.assertIn(BROWSER_THEME_CSS, CSS)
        self.assertNotIn("@import", BROWSER_THEME_CSS)
        self.assertNotIn("url(", BROWSER_THEME_CSS)

    def test_standalone_theme_text_pairs_meet_normal_text_contrast(self):
        def luminance(color):
            parts = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            parts = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                     for value in parts]
            return sum(a * b for a, b in zip(parts, (0.2126, 0.7152, 0.0722)))

        def ratio(a, b):
            light, dark = sorted((luminance(a), luminance(b)), reverse=True)
            return (light + 0.05) / (dark + 0.05)

        # Color values are tested, not claims about an unavailable rendered page.
        for foreground, background in (("#1c283b", "#f5f7fa"), ("#52627a", "#ffffff"),
                                       ("#ffffff", "#235bc5"), ("#e4eaf3", "#12171f"),
                                       ("#a6b5ca", "#1a212c"), ("#11234a", "#9cbdff")):
            with self.subTest(foreground=foreground, background=background):
                self.assertGreaterEqual(ratio(foreground, background), 4.5)

    def run_browser(self, scenario):
        if not NODE:
            self.skipTest("Node.js required for shipped browser resource")
        harness = READER_HARNESS.replace("(async()=>{", "(async()=>{\n" + scenario, 1)
        result = subprocess.run([NODE, "-e", harness],
                                input=json.dumps({"script": JAVASCRIPT, "scenario": "presentation"}),
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ok", result.stdout)

    def test_missing_stats_are_unknown_and_actual_zero_stays_zero(self):
        self.run_browser(r"""
 run("state.statsData={project_id:'test',project_name:'Example',project_path:'/safe/project',knowledge_active:0};renderStats()");
 const cards=get('stats').children;
 assert.equal(cards[0].children[1].textContent,'0');
 assert.equal(cards[1].children[1].textContent,'Unknown');
 assert.equal(cards[2].children[1].textContent,'Unknown');
 assert.equal(cards[3].children[1].textContent,'Unknown');
 assert.equal(get('project').textContent,'Example');
 assert(get('project-context').textContent.includes('/safe/project'));
 assert.equal(requests.length,0,'rendering must not query the queue or health report');
""")

    def test_empty_search_and_view_hints_follow_filters_and_language(self):
        self.run_browser(r"""
 run("state.view='knowledge';state.listData={items:[],total:0,offset:0,next_offset:null};document.getElementById('state').value='active';renderList()");
 assert.equal(get('list').children[0].children[0].textContent,'No records in this view');
 get('query').value='ERR_EXAMPLE';run('renderList()');
 assert.equal(get('list').children[0].children[0].textContent,'No matching knowledge');
 run("state.language='zh-CN';renderList()");
 assert.equal(get('view-heading').textContent,run("t('knowledge')"));
 assert.equal(get('list').children[0].children[0].textContent,'没有匹配的知识');
 assert.equal(requests.length,0);
""")

    def test_stale_health_is_visible_even_when_saved_pass_is_completed(self):
        self.run_browser(r"""
 run("healthView.data={state:'completed',scanned:19,affected:1,rule_version:1,expected_rule_version:1,snapshot_current:false,items:[],next_offset:null};renderHealth()");
 assert.equal(get('health-state').textContent,'Snapshot out of date');
 assert.equal(get('health-state')['data-tone'],'warning');
 assert(get('health-result').children.some(x=>x.className==='health-stale'));
 run("state.language='zh-CN';renderHealth()");
 assert.equal(get('health-state').textContent,'体检快照已过期');
 run('resetHealth()');assert.equal(get('health-state').textContent,'按需读取');
 assert.equal(requests.length,0,'a saved report must not be recomputed on render');
""")

    def test_clear_filters_is_a_read_operation_and_returns_to_first_page(self):
        scenario = r"""
 run("state.view='knowledge';state.offset=40;document.getElementById('query').value='narrow';document.getElementById('category').value='project';document.getElementById('state').value='revoked'");
 await get('clear-filters').listeners.click[0]();
 assert.equal(get('query').value,'');assert.equal(get('category').value,'');assert.equal(get('state').value,'active');
 assert.equal(run('state.offset'),0);
 assert.equal(requests.length,1);assert(requests[0].url.startsWith('/api/knowledge-list?'));
 assert(!requests[0].options.method||requests[0].options.method==='GET');
"""
        harness = READER_HARNESS.replace("if(url.startsWith('/api/admin-control/history'))",
            "if(url.startsWith('/api/knowledge-list?'))data={items:[],total:0,offset:0,next_offset:null};\n  else if(url.startsWith('/api/admin-control/history'))", 1)
        harness = harness.replace("(async()=>{", "(async()=>{\n" + scenario, 1)
        if not NODE:
            self.skipTest("Node.js required")
        result = subprocess.run([NODE, "-e", harness],
            input=json.dumps({"script": JAVASCRIPT, "scenario": "presentation"}),
            text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_metadata_and_preview_remain_text_not_html(self):
        self.run_browser(r"""
 run("state.view='knowledge';document.getElementById('state').value='active';state.listData={items:[{knowledge_id:'k1',title:'<svg onload=bad()>',preview:'<script>bad()</script>',state:'active',revision:1,created:1}],total:1,offset:0,next_offset:null};renderList()");
 const body=get('list').children[0].children[0];
 assert.equal(body.children[0].textContent,'<svg onload=bad()>');
 assert(body.children.some(x=>x.textContent==='<script>bad()</script>'));
 assert.equal(requests.length,0);
""")


@unittest.skipUnless(NODE, "Node.js required for actual DSH resource")
class DshKnowledgePresentationTests(unittest.TestCase):
    def run_client(self, code, helpers=False):
        if helpers:
            harness = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');let factory;
const source=fs.readFileSync(process.argv[1],'utf8').replace('exports.apply = apply;',
 'exports.reviewRows=reviewRows;exports.currentPresetOf=currentPresetOf;exports.getLabels=getLabels;exports.PANEL_CSS=PANEL_CSS;exports.apply = apply;');
vm.runInNewContext(source,{window:{__ModuleLoader__:{load:x=>{factory=x.factory;}}},navigator:{language:'en'}});
const plugin=factory(()=>({})),t=plugin.getLabels('en');
"""
        else:
            harness = DSH_HARNESS
        result = subprocess.run([NODE, "-e", harness + "\n(async()=>{\n" + code +
                                 "\nconsole.log('ok');})().catch(e=>{console.error(e);process.exitCode=1;});", str(CLIENT)],
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_policy_admission_routing_budget_and_execution_are_separate(self):
        self.run_client(r"""
const r=plugin.reviewRows({admin_admission_enabled:false,administrator:{policy_enabled:true,admission_enabled:false,
 routing_ready:false,routing_reason:'dsh_profile_unavailable',review_mode:'legacy',phase:'idle'},admission:{admission_status:'available'}},t);
assert.equal(r.label,'New reviews paused');assert.equal(r.admitted,false);
const rows=Object.fromEntries(r.rows);
assert.equal(rows['Policy switch'],'Enabled');assert.equal(rows['Model routing'],'Not ready');
assert.equal(rows['Budget admission'],'Available');assert.equal(rows['Execution'],'Idle');
assert.equal(r.reason,'dsh_profile_unavailable');assert.equal(r.model,undefined);
""", helpers=True)

    def test_absent_admission_is_unknown_not_off_or_inferred_from_budget(self):
        self.run_client(r"""
const r=plugin.reviewRows({admission:{admission_status:'available'}},t);
assert.equal(r.label,'Unknown');assert.equal(Object.fromEntries(r.rows)['Model routing'],'Unknown');
assert.equal(plugin.currentPresetOf({}), 'custom');
assert.equal(plugin.currentPresetOf({admin_admission_enabled:false}), 'off');
""", helpers=True)

    def test_client_styles_only_use_verified_host_color_tokens(self):
        self.run_client(r"""
assert(!/#(?:[0-9a-f]{3}){1,2}\b|rgba?\(/i.test(plugin.PANEL_CSS));
for(const token of plugin.PANEL_CSS.match(/--[a-z0-9-]+/g))assert(token.startsWith('--dsw-alias-'));
assert(plugin.PANEL_CSS.includes('max-height:calc(100dvh'));
assert(plugin.PANEL_CSS.includes(':focus-visible'));
""", helpers=True)

    def test_overview_uses_unknown_for_missing_counts_and_zero_for_actual_counts(self):
        self.run_client(r"""
const h=harness();let cards=nodes(h.render()).filter(x=>x.props?.className==='al-stat');
assert.equal(cards.length,4);assert(cards.every(c=>text(c.children[1])==='Unknown'));
h.states[0]={...summary('confirmed'),active_knowledge:0,work_memories:0,pending_review_cases:0,awaiting_approval_cases:0};
cards=nodes(h.render()).filter(x=>x.props?.className==='al-stat');
assert(cards.every(c=>text(c.children[1])==='0'));assert.equal(h.calls.length,0);
""")

    def test_collapsed_details_and_read_only_primary_action(self):
        self.run_client(r"""
const h=harness(),tree=h.render();
const details=nodes(tree).filter(x=>x.type==='details');assert(details.length>=3);
assert(details.every(x=>!x.props?.open));
const primary=nodes(tree).find(x=>x.type==='button'&&x.props?.className==='al-primary');
assert(primary);assert(text(primary).toLowerCase().includes('read-only'));
assert.equal(h.calls.length,0,'rendering cannot enable review or import history');
""")

    def test_dialog_uses_native_top_layer_not_composer_positioning_or_extra_framework(self):
        source = CLIENT.read_text(encoding="utf-8")
        self.assertIn('h("dialog", {ref: dialogRef', source)
        self.assertIn("dialog.showModal()", source)
        self.assertIn('"aria-haspopup": "dialog"', source)
        self.assertIn("event.preventDefault(); closePanel();", source)
        self.assertIn("lockRef.current", source)
        self.assertNotIn("document.body.append", source)
        self.assertNotIn("dsh-client-ui-primitives", source)
        self.assertNotIn('position: "absolute"', source)

    def run_composer(self, code):
        harness = "let confirmation=true,confirmationCalls=0;\n" + COMPOSER_HARNESS.replace("window:{__ModuleLoader__:",
            "window:{confirm:()=>{++confirmationCalls;return confirmation;},crypto:{randomUUID:()=> 'fixture-request-id'},__ModuleLoader__:", 1)
        harness = harness.replace("assert.equal(options,undefined,'only GET is allowed');", "", 1)
        harness = harness.replace("calls.push({url,wait})", "calls.push({url,wait,options})", 1)
        result = subprocess.run([NODE, "-e", harness + "\n(async()=>{\n" + code +
                                 "\nconsole.log('ok');})().catch(e=>{console.error(e);process.exitCode=1;});", str(CLIENT)],
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_native_cancel_keeps_an_inflight_setting_open_but_allows_closing_after_receipt(self):
        self.run_composer(r"""
const h=harness();await readyA(h);h.toggle();h.answer(1,summary('PROJECT_A',A));await h.flush();
const choose=()=>nodes(h.tree()).find(n=>n.type==='button'&&text(n)==='Light (15/d)');
choose().props.onClick();h.commit();assert.equal(h.calls[2].options.method,'POST');
let prevented=false;nodes(h.tree()).find(n=>n.type==='dialog').props.onCancel({preventDefault:()=>{prevented=true;}});h.commit();
assert(prevented);assert(h.toolbar().props['aria-expanded'],'an inflight write must not lose its dialog');
h.answer(2,{ok:true});await h.flush();h.answer(3,summary('PROJECT_A',A));await h.flush();
nodes(h.tree()).find(n=>n.type==='dialog').props.onCancel({preventDefault(){}});h.commit();
assert.equal(h.toolbar().props['aria-expanded'],false);
""")

    def test_uncertain_setting_survives_close_and_reopen_with_the_same_request_id(self):
        self.run_composer(r"""
const h=harness();await readyA(h);h.toggle();h.answer(1,summary('PROJECT_A',A));await h.flush();
const choose=()=>nodes(h.tree()).find(n=>n.type==='button'&&text(n)==='Light (15/d)');
choose().props.onClick();h.commit();const first=JSON.parse(h.calls[2].options.body);
h.answer(2,{ok:false,error:'result not confirmed'},false);await h.flush();
h.answer(3,{ok:true,found:false});await h.flush();h.answer(4,summary('PROJECT_A',A));await h.flush();
nodes(h.tree()).find(n=>n.type==='dialog').props.onCancel({preventDefault(){}});h.commit();
assert.equal(h.toolbar().props['aria-expanded'],false,'a completed-but-uncertain request must not trap the user');
h.toggle();h.answer(5,summary('PROJECT_A',A));await h.flush();
assert(nodes(h.tree()).find(n=>n.type==='button'&&n.props.className==='al-primary').props.disabled);
choose().props.onClick();h.commit();const retry=JSON.parse(h.calls[6].options.body);
assert.equal(retry.request_id,first.request_id);assert.deepEqual(retry.panel_snapshot,first.panel_snapshot);
h.answer(6,{ok:true});await h.flush();h.answer(7,summary('PROJECT_A',A));await h.flush();
""")

    def test_default_view_has_one_metric_two_actions_and_one_advanced_disclosure(self):
        self.run_client(r"""
const h=harness(),tree=h.render();
const visible=n=>!n||typeof n!=='object'?[]:[n,...(n.type==='details'?[n.children.find(c=>c?.type==='summary')]:n.children||[]).flatMap(visible)];
const shown=visible(tree),buttons=shown.filter(n=>n.type==='button');
assert.equal(shown.filter(n=>n.props?.className==='al-stat').length,1);
assert.deepEqual(buttons.map(n=>n.props['data-action']).filter(Boolean),['open_browser','show_recommendations']);
const advanced=shown.filter(n=>n.type==='details');assert.equal(advanced.length,1);
assert.equal(text(advanced[0].children[0]),'Advanced settings');assert(!advanced[0].props.open);
for(const forbidden of ['Model routing','Reviews today','Maintenance and diagnostics','Review and settings'])
 assert(!shown.some(n=>(n.children||[]).includes(forbidden)),forbidden+' belongs in Advanced');
assert(!shown.some(n=>n.type==='select'));assert.equal(h.calls.length,0);
""")

    def test_recommendation_is_only_preselected_and_does_not_relabel_the_current_preset(self):
        self.run_client(r"""
const h=harness();h.states[0]={...summary('confirmed'),admin_admission_enabled:false,administrator:{routing_ready:true}};
let tree=h.render();nodes(tree).find(n=>n.props?.['data-action']==='show_recommendations').props.onClick();tree=h.render();
const section=nodes(tree).find(n=>n.props?.className==='al-recommendation');
assert.equal(nodes(section).find(n=>n.type==='select').props.value,'light');
assert(text(section).includes('Current preset: Off'));assert(text(section).includes('only a suggestion'));
assert.equal(h.states[0].admin_admission_enabled,false);assert.equal(h.calls.length,0);
const h2=harness();nodes(h2.render()).find(n=>n.props?.['data-action']==='show_recommendations').props.onClick();
assert(text(nodes(h2.render()).find(n=>n.props?.className==='al-recommendation')).includes('Current preset: Unknown'));
""")

    def test_recommendation_enable_waits_for_confirmation_and_uses_frozen_snapshot(self):
        self.run_composer(r"""
const h=harness();await readyA(h);h.toggle();
const data={...summary('PROJECT_A',A),admin_admission_enabled:false,administrator:{routing_ready:true}};
h.answer(1,data);await h.flush();
const action=name=>nodes(h.tree()).find(n=>n.props?.['data-action']===name);
action('show_recommendations').props.onClick();h.commit();assert.equal(h.calls.length,2);
confirmation=false;action('apply_recommendation').props.onClick();h.commit();assert.equal(h.calls.length,2);assert.equal(confirmationCalls,1);
confirmation=true;action('apply_recommendation').props.onClick();h.commit();assert.equal(h.calls.length,3);
const sent=JSON.parse(h.calls[2].options.body);assert.equal(sent.action,'set_admin_preset');assert.equal(sent.preset,'light');
assert.equal(sent.confirm,true);assert.deepEqual(sent.panel_snapshot,data.panel_snapshot);
assert.equal(data.admin_admission_enabled,false,'there must be no optimistic settings mutation');
h.answer(2,{ok:true});await h.flush();h.answer(3,data);await h.flush();
""")

    def test_pause_recommendation_also_requires_confirmation(self):
        self.run_composer(r"""
const h=harness();await readyA(h);h.toggle();
const data={...summary('PROJECT_A',A),admin_admission_enabled:true,administrator:{routing_ready:true}};
h.answer(1,data);await h.flush();
const action=name=>nodes(h.tree()).find(n=>n.props?.['data-action']===name);
action('show_recommendations').props.onClick();h.commit();
nodes(h.tree()).find(n=>n.type==='select').props.onChange({target:{value:'off'}});h.commit();
confirmation=false;action('apply_recommendation').props.onClick();h.commit();assert.equal(h.calls.length,2);
confirmation=true;action('apply_recommendation').props.onClick();h.commit();assert.equal(h.calls.length,3);
assert.equal(JSON.parse(h.calls[2].options.body).preset,'off');assert.equal(confirmationCalls,2);
h.answer(2,{ok:true});await h.flush();h.answer(3,data);await h.flush();
""")

    def test_stage_or_token_mode_recommendation_preserves_configuration_without_legacy_apply(self):
        self.run_client(r"""
for(const extra of [{administrator:{review_mode:'phase'}},{project_usage:{...summary('confirmed').project_usage,budget_mode:'tokens'}}]){
 const h=harness();h.states[0]={...summary('confirmed'),...extra};
 nodes(h.render()).find(n=>n.props?.['data-action']==='show_recommendations').props.onClick();
 const section=nodes(h.render()).find(n=>n.props?.className==='al-recommendation');
 assert(text(section).includes('Keep the current review mode, frequency and token budget'));
 assert(!nodes(section).some(n=>n.type==='select'||n.props?.['data-action']==='apply_recommendation'));
 assert(nodes(section).some(n=>n.props?.['data-action']==='open_review'));assert.equal(h.calls.length,0);
}
""")

    def test_known_unready_routing_disables_new_recommendation_but_not_local_browsing(self):
        self.run_client(r"""
const h=harness();h.states[0]={...summary('confirmed'),admin_admission_enabled:false,administrator:{routing_ready:false}};
nodes(h.render()).find(n=>n.props?.['data-action']==='show_recommendations').props.onClick();
const tree=h.render();assert(nodes(tree).find(n=>n.props?.['data-action']==='apply_recommendation').props.disabled);
assert(!nodes(tree).find(n=>n.props?.['data-action']==='open_browser').props.disabled);
assert(text(tree).includes('Model routing is not ready'));assert.equal(h.calls.length,0);
""")

    def test_recommendation_state_resets_when_changing_project_and_chat(self):
        self.run_composer(r"""
const h=harness('panel',{cwd:A,sessionId:'chat-a'});h.commit();h.answer(0,summary('PROJECT_A',A));await h.flush();
const action=name=>nodes(h.tree()).find(n=>n.props?.['data-action']===name);
action('show_recommendations').props.onClick();h.commit();
nodes(h.tree()).find(n=>n.type==='select').props.onChange({target:{value:'off'}});h.commit();
h.setProps({cwd:B,sessionId:'chat-b'});h.commit();h.answer(1,summary('PROJECT_B',B));await h.flush();
assert(!action('show_recommendations').props['aria-expanded']);
action('show_recommendations').props.onClick();h.commit();
assert.equal(nodes(h.tree()).find(n=>n.type==='select').props.value,'light');assert.equal(h.calls.length,2);
""")

    def test_recommendation_uncertain_retry_reuses_original_request_even_if_routing_changes(self):
        self.run_composer(r"""
const h=harness();await readyA(h);h.toggle();
let data={...summary('PROJECT_A',A),admin_admission_enabled:false,administrator:{routing_ready:true}};
h.answer(1,data);await h.flush();
const action=name=>nodes(h.tree()).find(n=>n.props?.['data-action']===name);
action('show_recommendations').props.onClick();h.commit();action('apply_recommendation').props.onClick();h.commit();
const first=JSON.parse(h.calls[2].options.body);
h.answer(2,{ok:false,error:'uncertain'},false);await h.flush();h.answer(3,{ok:true,found:false});await h.flush();
h.answer(4,data);await h.flush();nodes(h.tree()).find(n=>n.type==='dialog').props.onCancel({preventDefault(){}});h.commit();
h.toggle();data={...data,administrator:{routing_ready:false}};h.answer(5,data);await h.flush();
action('show_recommendations').props.onClick();h.commit();assert(!action('apply_recommendation').props.disabled);
action('apply_recommendation').props.onClick();h.commit();const retry=JSON.parse(h.calls[6].options.body);
assert.equal(retry.request_id,first.request_id);assert.deepEqual(retry.panel_snapshot,first.panel_snapshot);
h.answer(6,{ok:true});await h.flush();h.answer(7,data);await h.flush();
""")

    def test_capture_uncertainty_remains_visible_outside_advanced_settings(self):
        self.run_client(r"""
const h=harness();h.states[0]={...summary('confirmed'),capture_transport:{pending_events:2,retryable_events:1,unresolved_events:1,persistence:'bridge_memory_only'}};
const tree=h.render(),visible=n=>!n||typeof n!=='object'?[]:[n,...(n.type==='details'?[]:n.children||[]).flatMap(visible)];
assert(visible(tree).some(n=>(n.children||[]).some(c=>typeof c==='string'&&c.includes('Capture is unconfirmed'))));
assert.equal(h.calls.length,0);
""")

    def test_pending_pause_reopens_with_the_original_selection_and_no_new_confirmation(self):
        self.run_composer(r"""
const h=harness();await readyA(h);h.toggle();
const data={...summary('PROJECT_A',A),admin_admission_enabled:true,administrator:{routing_ready:true}};
h.answer(1,data);await h.flush();
const action=name=>nodes(h.tree()).find(n=>n.props?.['data-action']===name);
action('show_recommendations').props.onClick();h.commit();
nodes(h.tree()).find(n=>n.type==='select').props.onChange({target:{value:'off'}});h.commit();
action('apply_recommendation').props.onClick();h.commit();const first=JSON.parse(h.calls[2].options.body);
h.answer(2,{ok:false,error:'uncertain'},false);await h.flush();h.answer(3,{ok:true,found:false});await h.flush();
h.answer(4,data);await h.flush();nodes(h.tree()).find(n=>n.type==='dialog').props.onCancel({preventDefault(){}});h.commit();
h.toggle();h.answer(5,data);await h.flush();action('show_recommendations').props.onClick();h.commit();
assert.equal(nodes(h.tree()).find(n=>n.type==='select').props.value,'off');assert(!action('apply_recommendation').props.disabled);
action('apply_recommendation').props.onClick();h.commit();const retry=JSON.parse(h.calls[6].options.body);
assert.equal(retry.preset,'off');assert.equal(retry.request_id,first.request_id);assert.equal(confirmationCalls,1);
h.answer(6,{ok:true});await h.flush();h.answer(7,data);await h.flush();
""")

    def test_already_applied_requires_complete_matching_limits_not_only_the_daily_cap(self):
        self.run_client(r"""
for(const max_calls_per_batch of [2,5]){
 const h=harness();h.states[0]={...summary('confirmed'),admin_admission_enabled:true,administrator:{routing_ready:true},
  panel_snapshot:{...summary('confirmed').panel_snapshot,project_limits:{max_calls_per_day:15,max_calls_per_batch,max_batch_seconds:180,max_run_seconds:300}}};
 nodes(h.render()).find(n=>n.props?.['data-action']==='show_recommendations').props.onClick();
 const apply=nodes(h.render()).find(n=>n.props?.['data-action']==='apply_recommendation');
 assert.equal(apply.props.disabled,max_calls_per_batch===2);assert.equal(h.calls.length,0);
}
""")
