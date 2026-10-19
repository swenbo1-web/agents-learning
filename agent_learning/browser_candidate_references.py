"""Offline controls for optional references and model-free response rechecks."""

REFERENCE_HTML = '''<section id="candidate-reference-settings" class="admin-details">
<h3 data-i18n="candidateReferenceHeading">Unpublished candidate references</h3>
<p class="note" data-i18n="candidateReferenceNote">Opt in for this project. Eligible candidates supply a small unverified index; publication stays separate. Historical batches need their own permission.</p>
<p id="candidate-reference-state" role="status"></p><button id="candidate-reference-toggle" type="button" disabled data-i18n="candidateReferenceEnable">Enable candidate references</button>
</section>'''

REFERENCE_JAVASCRIPT = r'''
Object.assign(strings,{
candidateReferenceHeading:['Unpublished candidate references','未发布候选参考'],
candidateReferenceNote:['Project-wide opt-in. Eligible candidates provide at most one small unverified index within the existing context budget. This starts no model call and does not approve publication. Historical batches need separate permission.','项目级可选功能。符合条件的候选在现有上下文预算内最多提供一条未验证索引；不启动模型、不批准发布。历史批次另行授权。'],
candidateReferenceOn:['Candidate references are enabled for this project.','本项目已允许候选参考。'],
candidateReferenceOff:['Candidate references are disabled.','候选参考已关闭。'],
candidateReferenceEnable:['Enable candidate references','开启候选参考'],
candidateReferenceDisable:['Disable candidate references','关闭候选参考'],
candidateReferenceConfirm:['Change candidate-reference access for this project? Existing administrator, model and publication settings remain independent.','更改本项目候选参考开关？管理员、模型与发布设置保持独立。'],
candidateReferenceHistoryEnable:['Allow candidate references from this batch','允许本批候选参考'],
candidateReferenceHistoryDisable:['Revoke candidate references from this batch','撤回本批候选参考'],
candidateReferenceHistoryConfirm:['Change reference access only for this frozen historical batch? This does not scan, continue review or publish knowledge.','仅更改这一冻结历史批次的参考权限？不扫描、不继续审核、不发布知识。'],
candidateReferenceConcerns:['Unresolved candidate concerns: {n}','尚待核对的候选疑问：{n}'],
candidateReferenceResolve:['Resolve reference concerns…','核对并处理候选疑问…'],
candidateReferenceResolveConfirm:['Confirm that you reviewed these concerns? This reopens eligibility checks only, does not verify the candidate, and does not publish it.','确认已核对这些疑问？仅恢复参考资格检查，不将候选变为已验证，也不发布知识。'],
'equivalent candidate has unresolved concerns':['An equivalent candidate has unresolved concerns','等价候选存在未解决疑问'],
'equivalent candidate is held':['An equivalent candidate was rejected or deferred','等价候选已被拒绝或暂缓'],
candidate_reference_disputed:['Candidate concerns require explicit resolution','候选疑问需先明确处理'],
responseRecheck:['Revalidate saved response (no model call)','重新验收已保存答复（不调用模型）'],
responseRecheckConfirm:['Revalidate the saved public response against current sources and policies? This may commit eligible candidates and publication under existing permission. It makes no model call and preserves the original attempt and usage.','按当前来源和策略重新验收已保存的公开答复？可能按已有权限保存候选并发布合格知识；不调用模型，原调用和用量保留。'],
responseRejection:['Local rejection: {code} · {path}','本地拒绝原因：{code} · {path}'],
responseUnavailable:['This old attempt has no saved public response. A model-free recheck is unavailable.','此旧调用没有留存公开答复，无法本地重验。'],
'candidate reference source requires review':['Source may be influenced by disputed candidate references','来源可能受争议候选参考影响，需核对'],
'candidate reference requires independent source evidence':['Referenced candidate restatements require independent evidence','候选参考的复述需要独立来源证据'],
public_response_unavailable:['No saved public response','没有留存公开答复'],
history_run_reapproval_required:['This historical batch requires explicit permission handling; local revalidation cannot restore it or authorize a model rerun.','此历史批次需先明确处理批次权限；本地重验不能恢复权限，也不授权重新调用模型。'],
response_schema_invalid:['Invalid structured response','结构化答复不符合契约'],
candidate_batch_ambiguous:['Inconsistent multi-candidate decision','多候选决定与治理提议不一致'],
candidate_tool_success_missing:['No directly observed successful tool evidence','缺少直接观察到的工具成功证据'],
candidate_source_missing:['Source evidence is missing or outside this case','来源证据缺失或不属于当前案例'],
candidate_source_identity_changed:['Source task identity changed','来源任务身份已变化'],
candidate_task_snapshot_missing:['Source task snapshot is missing','缺少来源任务快照'],
local_decision_rejected:['Local acceptance checks rejected the response','本地验收检查未通过'],
phase_source_changed:['Stage sources or comparisons changed','阶段来源或比较资料已变化'],
phase_context_changed:['Stage review context changed','阶段审核上下文已变化'],
candidate_identity_changed:['Candidate source identity changed','候选来源身份已变化'],
candidate_evidence_outside_packet:['Candidate cites evidence outside the review packet','候选引用了送审范围以外的证据'],
review_evidence_changed:['Review sources or comparisons changed','审核来源或比较资料已变化']
});
const baseCandidateReferenceRender=renderAdminState;
renderAdminState=function(){baseCandidateReferenceRender();const policy=state.adminData?.candidate_reference_policy;
 const button=$('candidate-reference-toggle');if(!button)return;
 $('candidate-reference-state').textContent=policy?t(policy.enabled?'candidateReferenceOn':'candidateReferenceOff'):t('adminNoData');
 button.textContent=t(policy?.enabled?'candidateReferenceDisable':'candidateReferenceEnable');
 button.disabled=!policy||!canAdmin()||state.busy||Boolean(state.adminPending);
};
const baseCandidateReferenceControls=controls;
controls=function(){baseCandidateReferenceControls();const button=$('candidate-reference-toggle');if(button)button.disabled=!state.adminData?.candidate_reference_policy||!canAdmin()||state.busy||Boolean(state.adminPending);};
async function changeCandidateReference(job){
 if(!canAdmin()||state.adminPending)throw new Error(t('adminSaveBeforeWake'));
 const data=state.adminData,policy=job?job.candidate_reference_grant:data.candidate_reference_policy;if(!policy)return;
 if(!window.confirm(t(job?'candidateReferenceHistoryConfirm':'candidateReferenceConfirm')))return;
 const payload={request_id:newRequestId(),expected_session_epoch:data.session_epoch,expected_governance_epoch:data.governance_epoch,
 expected_version:policy.version,enabled:!policy.enabled};if(job)payload.job_id=job.id;
 await sendAdminRequest('admin-control/candidate-reference',payload);
}
$('candidate-reference-toggle').addEventListener('click',()=>run(()=>changeCandidateReference()));
const baseCandidateHistoryControls=addHistoryReviewControls;
addHistoryReviewControls=function(card,item){baseCandidateHistoryControls(card,item);if(!item.candidate_reference_grant)return;
 const grant=item.candidate_reference_grant,button=node('button',t(grant.enabled?'candidateReferenceHistoryDisable':'candidateReferenceHistoryEnable'));
 button.disabled=!canAdmin()||state.busy||Boolean(state.adminPending);
 button.addEventListener('click',()=>run(()=>changeCandidateReference(item)));card.append(button);
};
async function recheckSavedResponse(item){
 if(!canAdmin()||state.adminPending||state.adminDirty)throw new Error(t('adminSaveBeforeWake'));
 if(!item.saved_response?.can_recheck||!window.confirm(t('responseRecheckConfirm')))return;
 const data=state.adminData;
 await sendAdminRequest('admin-control/response-recheck',{request_id:newRequestId(),call_id:item.id,
 expected_version:item.saved_response.version,expected_session_epoch:data.session_epoch,
 expected_governance_epoch:data.governance_epoch,confirmed:true});
}
const baseCandidateReferenceDetail=renderDetail;
renderDetail=function(){baseCandidateReferenceDetail();if(state.detail?.kind!=='candidate')return;
 const concern=state.detailData?.candidate_reference_feedback;if(!concern?.unresolved)return;
 const area=$('detail-context');area.append(node('p',t('candidateReferenceConcerns',{n:concern.unresolved}),'note'));
 for(const entry of concern.items||[])area.append(node('p',entry.summary,'muted'));
 if(canAdmin()&&!concern.truncated){const button=node('button',t('candidateReferenceResolve'));button.disabled=state.busy||Boolean(state.adminPending);
 button.addEventListener('click',()=>run(async()=>{
  if(!window.confirm(t('candidateReferenceResolveConfirm')))return;const reason=window.prompt(t('recoveryReason'),'');if(!reason?.trim())return;
  if(!state.adminData)await loadAdminCore();const base=state.adminData;if(!base)throw new Error(t('adminNoData'));
  await sendAdminRequest('admin-control/candidate-reference',{request_id:newRequestId(),candidate_id:state.detail.id,
   expected_version:concern.version,reason:reason.trim(),enabled:true,expected_session_epoch:base.session_epoch,expected_governance_epoch:base.governance_epoch});
  await loadField();
 }));area.append(button);}
};
'''
