"""Offline presentation of review subjects, evidence and explicit history work."""

FLOW_HTML = '<section id="admin-flow" class="admin-details" aria-live="polite"></section>'

FLOW_JAVASCRIPT = r'''
const historyReviewDrafts=new Map();
let adminHistoryPage=null,adminHistoryError='',adminHistoryOffset=0;
let adminHistoryUpdated=0,adminHistoryReadOffset=null,adminHistoryInFlight=null,adminHistorySerial=0;
Object.assign(strings,{
flowHistoryUnavailable:['History details are temporarily unavailable: {message}. Administrator settings remain available.','历史详情暂不可用：{message}。管理员设置仍可查看。'],
flowHistoryReload:['Refresh history','刷新历史进度'],
flowHistoryPrevious:['Previous batches','上一页批次'],flowHistoryNext:['Next batches','下一页批次'],
flowHistoryPhases:['Unreviewed {ready} · needs evidence {evidence} · blocked {blocked} · awaiting approval {approval}','待主审{ready} · 待补证{evidence} · 待核对{blocked} · 待批准{approval}'],
flowHistoryChanges:['New or changed sources {changed} · reused sources {reused} (context only)','新增或变化来源{changed} · 复用来源{reused}（仅作上下文）'],
flowHistoryDefer:['Defer this batch…','暂存本批主审…'],
flowHistorySelected:['Selected main-review batch','当前优先批次'],
flowHistoryBlockedBy:['Continue or explicitly defer batch {id} before reviewing this batch.','请先继续或明确暂存批次{id}，再整理本批。'],
flowHistoryDeferConfirm:['Defer this main review to allow a later incremental scan? Records and pending approvals remain. This does not retry calls or publish knowledge.','暂存本批主审，允许后续检测增量？保留原始记录和待批准内容；此操作不重试调用、不发布知识。'],
flowHistoryDeferred:['Main review deferred by the user; unresolved work is retained.','用户已暂存主审，未处理内容仍保留。'],
flowHeading:['Review subjects and publication','审核内容与发布去向'],
flowNote:['Read visible records and evidence only; this is not model internal reasoning. Refreshing makes no model call.','展示可见记录与证据，不代表读取模型内部思考；刷新不调用模型。'],
flowUpcoming:['Next daily cases (up to 5)','接下来可审的日常案例（最多5项）'],
flowRecent:['Current and recent reviews (up to 8)','正在进行与最近审核（最多8项）'],
flowLocallyAccepted:['Local revalidation accepted this review without a new model call. Original execution record: {state}; original usage is preserved.','此审核已通过本地重验，未新增模型调用。原执行记录：{state}；原用量保留。'],
adminLocallyRecovered:['Of these, locally recovered','其中本地恢复完成'],
flowDeferred:['Local checks: no model call','本地待补证：未调用模型'],
flowNone:['No records in this view.','此范围暂无记录。'],
flowPurpose:['Purpose: {purpose}','审核用途：{purpose}'],
flowEvidence:['Current case evidence: {events} records · {successes} tool successes · {failures} failures · {statements} public statements','当前案例依据：{events}条记录 · {successes}次工具成功 · {failures}次失败 · {statements}条公开说明'],
flowRevision:['Reviewed revision {reviewed} · current revision {current}','审核版本{reviewed} · 当前版本{current}'],
flowCaseChanged:['This case changed after the review. The decision below belongs to its earlier revision.','案例在审核后已有变化，下方结论对应旧版本。'],
flowMissing:['Missing evidence: {value}','待补充：{value}'],
flowSource:['Source host: {host} · {category}','资料宿主：{host} · {category}'],
flowApplies:['Applies when: {value}','适用条件：{value}'],
flowLimits:['Limits: {value}','限制：{value}'],
flowHistoryAccess:['Reconfirm this archived batch before reading its review text.','请先重新确认此历史批次，才能读取审核正文。'],
flowPublication:['Publication: {value}','发布去向：{value}'],
flowPreview:['Check publication conditions (read only)','检查发布条件（只读）'],
flowPreviewResult:['{title}: {reason}. Existing candidates still need explicit approval.','{title}：{reason}。现有候选仍需明确批准。'],
flowCandidates:['Candidates from this review','本次审核生成的候选'],
flowCandidatePreview:['Summary preview; open candidate review for full text and approval.','此处为摘要预览；完整内容与批准请进入候选审批列表。'],
flowCandidateEvidence:['Evidence: {level} · references: {refs}','证据等级：{level} · 引用：{refs}'],
flowCandidatesMore:['More candidates are available in the approval view.','其余候选请在审批列表查看。'],
flowHistoryResume:['Continue this archived batch…','继续整理此归档批次…'],
flowHistoryInterrupted:['This batch needs explicit continuation. Calls with unknown results remain blocked until checked.','此批次需要明确续做；结果未知的调用仍需核对，不会自动重试。'],
superseded:['Previous account or governance request (inactive)','旧账户或治理轮次请求（已失效）'],
flowPublicationOn:['Future verified operation solutions may publish automatically','后续通过验证的操作解法可自动发布'],
flowPublicationTiered:['Tiered publication: verified solutions and sourced references; disputes await review','分级发布：验证解法与带来源参考自动进入知识库；争议转人工'],
publicationMode:['Publication policy','自动发布范围'],
tiered:['Tiered publication (recommended)','分级自动发布（推荐）'],
verified_operation_solutions:['Verified operation solutions only (legacy)','仅验证操作解法（原策略）'],
source_reference:['Sourced reference · not verified','带来源参考 · 尚未验证'],
knowledgeDisputed:['{count} unresolved counterexample(s). Automatic injection is suspended; compare evidence and explicitly revise or revoke.','存在{count}条未解决反例，已暂停自动注入；需比较证据后明确修订或撤销。'],
knowledgeReferenceNote:['A sourced statement, not verification of the current implementation. Check scope, source revision and limits before reuse.','这是带来源的陈述，不代表当前实现已验证；复用前核对适用范围、来源版本与限制。'],
tool_verified:['Observed tool success · not user acceptance','工具成功依据 · 不等于用户验收'],
partially_published:['Partially published; remaining candidates need review','已部分发布，其余候选待审核'],
publication_mode_changed:['Publication policy changed during this review','审核期间自动发布范围已变化'],
flowHistoryReferences:['Allow sourced references from this batch to publish under the tiered policy (still reported, not verified)','允许本批资料按分级策略发布带来源参考（保留reported，不算验证结论）'],
flowHistoryReferenceConfirm:[' Allow sourced references from this batch to publish automatically? Evidence stays reported; disputes and incomplete sources remain for manual review.',' 允许本批资料自动发布带来源参考？证据仍为reported，冲突或来源不完整的内容留待人工审核。'],

flowPublicationOff:['Automatic publication is off; candidates await approval','自动发布关闭；候选等待批准'],
autoPublishLabel:['Automatically publish eligible future knowledge for this project and host','为此项目、此宿主自动发布后续符合条件的知识'],
autoPublishNote:['Tiered: verified operation solutions publish with current evidence; sourced project facts publish as unverified references. Historical references require permission for each explicitly started batch. Disputed, stale or incomplete evidence stays for review. Existing settings and pending candidates are preserved.','分级策略：验证操作解法需当前证据；有来源的项目事实以未验证参考发布。历史参考需在每次主动整理批次时授权。争议、过时或不完整依据转人工；保留原设置与已有待批候选。'],
autoPublishConfirm:['Enable the selected publication policy for future reviews in this project and host? Source references remain unverified. Disputes and existing candidates require review; history needs per-batch permission.','为此项目、此宿主的后续审核开启所选发布策略？带来源参考仍属未验证；争议与已有候选需审核，历史资料需逐批授权。'],
flowHistoryCounts:['Archived cases {cases} · reviewed {reviewed} · candidates {candidates} · published {published}','已归档案例{cases} · 已主审{reviewed} · 候选{candidates} · 已发布{published}'],
flowHistoryStart:['Review this archived batch…','整理此归档批次…'],
flowHistoryCalls:['Calls (1–20)','调用上限（1–20）'],
flowHistorySeconds:['Seconds (1–600)','秒数上限（1–600）'],
flowHistoryConfirm:['Review only this already archived batch using the configured model, up to {calls} calls and {seconds} seconds? This uses platform quota and does not scan new sources. Without batch permission, candidates remain for approval.','使用已配置模型，仅整理此已归档批次，最多{calls}次调用、{seconds}秒？会使用平台额度；不扩大来源；未授权自动参考时，候选留待批准。'],
flowHistoryQueued:['History review was requested for this batch. Follow progress and publication receipts below.','已请求整理此历史批次，可在下方查看进度和发布回执。'],
flowHistoryRequest:['Review request: {state}','整理请求：{state}'],
awaiting_recovery_evidence:['Waiting for recovery or an explicit explanation; deferred locally without a model call','等待恢复结果或明确说明；已在本地暂缓，未调用模型'],
recovery_result_or_explanation:['A linked successful verification or an explanation of the reusable conclusion','关联的成功验证，或可复用结论的明确说明'],
daily_review:['Daily case review','日常案例审核'],history_review:['Archived project review','历史资料整理'],work_summary:['Public conversation summary','公开对话摘要'],
automatic_publication_disabled:['Automatic publication is disabled','自动发布未开启'],
publication_not_enabled_at_reservation:['Publication was not enabled when this review started','此审核开始时未开启自动发布'],
history_requires_approval:['Historical knowledge requires explicit approval','历史知识需明确批准'],
work_summary_not_published:['Work summary only; not formal knowledge','仅为工作摘要，不作为正式知识发布'],
candidate_not_pending:['Candidate is not awaiting approval','候选当前不在待审批状态'],
check_equivalent_knowledge_revoked:['Equivalent knowledge was revoked; use its explicit restoration workflow','等价知识已被撤销，请使用对应的显式恢复治理入口'],
check_equivalent_knowledge_superseded:['Equivalent knowledge was superseded; compare its current revision before publication','等价旧知识已被替换，请先比较当前修订再决定发布'],
check_equivalent_knowledge_disputed:['Equivalent knowledge has unresolved counterexamples','等价知识存在未处置反例，请先完成治理'],
check_equivalent_knowledge_limit:['Too many equivalent revisions to compare safely; review the exact knowledge target','等价修订超过安全比较范围，请明确目标知识后治理'],
check_equivalent_candidate_disputed:['An equivalent candidate has unresolved concerns','等价候选存在未解决疑问，请先明确处理'],
check_candidate_comparison_limit:['Candidate comparisons reached their bound; resolve the exact candidate before publication','候选比较达到上限，请明确核对目标候选后再发布'],
candidate_evidence_changed:['Evidence changed; review the current case first','依据已变化，请先重新审核当前案例'],
eligible_future_review_only:['Evidence meets the narrow automatic publication checks','证据满足窄范围自动发布检查'],
'evidence truncated':['Evidence is incomplete or truncated','证据不完整或被截断'],
'requires trusted tool-success evidence':['Requires verified tool success','需要受信工具成功证据'],
'latest observation is unresolved failure':['A later failure remains unresolved','后续失败尚未解决'],
'requires current cited tool-success evidence':['The candidate must cite current successful verification','候选必须实际引用当前成功验证'],
'requires operation solution':['Only operation solutions qualify','仅操作类解法可自动发布'],
'target knowledge requires trusted governance':['The target requires explicit governance approval','目标知识需明确治理批准'],
'existing knowledge requires trusted governance':['The existing knowledge requires explicit governance approval','已有知识需明确治理批准'],
'requires sourced project fact':['Requires a sourced project fact; other daily conclusions need review','需要有来源的项目事实；其他日常结论仍需审核'],
'reference requires scope and limitations':['Reference needs explicit scope and limitations','参考需有明确适用范围与限制'],
'reference source is unhealthy':['Reference source failed or is disputed','参考来源失败或存在冲突'],
'reference requires archived provenance':['Reference requires frozen source identity and content hash','参考需有冻结来源身份与内容指纹'],
'reference requires public source evidence':['Requires an observable public source, not only tool results','需有可观察的公开来源，不能只依据工具流水'],
'equivalent knowledge was revoked':['Equivalent knowledge was revoked; explicit review required','相同知识已撤销，需明确复核'],
'previous knowledge title requires review':['A prior entry with this title was revoked; compare before publication','同名旧知识已撤销，发布前需比较复核'],
'equivalent knowledge was superseded':['Equivalent knowledge was replaced by a newer revision','相同知识已有新修订，不自动回写旧版'],
'unresolved knowledge counterexample':['Related knowledge has an unresolved counterexample','关联知识存在未解决反例'],
'conflicting knowledge requires review':['Same-title knowledge differs; compare and review before publication','同名知识内容不同，需比较审核后发布'],
some_references_require_review:['Some references still need manual review','部分参考仍需人工审核'],
local_preflight:['Local evidence check','本地证据检查'],result_unknown:['Result unknown; inspect before retrying','结果未知，重试前需核对']
});
function knowledgeNotice(target,item){
 if(item.pending_counterexample_count)target.append(node('p',t('knowledgeDisputed',{count:item.pending_counterexample_count}),'note'));
 if(item.knowledge_tier==='source_reference')target.append(node('p',t('knowledgeReferenceNote'),'note'));
}
function renderFlowCandidate(card,item){
 const section=node('section',undefined,'history-item');section.append(node('strong',item.title||item.candidate_id));
 section.append(node('p',item.candidate_id+' · '+label(item.status),'muted'));
 if(item.content_summary)section.append(node('p',item.content_summary));
 if(item.applicability)section.append(node('p',t('flowApplies',{value:item.applicability})));
 if(item.limitations)section.append(node('p',t('flowLimits',{value:item.limitations}),'note'));
 section.append(node('p',t('flowCandidatePreview'),'note'));
 if(item.validation_level)section.append(node('p',t('flowCandidateEvidence',{level:label(item.validation_level),refs:(item.evidence_refs||[]).join(', ')+(item.evidence_refs_truncated?' …':'')}),'muted'));
 if(item.publication)section.append(node('p',t('flowPublication',{value:label(item.publication.status)+(item.publication.knowledge_id?' · '+item.publication.knowledge_id+'@'+item.publication.revision:'')})));
 if(item.status==='candidate'||item.status==='deferred'){const button=node('button',t('flowPreview'));button.disabled=state.busy||Boolean(state.adminPending);button.addEventListener('click',()=>run(async()=>{const result=await api('admin-control/publication-preview',{candidate_id:item.candidate_id});section.append(node('p',t('flowPreviewResult',{title:result.title,reason:label(result.reason)}),'note'));}));section.append(button);}
 card.append(section);
}
function renderAdminFlow(data){
 const root=$('admin-flow');if(!root)return;root.replaceChildren();
 root.append(node('h3',t('flowHeading')),node('p',t('flowNote'),'note'));
 root.append(node('strong',t(data.publication_policy?.enabled?(data.publication_policy?.mode==='tiered'?'flowPublicationTiered':'flowPublicationOn'):'flowPublicationOff')));
 if(!renderAdminReadNotice(root,'activity'))return;
 const activity=data.review_activity||{};
 for(const [heading,key]of [['flowRecent','recent'],['flowUpcoming','upcoming'],['flowDeferred','deferred']]){
  root.append(node('h4',t(heading)));const items=activity[key]||[];
  if(!items.length)root.append(node('p',t('flowNone'),'muted'));
  for(const item of items){
   const card=node('article',undefined,'history-item');card.append(node('strong',item.title||item.problem_id));
   card.append(node('p',t('flowPurpose',{purpose:label(item.purpose)})+' · '+item.problem_id+(item.state?' · '+label(item.state):''),'muted'));
   if(item.local_recheck)card.append(node('p',t('flowLocallyAccepted',{state:label(item.execution_state)}),'note'));
   if(item.reviewed_revision!==undefined)card.append(node('p',t('flowRevision',{reviewed:item.reviewed_revision,current:item.revision}),'muted'));
   if(item.case_changed_since_review)card.append(node('p',t('flowCaseChanged'),'note'));
   card.append(node('p',t('flowSource',{host:item.source_host||t('unknown'),category:label(item.category)}),'muted'));
   if(item.summary)card.append(node('p',item.summary));
   if(item.source_file)card.append(node('p',item.source_file));
   if(item.files?.length)card.append(node('p',item.files.join(' · '),'muted'));
   const evidence=item.evidence||{};card.append(node('p',t('flowEvidence',{events:evidence.events??0,successes:evidence.successes??0,failures:evidence.failures??0,statements:evidence.public_statements??0})));
   if(item.reason)card.append(node('p',label(item.reason)));
   for(const missing of item.missing_evidence||[])card.append(node('p',t('flowMissing',{value:label(missing)}),'note'));
   if(!item.history_access_required&&item.candidates?.length){card.append(node('h5',t('flowCandidates')));for(const candidate of item.candidates)renderFlowCandidate(card,candidate);}
   else if(!item.history_access_required){if(item.candidate_title)card.append(node('p',item.candidate_title));if(item.candidate_summary)card.append(node('p',item.candidate_summary));if(item.applicability)card.append(node('p',t('flowApplies',{value:item.applicability})));if(item.limitations)card.append(node('p',t('flowLimits',{value:item.limitations}),'note'));}
   if(item.candidates_truncated)card.append(node('p',t('flowCandidatesMore'),'note'));
   if(item.history_access_required)card.append(node('p',t('flowHistoryAccess'),'note'));
   if(item.publication)card.append(node('p',t('flowPublication',{value:label(item.publication.reason||item.publication.status)})));
   root.append(card);
  }
 }
}
function addHistoryReviewControls(card,item){
 if(item.selected)card.append(node('strong',t('flowHistorySelected')));
 if(item.blocked_by_batch)card.append(node('p',t('flowHistoryBlockedBy',{id:item.blocked_by_batch}),'note'));
 const counts=item.counts||{};card.append(node('p',t('flowHistoryCounts',{cases:counts.cases??0,reviewed:counts.reviewed_cases??0,candidates:counts.candidates??0,published:counts.published_candidates??0})));
 const phases=item.coverage?.review||item.review_progress,publication=item.coverage?.publication||{};
 if(phases)card.append(node('p',t('flowHistoryPhases',{ready:phases.ready_cases,evidence:phases.awaiting_evidence_cases,blocked:phases.blocked_cases,approval:publication.awaiting_approval??item.awaiting_approval??0})));
 if(item.changes)card.append(node('p',t('flowHistoryChanges',{changed:item.changes.new_or_changed_sources,reused:item.changes.context_only_sources??0}),'note'));
 if(item.workflow?.deferred)card.append(node('p',t('flowHistoryDeferred'),'note'));
 if(item.can_defer){const defer=node('button',t('flowHistoryDefer'));defer.disabled=state.busy||Boolean(state.adminPending)||state.adminDirty;defer.addEventListener('click',()=>run(()=>confirmAdminRecovery('defer_history',item)));card.append(defer);}
 if(item.review_request)card.append(node('p',t('flowHistoryRequest',{state:label(item.review_request.state)}),'note'));
 if(item.resume_required)card.append(node('p',t('flowHistoryInterrupted'),'note'));
 if(!item.can_review)return;
 for(const [field,key,value,max]of [['calls','flowHistoryCalls',5,20],['seconds','flowHistorySeconds',180,600]]){
  const id='history-review-'+field+'-'+item.id,input=node('input'),caption=node('label',t(key)),draftKey=state.epoch+':'+id;caption.htmlFor=id;input.id=id;input.type='number';input.min='1';input.max=String(max);input.step='1';input.value=historyReviewDrafts.get(draftKey)??String(value);input.addEventListener('input',()=>historyReviewDrafts.set(draftKey,input.value));card.append(caption,input);
 }
 if(state.adminData?.publication_policy?.enabled&&state.adminData.publication_policy.mode==='tiered'){const id='history-review-references-'+item.id,check=node('input'),caption=node('label'),draftKey=state.epoch+':'+id;check.id=id;check.type='checkbox';check.checked=historyReviewDrafts.get(draftKey)??true;check.addEventListener('change',()=>historyReviewDrafts.set(draftKey,check.checked));caption.append(check,node('span',t('flowHistoryReferences')));card.append(caption);}
 const button=node('button',t(item.resume_required?'flowHistoryResume':'flowHistoryStart'));button.disabled=state.busy||Boolean(state.adminPending)||state.adminDirty;button.addEventListener('click',()=>run(()=>confirmHistoryReview(item)));card.append(button);
}
async function confirmHistoryReview(item){
 if(!canAdmin()||state.adminDirty||state.adminPending)throw new Error(t('adminSaveBeforeWake'));
 const calls=Number($('history-review-calls-'+item.id).value),seconds=Number($('history-review-seconds-'+item.id).value);
 if(!Number.isInteger(calls)||calls<1||calls>20||!Number.isInteger(seconds)||seconds<1||seconds>600)throw new Error(t('invalidResponse'));
 const autoReference=Boolean($('history-review-references-'+item.id)?.checked);
 if(!window.confirm(t('flowHistoryConfirm',{calls,seconds})+(autoReference?t('flowHistoryReferenceConfirm'):'')))return;
 const base=state.adminData;
 return sendAdminRequest('admin-control/history-review',{...adminExpected(base),job_id:item.id,version:item.version,
  confirmed:true,max_calls:calls,max_seconds:seconds,auto_reference:autoReference});
}
async function saveAdminConfiguration(action){
 const payload=adminConfiguration(action);
 if(payload.auto_publish&&(!state.adminData?.publication_policy?.enabled||payload.publication_mode!==state.adminData?.publication_policy?.mode)&&!window.confirm(t('autoPublishConfirm')))return;
 return sendAdminRequest('admin-control/configure',payload);
}
async function loadAdminHistory(force=true){
 const epoch=state.epoch,offset=adminHistoryOffset;
 const active=adminHistoryPage?.items?.some(item=>['materializing','reviewing'].includes(item.state)||item.review_request?.current&&['reserved','running'].includes(item.review_request.state));
 const interval=active?5000:30000;
 if(!force&&adminHistoryReadOffset===offset&&Date.now()-adminHistoryUpdated<interval)return;
 if(adminHistoryInFlight?.epoch===epoch&&adminHistoryInFlight.offset===offset)return adminHistoryInFlight.promise;
 const request={epoch,offset,serial:++adminHistorySerial};
 request.promise=(async()=>{
  const current=()=>epoch===state.epoch&&offset===adminHistoryOffset&&request.serial===adminHistorySerial;
  try{const page=await api('admin-control/history',{offset,limit:3});if(!current())return;adminHistoryPage=page;adminHistoryError='';adminHistoryUpdated=Date.now();adminHistoryReadOffset=offset;}
  catch(error){if(!current())return;adminHistoryError=error;adminHistoryUpdated=Date.now();adminHistoryReadOffset=offset;}
  finally{if(adminHistoryInFlight===request)adminHistoryInFlight=null;}
  if(current()&&state.adminData)renderAdminRecovery(state.adminData);
 })();
 adminHistoryInFlight=request;return request.promise;
}
'''
