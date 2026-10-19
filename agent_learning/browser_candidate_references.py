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
historyResponseRecheck:['Accept this batch’s saved response (no model call)','验收本批已保存答复（不调用模型）'],
historyResponseReconfirm:['Reconfirm access to this result (no model call)','重新确认本次结果访问（不调用模型）'],
historyResponseConfirm:['Accept only this call’s frozen sources and saved public response under current checks? This does not scan, continue historical review, call a model, change the original usage, or unlock model retries. Publication is disabled unless separately selected below.','仅按当前校验验收本次调用的冻结资料与已保存公开答复？不扫描、不续审、不调用模型、不修改原用量或解锁模型重试。未单独勾选时不自动发布。'],
historyResponseReconfirmConfirm:['Reconfirm access only to this call’s already accepted results after checking their frozen sources? This does not create candidates, publish knowledge, resume review, call a model, or change the original usage.','核对冻结来源后，仅重新确认本次调用已验收结果的访问？不生成候选、不发布知识、不续审、不调用模型，也不修改原用量。'],
historyResponseBoundaryConfirm:['The batch has these explicit boundaries: {reasons}. Confirm one local operation on this call only? The batch stays paused, cancelled or deferred; account and governance permissions are not restored.','此批次存在以下显式边界：{reasons}。确认仅为本次调用执行一次本地处理？不恢复批次运行，不解除暂停、取消或暂存，不恢复旧账户／治理权限。'],
historyResponseReason:['Explain why you are authorizing this one local operation (required).','请说明授权本次本地处理的原因（必填）。'],
historyResponsePublication:['Also allow publication within the original batch permission and current saved policy (off by default)','同时允许按原批次授权与当前已保存策略发布（默认不勾选）'],
historyResponsePublicationConfirm:['Also allow eligible knowledge to publish within both the original batch’s publication scope and the current saved policy? This cannot expand the old permission or restore revoked knowledge.','额外允许合格知识按原批次发布范围与当前已保存策略的共同范围发布？不扩大旧授权，也不恢复已撤销知识。'],
historyResponseAccepted:['Local acceptance completed; no model call was started.','本地验收完成，未启动模型调用。'],
historyResponseRejected:['Local checks did not accept this saved response. Inspect the reason below; no model call was started.','本地校验未通过，请查看下方原因；未启动模型调用。'],
historyResponseAccessConfirmed:['Access to the existing results was reconfirmed; no new review or publication was started.','已重新确认已有结果访问，未启动新审核或发布。'],
account_boundary:['Login account changed','登录账户边界已变化'],
governance_boundary:['Governance authorization changed','治理授权边界已变化'],
history_paused:['The batch was explicitly paused','批次已被明确暂停'],
history_cancelled:['The batch was cancelled','批次已取消'],
history_deferred:['The batch was deferred','批次已暂存'],
history_interrupted:['The previous batch owner was interrupted','原批次执行器已中断'],
history_superseded:['A newer historical batch exists for these sources','这些来源已有更新的历史批次'],
history_run_active:['Wait for the current batch owner to finish. Local acceptance is unavailable while it is running.','批次仍有有效执行器，请待其结束后再本地验收。'],
history_snapshot_unavailable:['This old call has no complete frozen recovery snapshot; local acceptance is unavailable.','此旧调用没有完整冻结恢复快照，无法本地验收。'],
history_snapshot_changed:['Frozen sources or comparison versions changed; this saved response cannot be accepted.','冻结来源或比较版本已变化，不能验收此已保存答复。'],
history_comparison_changed:['Comparison knowledge changed; inspect the current versions before another action.','比较知识已变化，请先核对当前版本。'],
history_terminal_unconfirmed:['The model attempt has no confirmed successful terminal response; local acceptance is unavailable.','原模型调用缺少已确认的成功结束答复，不能本地验收。'],
invalid_explicit_history_response_recheck:['The local acceptance request is incomplete or invalid. Refresh this page and confirm again.','本地验收参数不完整或无效，请刷新页面后重新确认。'],
history_local_job_version_changed:['The historical batch changed. Refresh its status before confirming again.','历史批次已变化，请刷新状态后再确认。'],
history_local_boundary_acknowledgement_required:['Explicitly confirm the historical boundary and enter a reason.','请明确确认历史边界并填写原因。'],
history_local_publication_not_authorized:['This request cannot publish under the original and current permissions. Leave publication unchecked.','原授权与当前权限不允许本次发布，请取消勾选发布。'],
history_local_work_active:['Other administrator work is active. Wait before local acceptance.','其他管理员工作仍在运行，请稍后本地验收。'],
history_local_request_identity_changed:['This request ID was reused with different parameters. Preserve the original request receipt.','同一请求ID的参数发生变化，请保留原请求回执。'],
history_local_request_running_or_interrupted:['This local request is running or was interrupted. Retrieve its receipt before another request.','此本地请求正在运行或曾中断，请先取回本次回执。'],
history_local_governance_not_supported:['Saved governance proposals require their explicit governance workflow.','已保存的治理提议需要通过显式治理流程处理。'],
history_local_acceptance_epoch_changed:['Account or governance permissions changed during acceptance. Refresh the project connection.','验收过程中账户或治理权限已变化，请刷新项目连接。'],
history_local_acceptance_job_changed:['The historical batch changed during acceptance; no result was committed.','验收过程中历史批次已变化，本次未提交结果。'],
history_local_acceptance_lease_changed:['The local acceptance lease changed or expired. Inspect the existing receipt.','本地验收租约已变化或过期，请核对现有回执。'],
history_local_acceptance_identity_changed:['The frozen call identity changed; inspect the stored response.','冻结调用身份已变化，请核对已保存答复。'],
history_local_acceptance_concurrent_model:['A model review started concurrently; local acceptance stopped.','同时出现新的模型审核，本地验收已停止。'],
history_local_acceptance_publication_not_authorized:['Publication authorization changed; local acceptance cannot publish.','发布授权已变化，本地验收不能发布。'],
history_local_acceptance_candidate_outside_reply:['A candidate was outside this saved response; local acceptance stopped.','候选超出本次已保存答复范围，本地验收已停止。'],
history_local_acceptance_cannot_run_models:['This local acceptance operation cannot start model calls.','本地验收操作不能启动模型调用。'],
history_local_acceptance_operation_unavailable:['This operation is unavailable within the frozen response workflow.','冻结答复流程不允许此操作。'],
history_local_acceptance_scope_unavailable:['The local acceptance scope is unavailable; inspect the existing receipt.','本地验收范围不可用，请核对现有回执。'],
history_recovered_artifact_changed:['Previously accepted candidate content changed; inspect its current version.','此前验收的候选正文已变化，请核对当前版本。'],
history_recovered_artifact_authorization_changed:['The recovered result access changed; explicitly reconfirm this result.','已恢复结果的访问授权已变化，请明确重新确认本次结果。'],
history_recovered_artifact_epoch_changed:['The recovered result belongs to an earlier authorization boundary; reconfirm access explicitly.','已恢复结果属于旧授权边界，请明确重新确认访问。'],
history_recovered_artifact_identity_changed:['The accepted result identity changed; inspect its current source.','已验收结果身份已变化，请核对当前来源。'],
history_recovered_artifact_receipt_missing:['The accepted result receipt is unavailable; preserve the records and inspect this call.','已验收结果缺少可用回执，请保留记录并核对本次调用。'],
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
for(const code of Object.keys(strings).filter(key=>key.startsWith('history_')||key==='invalid_explicit_history_response_recheck')){
 browserErrorCodes.add(code);legacyBrowserErrors.set(code,code);
}
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
function addHistoryResponseControl(card,item){
 const recovery=item.saved_response?.history_recovery;if(!recovery)return;
 if(!recovery.available){if(recovery.reason)card.append(node('p',label(recovery.reason),'note'));return;}
 const accessOnly=recovery.mode==='reconfirm_access'||recovery.access_reconfirmation===true;let publication=null;
 if(!accessOnly&&recovery.can_allow_publication&&recovery.original_publication_authorized){
  const option=node('label');publication=node('input');publication.type='checkbox';publication.checked=false;
  publication.disabled=!canAdmin()||state.busy||Boolean(state.adminPending);
  option.append(publication,node('span',t('historyResponsePublication')));card.append(option);
 }
 const button=node('button',t(accessOnly?'historyResponseReconfirm':'historyResponseRecheck'));
 button.disabled=!canAdmin()||state.busy||Boolean(state.adminPending);
 button.addEventListener('click',()=>run(()=>recheckHistorySavedResponse(item,Boolean(publication?.checked))));card.append(button);
}
async function recheckHistorySavedResponse(item,allowPublication=false){
 if(!canAdmin()||state.adminPending)throw new Error(t('adminSaveBeforeWake'));
 const recovery=item.saved_response?.history_recovery;if(!recovery?.available)return;
 const accessOnly=recovery.mode==='reconfirm_access'||recovery.access_reconfirmation===true;
 if(!window.confirm(t(accessOnly?'historyResponseReconfirmConfirm':'historyResponseConfirm')))return;
 if(recovery.ack_required&&!window.confirm(t('historyResponseBoundaryConfirm',{reasons:(recovery.boundary_reasons||[]).map(label).join(' · ')})))return;
 const reason=window.prompt(t('historyResponseReason'),'');if(!reason?.trim())return;
 const publish=!accessOnly&&allowPublication===true&&recovery.can_allow_publication===true&&recovery.original_publication_authorized===true;
 if(publish&&!window.confirm(t('historyResponsePublicationConfirm')))return;
 const data=state.adminData;
 await sendAdminRequest('admin-control/history-response-recheck',{request_id:newRequestId(),call_id:item.id,
  expected_version:item.saved_response.version,expected_job_version:recovery.job_version,
  expected_session_epoch:data.session_epoch,expected_governance_epoch:data.governance_epoch,
  acknowledge_boundary:recovery.ack_required===true,reason:reason.trim(),allow_publication:publish,confirmed:true});
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
