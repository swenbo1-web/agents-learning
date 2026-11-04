"""Shared offline phase-review controls for all host profiles."""

PHASE_HTML = '''<section class="admin-details" id="phase-settings"><h3 data-i18n="phaseHeading">Stage review and token budget</h3>
<label for="phase-mode" data-i18n="phaseMode">Review method</label><select id="phase-mode"><option value="phase" data-i18n="phaseModeStage">Completed-stage batches</option><option value="legacy" data-i18n="phaseModeLegacy">Legacy case review</option></select>
<p class="note" data-i18n="phaseNote">Count real task deliveries, not replies. Raw work remains searchable. Historical imports are separate.</p>
<div class="admin-limit-grid"><div><label for="phase-every" data-i18n="phaseEvery">Review every N completed stages</label><input id="phase-every" type="number" min="1" max="20" value="1"></div>
<div><label for="phase-wait" data-i18n="phaseWait">Optional maximum wait in seconds (0 off)</label><input id="phase-wait" type="number" min="0" max="86400" value="0"></div>
<div><label for="phase-daily" data-i18n="phaseDaily">Project tokens per UTC day</label><input id="phase-daily" type="number" min="0" max="1000000000" value="1000000"></div>
<div><label for="phase-input" data-i18n="phaseInput">Estimated input tokens per batch</label><input id="phase-input" type="number" min="16000" max="1000000" value="40000"></div>
<div><label for="phase-output" data-i18n="phaseOutput">Reserved output tokens per batch</label><input id="phase-output" type="number" min="256" max="40000" value="4000"></div></div>
<p class="note" data-i18n="phaseEstimate">Admission uses estimates; provider receipts settle actual usage. Unknown usage stays reserved. Explicit global protection still applies. Oversized sources remain uncovered.</p>
<p class="note" data-i18n="phaseWaitNote">Optional waiting applies to an undersized stage group on the next scheduler wake; it does not start a timer or bypass admission.</p>
<p class="note" id="phase-wait-upgrade" hidden></p>
<div id="phase-progress" aria-live="polite"></div><div id="phase-results"></div></section>'''

PHASE_JAVASCRIPT = r'''
Object.assign(strings,{
phaseHeading:['Stage review and token budget','阶段集中审核与 token 预算'],
phaseMode:['Review method','审核方式'],phaseModeStage:['Completed-stage batches','按完成阶段集中审核'],phaseModeLegacy:['Legacy case review','原逐案审核（兼容）'],
phaseNote:['Real task deliveries count once; replies, reconnects and child stops do not. Stages are grouped by executor/model; Run now may process a smaller group. Raw work is searchable immediately; historical imports remain separate.','真实任务或阶段交付只计一次；回复、重连和子任务停止不重复计数。按执行器和模型分组；可点立即整理处理未满频率的分组。原始记录可立即检索，历史吸收仍独立授权。'],
phaseEvery:['Review every N completed stages (1–20)','每完成几个阶段审核一次（1–20）'],phaseDaily:['Project token budget per UTC day','项目每日 token 预算（UTC）'],
phaseWait:['Optional maximum wait in seconds (0 off)','未满频率的最长等待秒数（0 关闭）'],
phaseWaitNote:['When explicitly enabled, an undersized stage group may be reviewed on the next scheduler wake after this wait. No persistent timer is started; budget, executor and evidence checks still apply.','明确开启后，未满阶段频率的分组可在等待到期后的下一次调度唤醒时审核；不启动常驻计时器，仍检查预算、执行器和证据。'],
phaseWaitUpgrade:['This core does not support maximum waiting yet. Upgrade it before enabling this option; other settings can still be saved.','当前核心版本尚不支持最长等待；请升级后再开启此项，其他设置仍可正常保存。'],
phaseInput:['Estimated input tokens per batch','每批预估输入 token 上限'],phaseOutput:['Reserved output tokens per batch','每批输出 token 预留'],
phaseEstimate:['Admission uses estimates, not a hard provider billing limit. Actual receipts settle usage; unknown usage stays reserved. Existing global protection still applies. Uncovered sources remain pending.','准入使用估算，不是平台计费硬上限；按实际回执结算，未知用量继续预留。已有全局保护仍生效；未覆盖资料继续保留。'],
phaseProgress:['Stages waiting: {waiting} · completed: {done} · partial: {partial} · blocked: {blocked}. Reported tokens: {used}; unsettled reservations: {reserved}. Effective runner: {seconds}s.','待整理阶段：{waiting} · 已整理：{done} · 部分覆盖：{partial} · 待核对：{blocked}。已上报 token：{used}；未结算预留：{reserved}。实际运行上限：{seconds}秒。'],
phaseInvalid:['Invalid stage frequency, maximum wait or token budget','阶段频率、最长等待或 token 预算不符合范围'],
phaseUsed:['Reported project tokens today','项目今日已上报 token'],phaseAvailable:['Estimated tokens remaining','项目估算可用 token'],
phaseAwaiting:['Awaiting a review result','等待审核结果'],phaseOldBoundary:['{n} stages belong to an earlier login or governance boundary and are retained outside the current queue.','另保留旧登录或治理边界的 {n} 个阶段，不计入当前待办。'],
phaseSourceLimit:['{n} public sources exceeded the stage scan limit. They remain searchable but were not reviewed; split the stage or explicitly import the selected history. Repeated Run now cannot extend this capture limit.','{n} 条公开来源超出单阶段扫描上限，仍可查阅但尚未审核；请拆分阶段或明确选取历史资料导入。重复立即整理不会扩大此次采集上限。'],
phaseWindow:['A full call needs {timeout}s. The batch, runner or scheduler window is too short; increase it explicitly before retrying.','完整调用需预留 {timeout}秒，当前批次、运行器或调度窗口不足；请明确调大对应限时后重试。'],
phaseCoverage:['Coverage complete','所选案例覆盖完成'],phasePartial:['Some sources remain uncovered','仍有未覆盖资料'],
phaseBatch:['Batch {id}: {state}','批次 {id}：{state}'],phaseItem:['Case {id}: {state}; candidates {count}. {reason}','案例 {id}：{state}；候选 {count} 条。{reason}'],
phaseQueueNote:['These stages belong to this host and account boundary. Legacy cases, work summaries and historical units are separate; they are not the stage batch queue.','这里仅统计当前宿主与登录边界的阶段；旧逐案、工作摘要和历史资料分别统计，不等于阶段批次排队。'],
phaseLocallyDeferred:['Pure status checks retained locally: {n} stages; some material remains unreviewed. Only the remaining material has not been submitted; these stages are not complete. A later normal stage may include it within its remaining budget; use “Check and continue” here to request review.','纯状态检查本地暂存：{n} 个阶段，仍有未审核资料。仅余下资料尚未送审，不算审核完成。后续正常阶段可按剩余预算合批；可在此页点击“检查并继续整理”主动补审。'],
routine_deferred:['Retained locally; some material remains unreviewed','本地暂存，仍有未审核资料'],
executor_waiting:['Waiting for executor','等待执行器'],
check_candidate_reference_source_blocked:['Candidate source lineage requires review; approval does not resolve upstream concerns.','候选来源链需要复核；批准此条不会解决上游来源的疑点。'],
phaseSealPending:['{n} delivered stages await local sealing recovery. Their original Stop boundaries are retained; this is not model review.','{n} 个已交付阶段等待本地封存恢复；保留原 Stop 边界，尚未送入模型审核。'],
phaseExecutorWaiting:['{n} batches await their original executor. Other available model groups can continue; existing call locks remain in force.','{n} 个批次等待原执行器；其他可用模型分组可继续，原调用锁保持有效。'],
phaseBatchCounts:['This batch: {cases} submitted cases · {evidence} evidence records · {calls} execution attempts · {candidates} candidates · {published} automatically published.','本批送审 {cases} 个案例 · {evidence} 条证据 · {calls} 次执行尝试 · 生成 {candidates} 条候选 · 自动发布 {published} 条。'],
phaseBatchTime:['Batch elapsed {batch}s (includes waiting); recorded call processing {call}s (includes startup and local checks). These are not per-knowledge times.','批次经过 {batch} 秒（含等待）；调用处理 {call} 秒（含启动与本地检查）。均不是单条知识耗时。'],
phaseBatchTokens:['Host-reported input {input} · output {output} · total {total} tokens. Missing usage stays unknown; execution attempts are not proof of paid requests.','宿主上报输入 {input} · 输出 {output} · 合计 {total} token。未上报用量保持未知；执行尝试数不等于确认计费次数。'],
phaseBatchCached:['Cached input {cached} tokens is a subset of input; do not add it again. Missing cache usage remains unknown.','缓存输入 {cached} token 已包含在输入量内，不额外累加；未上报缓存用量保持未知。'],
phaseUsageConflict:['Usage records are inconsistent. Unreliable cache counts are unavailable; reported lower bounds and conservative reservations are retained.','用量记录存在不一致；无法核实的缓存数不显示，保留已报告的用量下界与保守预留。'],
phaseSelection:['Frozen selection: {cases} cases from {phases} stages; {reused} reused, {uncovered} uncovered. Packet {chars} characters; related knowledge {knowledge}, candidates {candidates} (reference occurrences across cases).','冻结选材：{phases} 个阶段送审 {cases} 个案例；复用 {reused} 个，未覆盖 {uncovered} 个。审核包 {chars} 字符；比较知识 {knowledge}、候选 {candidates} 次（按案例累计引用）。'],
phaseSelectionNote:['This describes the frozen material, not a replay of historical admission. An unrecorded trigger remains unknown; a skipped result is not a failed or free call.','此处说明冻结材料，不重新推断历史准入；未记录的触发原因保持未知。跳过结果不代表调用失败或没有消耗。'],
phaseAdmissionTrigger:['Recorded selection trigger: {reason}.','本地记录的选材触发原因：{reason}。'],
phaseAdmissionPolicy:['Policy at selection: every {every} stages; maximum wait {wait}s (0 off). These are the frozen values, not current settings.','选材时策略：每 {every} 个阶段；最长等待 {wait} 秒（0 关闭）。这是当时冻结值，不是当前设置。'],
phaseAdmissionCount:['Completed-stage frequency reached','已达到阶段频率'],
phaseAdmissionWait:['Undersized group reached its optional wait','未满频率分组已达到可选等待时限'],
phaseAdmissionContinuation:['Continue remaining material','继续处理未覆盖材料'],
phaseAdmissionExplicit:['Explicit request to prepare a batch','显式请求组批'],
phaseAdmissionUnknown:['Not recorded','未记录'],
phaseSelectionVisible:['Visible material was selected for review.','已选择可见材料供审核。'],
phaseSelectionReused:['No new cases were submitted; local reuse was recorded. This does not prove a new model call.','没有送审新案例，已登记本地复用；不代表新增模型调用。'],
phaseSelectionUncovered:['Material remains uncovered.','资料仍未覆盖。'],
phaseSelectionEmpty:['No visible cases were submitted.','没有送审可见案例。'],
phaseBatchTools:['Selected related tool observations: {n}. This is not a full tool audit or independent verification.','已关联 {n} 条相关工具观察；这是有界选材，不是全部工具审计或独立验证。'],
phaseBatchToolsLimited:['Some matching observations were not included, or selection was limited or unavailable. Review covers only the visible cases and evidence; original work history remains available.','部分匹配观察未纳入，或选材受限／暂不可用；审核仅覆盖可见案例与证据，原工作历史仍可查阅。']
});
const PHASE_FIELDS=['phase-mode','phase-every','phase-wait','phase-daily','phase-input','phase-output'];
ADMIN_FIELDS.push(...PHASE_FIELDS);
function phaseWaitSupported(data){return Object.prototype.hasOwnProperty.call(data?.phase_review?.policy||{},'max_wait_seconds');}
const basePhaseHydrate=hydrateAdminForm;
hydrateAdminForm=function(data){basePhaseHydrate(data);const cfg=data.phase_review?.policy;if(!cfg)return;const fresh=!cfg.configured&&!data.policy?.policy_version&&!data.policy?.enabled&&!data.enabled;$('phase-mode').value=fresh?'phase':cfg.mode;for(const[id,key]of [['phase-every','every_phases'],['phase-wait','max_wait_seconds'],['phase-daily','daily_tokens'],['phase-input','max_input_tokens'],['phase-output','max_output_tokens']])$(id).value=String(cfg[key]??(key==='max_wait_seconds'?0:''));};
const basePhaseConfiguration=adminConfiguration;
adminConfiguration=function(action){const result=basePhaseConfiguration(action);const base=state.adminFormBase;if(action==='disable'||!base?.phase_review)return result;const cfg={mode:$('phase-mode').value},waitSupported=phaseWaitSupported(base);for(const[id,key,min,max]of [['phase-every','every_phases',1,20],['phase-wait','max_wait_seconds',0,86400],['phase-daily','daily_tokens',0,1000000000],['phase-input','max_input_tokens',16000,1000000],['phase-output','max_output_tokens',256,40000]]){const text=$(id).value,n=Number(text);if(!/^\d+$/.test(text)||!Number.isSafeInteger(n)||n<min||n>max)throw new Error(t('phaseInvalid'));if(key==='max_wait_seconds'&&!waitSupported){if(n!==0)throw new Error(t('phaseWaitUpgrade'));continue;}cfg[key]=n;}result.phase_review=cfg;result.expected_phase_version=base.phase_review.policy.version;if(cfg.mode==='phase')result.project_limits.max_reported_tokens_per_day=cfg.daily_tokens;return result;};
const basePhaseRender=renderAdminState;
function phaseBatchMetrics(detail,batch){const m=batch.metrics;if(!m)return;const show=value=>value??t('unknown');detail.append(node('p',t('phaseBatchCounts',{cases:show(m.submitted_cases),evidence:show(m.evidence_records),calls:show(m.call_count),candidates:show(m.candidate_count),published:show(m.published_count)}),'note'));detail.append(node('p',t('phaseBatchTime',{batch:show(m.batch_elapsed_seconds),call:show(m.call_elapsed_seconds)}),'muted'));detail.append(node('p',t('phaseBatchTokens',{input:show(m.input_tokens),output:show(m.output_tokens),total:show(m.total_tokens)}),'muted'));detail.append(node('p',t('phaseBatchCached',{cached:show(m.cached_input_tokens)}),'muted'));if(m.usage_conflict===true)detail.append(node('p',t('phaseUsageConflict'),'note'));const d=m.selection_diagnostics;if(d){const trigger={phase_count:'phaseAdmissionCount',max_wait:'phaseAdmissionWait',continuation:'phaseAdmissionContinuation',explicit_request:'phaseAdmissionExplicit'}[d.trigger_reason]||'phaseAdmissionUnknown';detail.append(node('p',t('phaseAdmissionTrigger',{reason:t(trigger)}),'note'));if(d.policy_at_selection)detail.append(node('p',t('phaseAdmissionPolicy',{every:show(d.policy_at_selection.every_phases),wait:show(d.policy_at_selection.max_wait_seconds)}),'muted'));const reason={visible_material_selected:'phaseSelectionVisible',local_reuse_only:'phaseSelectionReused',uncovered_material:'phaseSelectionUncovered',no_visible_material:'phaseSelectionEmpty'}[d.selection_reason];if(reason)detail.append(node('p',t(reason),'note'));detail.append(node('p',t('phaseSelection',{cases:show(d.selected_cases),phases:show(d.phase_count),reused:show(d.reused_cases),uncovered:show(d.uncovered_cases),chars:show(d.packet_chars),knowledge:show(d.related_knowledge_refs),candidates:show(d.related_candidate_refs)}),'muted'));detail.append(node('p',t('phaseSelectionNote'),'muted'));}}
renderAdminState=function(){basePhaseRender();const data=state.adminData,view=data?.phase_review;if(!view)return;const p=view.phases||{},usage=projectUsage(data)||{};$('phase-progress').textContent=t('phaseProgress',{waiting:view.waiting_phase_count??((p.sealed||0)+(p.queued||0)),blocked:view.blocked_phase_count??0,done:p.completed||0,partial:p.partial||0,used:usage.reported_tokens??'?',reserved:usage.token_reservations??'?',seconds:data.effective_run_seconds??'?'});const area=$('phase-results');area.replaceChildren();area.append(node('p',t('phaseQueueNote'),'note'));const deferred=view.locally_deferred_phase_count??p.routine_deferred??0;if(deferred)area.append(node('p',t('phaseLocallyDeferred',{n:deferred}),'note'));if(view.historical_boundary_phases)area.append(node('p',t('phaseOldBoundary',{n:view.historical_boundary_phases}),'note'));if(view.source_scan_omitted)area.append(node('p',t('phaseSourceLimit',{n:view.source_scan_omitted}),'note'));for(const batch of view.recent_batches||[]){const detail=document.createElement('details'),title=document.createElement('summary');title.textContent=t('phaseBatch',{id:batch.id.slice(0,10),state:t(batch.state)})+' · '+t(batch.coverage_complete===true?'phaseCoverage':batch.coverage_complete===false?'phasePartial':'phaseAwaiting');detail.append(title);phaseBatchMetrics(detail,batch);for(const item of batch.items||[]){const line=document.createElement('p');line.textContent=t('phaseItem',{id:(item.problem_id||'').slice(0,10),state:t(item.status),count:item.candidate_ids?.length||0,reason:item.reason||''});detail.append(line);}area.append(detail);}if(view.policy.mode==='phase'){const cards=$('admin-status-cards');cards.replaceChildren();for(const [key,value]of [['phaseUsed',usage.reported_tokens],['phaseAvailable',usage.remaining_reported_tokens],['adminCompletedReviews',(usage.effective_completed_reviews??usage.completed_reviews)],['adminUsageCalls',usage.counted_attempts]]){const card=node('div',undefined,'stat');card.append(node('span',t(key)),node('strong',value??t('unknown')));cards.append(card);}}const limits=data.project_budget_policy?.values||{};if(view.policy.mode==='phase'&&(Math.min(limits.max_batch_seconds??Infinity,data.effective_run_seconds??Infinity)<(limits.timeout_seconds??120)))area.append(node('p',t('phaseWindow',{timeout:limits.timeout_seconds??120}),'note'));const stage=$('phase-mode').value==='phase';if(stage)for(const id of ['admin-frequency-preset','admin-daily-calls','admin-batch-calls','admin-summary-enabled'])$(id).disabled=true;};
for(const id of PHASE_FIELDS)$(id).addEventListener('change',()=>{state.adminDirty=true;renderAdminState();controls();});
const basePhaseControls=controls;
controls=function(){basePhaseControls();const waitSupported=phaseWaitSupported(state.adminFormBase??state.adminData);$('phase-wait').disabled=!waitSupported||$('phase-wait').disabled;$('phase-wait-upgrade').hidden=waitSupported;$('phase-wait-upgrade').textContent=t('phaseWaitUpgrade');if(!state.adminData?.phase_review||$('phase-mode').value!=='phase')return;for(const id of ['admin-frequency-preset','admin-daily-calls','admin-batch-calls','admin-summary-enabled'])$(id).disabled=true;};
const basePhaseRecoveryRender=renderAdminState;
Object.assign(strings,{
phaseBoundarySample:['Delivery receipts: {n} sampled stages (limit {limit})','交付回执：抽样 {n} 个阶段（上限 {limit}）'],
phaseBoundaryItem:['Stage {id} · task {task} · revision {revision} · delivery {delivery}','阶段 {id} · 任务 {task} · 修订 {revision} · 交付版本 {delivery}'],
phaseBoundaryWaiting:['The current delivery is still waiting for its host Stop receipt.','当前交付仍等待宿主 Stop 回执。'],
phaseBoundaryMissing:['The task boundary has changed; the original matching Stop receipt was not found within the checked range.','任务边界已变化；已检查范围内未找到原交付对应的 Stop 回执。'],
phaseBoundarySeal:['The matching Stop receipt exists; local sealing is pending.','已存在匹配 Stop 回执，等待本地封存。'],
phaseBoundaryRetry:['The original receipt is retained; local sealing needs recovery.','原始回执已保留，本地封存待恢复。'],
phaseBoundaryHistorical:['This stage belongs to an earlier host or session boundary.','该阶段属于较早的宿主或会话边界。'],
phaseBoundaryUnknown:['The bounded check cannot confirm the original delivery receipt.','有界检查尚不能确认原交付回执。'],
phaseBoundaryUnavailable:['Delivery receipt diagnostics are unavailable; pending stages remain unverified. Basic settings remain usable.','交付回执诊断暂不可用，待处理阶段仍未核实；基本设置仍可操作。'],
phaseBoundaryLimited:['Only a bounded sample was checked; these counts are not all pending stages.','仅检查有界样本；此数量不代表全部待处理阶段。'],
phaseBoundaryNote:['Read-only receipt diagnostics. A new task’s Stop cannot complete an older delivery; this view starts no review or retry.','这里只读核对回执；新任务的 Stop 不能补结旧交付，查看不会启动审核或重试。'],
phaseInputCost:['Visible review input: {n} characters','可见审核输入：{n} 字符'],
phaseInputCostUnknown:['Input composition was not recorded or cannot be verified for this batch.','本批未记录输入构成，或当前无法核验。'],
phaseInputCostParts:['Instructions {instructions} · source evidence {source_events} · task {task_context} · governance target {governance_target} · history {related_history} · formal knowledge {related_knowledge} · candidates {related_candidates} · shared context {shared_context} · packet metadata {packet_metadata}','指令 {instructions} · 来源证据 {source_events} · 任务 {task_context} · 治理目标 {governance_target} · 历史 {related_history} · 正式知识 {related_knowledge} · 候选 {related_candidates} · 共享内容 {shared_context} · 包元数据 {packet_metadata}'],
phaseInputCostSchema:['Response schema: {n} characters (separate from the visible instruction).','响应结构：{n} 字符（与可见指令分别计量）。'],
phaseInputCostSaved:['Existing lossless sharing removed {n} characters from this visible instruction.','现有无损共享为本次可见指令减少 {n} 字符。'],
phaseInputCostNote:['Frozen local character counts, not provider tokens or billing. Hidden provider overhead is excluded; viewing adds no model call.','这是冻结时的本地字符统计，不是平台 token 或计费量；不含提供方隐藏开销，查看不新增模型调用。']
});
function renderPhaseBoundary(root,diagnostic){
 if(!diagnostic)return;
 if(diagnostic.checked!==true){root.append(node('p',t('phaseBoundaryUnavailable'),'note'));return;}
 if(!(diagnostic.sampled>0))return;
 const detail=document.createElement('details'),title=document.createElement('summary');
 title.textContent=t('phaseBoundarySample',{n:diagnostic.sampled,limit:diagnostic.sample_limit});detail.append(title);
 const reasons={awaiting_host_stop:'phaseBoundaryWaiting',missing_host_stop_after_boundary:'phaseBoundaryMissing',awaiting_local_seal:'phaseBoundarySeal',local_seal_retry:'phaseBoundaryRetry',historical_boundary:'phaseBoundaryHistorical'};
 for(const item of (Array.isArray(diagnostic.items)?diagnostic.items:[]).slice(0,8)){
  const identity=t('phaseBoundaryItem',{id:String(item.phase_id??'?').slice(0,10),task:String(item.task_id??'?').slice(0,10),revision:item.task_revision??'?',delivery:item.delivery_version??'?'});
  detail.append(node('p',identity+' · '+t(reasons[item.reason]||'phaseBoundaryUnknown'),'note'));
 }
 if(diagnostic.truncated||diagnostic.unknown_count>0)detail.append(node('p',t('phaseBoundaryLimited'),'muted'));
 detail.append(node('p',t('phaseBoundaryNote'),'muted'));root.append(detail);
}
function renderPhaseInputCost(root,cost){
 if(!cost)return;
 if(cost.status!=='recorded'){root.append(node('p',t('phaseInputCostUnknown'),'muted'));return;}
 const detail=document.createElement('details'),title=document.createElement('summary');
 title.textContent=t('phaseInputCost',{n:cost.instruction_chars});detail.append(title);
 detail.append(node('p',t('phaseInputCostParts',cost.components||{}),'muted'));
 if(cost.schema_chars!==null&&cost.schema_chars!==undefined)detail.append(node('p',t('phaseInputCostSchema',{n:cost.schema_chars}),'muted'));
 if(cost.transport_saved_chars>0)detail.append(node('p',t('phaseInputCostSaved',{n:cost.transport_saved_chars}),'muted'));
 detail.append(node('p',t('phaseInputCostNote'),'note'));root.append(detail);
}
const basePhaseToolMetrics=phaseBatchMetrics;
phaseBatchMetrics=function(detail,batch){basePhaseToolMetrics(detail,batch);renderPhaseInputCost(detail,batch.metrics?.input_cost);const view=batch.metrics?.tool_context;if(!view)return;detail.append(node('p',t('phaseBatchTools',{n:view.selected_observations}),'note'));if(view.selection_limited)detail.append(node('p',t('phaseBatchToolsLimited'),'note'));};
renderAdminState=function(){basePhaseRecoveryRender();const view=state.adminData?.phase_review;if(!view)return;const area=$('phase-results');if(view.local_seal_pending_count>0)area.append(node('p',t('phaseSealPending',{n:view.local_seal_pending_count}),'note'));if(view.executor_waiting_batch_count>0)area.append(node('p',t('phaseExecutorWaiting',{n:view.executor_waiting_batch_count}),'note'));renderPhaseBoundary(area,view.delivery_boundary_diagnostics??state.adminData.queue?.phase_health?.delivery_boundary_diagnostics);};
'''
