"""Offline workload filters and provenance-preserving case groups."""

WORKLOAD_HTML = '''<section id="workload-tools" hidden>
<h2 data-i18n="workloadHeading">Daily review work</h2><p class="note" data-i18n="workloadNote">Historical text units are separate. A pending database row is not a queued model call.</p>
<div id="workload-counts" class="stats"></div><div class="filters">
<label for="workload-stage" data-i18n="workloadStage">Review stage</label><select id="workload-stage">
<option value="pending_review" data-i18n="stage_pending_review">Awaiting review</option><option value="all" data-i18n="allStates">All states</option>
<option value="reviewing" data-i18n="stage_reviewing">Reviewing</option><option value="awaiting_evidence" data-i18n="stage_awaiting_evidence">Awaiting evidence</option>
<option value="local_deferred" data-i18n="stage_local_deferred">Deferred locally</option><option value="awaiting_approval" data-i18n="stage_awaiting_approval">Awaiting approval</option>
<option value="blocked" data-i18n="stage_blocked">Needs inspection</option><option value="skipped" data-i18n="stage_skipped">Skipped</option><option value="completed" data-i18n="stage_completed">Completed</option></select>
<label for="workload-view" data-i18n="workloadView">Display</label><select id="workload-view"><option value="groups" data-i18n="workloadGroups">Related case groups</option><option value="cases" data-i18n="workloadCases">Individual cases</option></select>
<button id="workload-back" data-i18n="workloadBack" hidden>Back to groups</button></div>
<p id="workload-bound" class="note" hidden></p><p class="note" data-i18n="workloadGroupNote">Grouping suggests related reports; it does not establish a common cause or share approval and verification.</p></section>'''

WORKLOAD_JAVASCRIPT = r'''
Object.assign(strings,{
workloadHeading:['Daily review work','日常整理待办'],
workloadNote:['Historical text units are separate. A pending database row is not a queued model call. Awaiting review can still require tool completion, model readiness or budget.','历史文本单元单独统计；数据库待处理记录不等于模型排队调用。待审核仍可能等待工具完成、可用模型或预算。'],
workloadStage:['Review stage','审核阶段'],workloadView:['Display','显示方式'],
workloadGroups:['Related case groups','同类案例聚合'],workloadCases:['Individual cases','逐条案例'],workloadBack:['Back to groups','返回聚合列表'],
workloadGroupNote:['Grouping suggests related reports; it does not establish a common cause or share approval and verification.','聚合仅表示相似线索，不确认同一根因；各案例保留独立证据、审核与批准。'],
workloadMembers:['{count} cases · {host}','{count}条案例 · {host}'],workloadOpenGroup:['View cases in this group','查看本组案例'],
workloadMoreMembers:['Member preview only; open the group to page through its cases.','此处为成员预览；进入本组可分页查看案例。'],
workloadBound:['Grouping covers the first {sampled} of {total} cases in this filter. Switch to individual cases to page through all records.','聚合覆盖此筛选下前{sampled}条，共{total}条；切换逐条案例可分页查看全部记录。'],
workloadHistory:['Historical units awaiting review','历史待审文本单元'],workloadDaily:['Daily pending rows','日常未归档记录'],workloadSummaries:['Summary pending rows','摘要未归档记录'],
workloadStored:['All pending storage rows (includes history)','全部待处理存储记录（含历史）'],
workload_upgrade_required:['Workload stages are unavailable in this older database. Upgrade explicitly to view them; refreshing does not migrate it.','旧数据库暂不支持分类统计，请明确升级后查看；刷新不会迁移数据库。'],
stage_pending_review:['Awaiting review','待审核'],stage_reviewing:['Reviewing','正在审核'],stage_awaiting_evidence:['Reviewed: awaiting evidence','已审核待补证'],
stage_local_deferred:['Deferred locally: no model call','本地暂缓未调用模型'],stage_awaiting_approval:['Awaiting approval','等待批准'],
stage_blocked:['Failed / unknown: inspect before retry','失败或未知待核对'],stage_skipped:['Skipped','已跳过'],stage_completed:['Completed','已完成'],
adminQueueEligible:['Host scan candidates (before model / budget checks)','本宿主待检查（尚需模型与预算检查）']
});
let workloadGroup='';
const workloadStages=['pending_review','reviewing','awaiting_evidence','local_deferred','awaiting_approval','blocked','skipped','completed'];
function workloadCountRows(root,summary){
 root.replaceChildren();
 for(const stage of workloadStages){const card=node('div',undefined,'stat');card.append(node('span',t('stage_'+stage)),node('strong',summary.stages?.[stage]??'—'));root.append(card);}
 for(const [key,value]of [['workloadHistory',summary.history_pending_units],['workloadDaily',summary.daily_pending_rows],['workloadSummaries',summary.summary_pending_rows]]){const card=node('div',undefined,'stat');card.append(node('span',t(key)),node('strong',value??'—'));root.append(card);}
}
function workloadCaseCard(item){
 const card=node('article',undefined,'history-item');
 card.append(node('strong',item.summary),node('div',[t('stage_'+item.queue_state),t('caseRevision',{revision:item.revision}),label(item.review_state)].filter(Boolean).join(' · '),'tags'));
 card.append(node('p',item.review_reason||t('notReviewed')),node('div',stamp(item.created)+' · '+item.id,'muted'));
 card.append(node('div',item.source_host+' · '+item.conversation,'muted'));
 if(item.last_call)card.append(node('p',t('lastCall')+': '+label(item.last_call.state)+' · '+stamp(item.last_call.created),'muted'));
 for(const candidate of item.candidates||[]){const button=node('button',candidate.title+' · '+label(candidate.status),'card-title');button.addEventListener('click',()=>run(()=>showDetail('candidate',candidate.id)));card.append(button);}
 return card;
}
function renderWorkload(data){
 const root=$('list');root.replaceChildren();workloadCountRows($('workload-counts'),data.summary||{});
 $('workload-back').hidden=!workloadGroup;$('workload-view').disabled=state.busy||Boolean(workloadGroup);
 $('workload-bound').hidden=!data.groups_truncated;$('workload-bound').textContent=t('workloadBound',{sampled:data.sampled_cases,total:data.total_cases});
 for(const item of data.items){
  if(data.view==='cases'){root.append(workloadCaseCard(item));continue;}
  const card=node('article',undefined,'card');card.append(node('strong',item.summary),node('p',t('workloadMembers',{count:item.member_count,host:item.source_host}),'muted'));
  card.append(node('p',Object.entries(item.stages).map(([stage,count])=>t('stage_'+stage)+': '+count).join(' · ')));
  const details=node('details');details.append(node('summary',t('workloadCases')));for(const member of item.members||[])details.append(workloadCaseCard(member));card.append(details);
  if(item.members_truncated)card.append(node('p',t('workloadMoreMembers'),'note'));
  const open=node('button',t('workloadOpenGroup'));open.addEventListener('click',()=>run(async()=>{workloadGroup=item.group_id;state.offset=0;await loadList();}));card.append(open);root.append(card);
 }
 if(!data.items.length)root.append(node('p',t(data.summary?.available===false?'workload_upgrade_required':'empty'),'muted'));renderPager(data);renderSelection();
}
function renderWorkloadQueue(data){
 const root=$('admin-queue-details');root.replaceChildren();const workload=data.queue?.workload;
 if(workload){for(const stage of workloadStages)root.append(adminStatusRow('stage_'+stage,workload.stages?.[stage]));root.append(adminStatusRow('workloadHistory',workload.history_pending_units),adminStatusRow('workloadStored',workload.pending_total));}
 root.append(adminStatusRow('adminQueueEligible',data.queue?.eligible));
}
for(const id of ['workload-stage','workload-view'])$(id).addEventListener('change',()=>run(async()=>{workloadGroup='';state.offset=0;await loadList();}));
$('workload-back').addEventListener('click',()=>run(async()=>{workloadGroup='';state.offset=0;await loadList();}));
'''
