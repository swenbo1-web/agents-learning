"""Read-only, offline file-capture and effective review-mode explanations."""

DIAGNOSTIC_JAVASCRIPT = r'''
Object.assign(strings,{
reviewModeCurrent:['Current review mode: {mode}. Setting: {setting}.','当前实际审核方式：{mode}。设置来源：{setting}。'],
reviewModeExplicit:['explicitly saved for this project and host','此项目、此宿主已显式保存'],
reviewModeDefault:['compatibility default; no explicit stage policy saved','兼容默认值，尚未显式保存阶段策略'],
reviewModeDraft:['The form selection is an unsaved choice. It takes effect only after you save and confirm the change.','表单中的方式是待保存选择；保存并确认变更后才会生效。'],
reviewModeAdmission:['New-review admission: {enabled} · budget gate: {budget} · executor: {executor} · running: {running}.','新审核准入：{enabled} · 预算检查：{budget} · 执行器：{executor} · 正在运行：{running}。'],
reviewModeEnabled:['enabled','已开启'],reviewModePaused:['paused','已暂停'],
reviewModeAvailable:['available','可用'],reviewModeBlocked:['waiting or blocked','等待或受限'],
reviewModeRunning:['yes','是'],reviewModeStopped:['no','否'],
reviewModeNote:['Enabled admission does not mean a review is running. Queue and evidence checks load separately; viewing this page changes no policy.','准入开启不代表正在审核；队列和证据另行检查，查看页面不会改变策略。'],
fileRootsHeading:['Linked engineering directories (read only)','已关联工程目录（只读）'],
fileRootsEmpty:['No additional engineering directory is explicitly linked.','尚未显式关联其他工程目录。'],
fileRootsCount:['Explicitly linked directories: {n}.','显式关联目录：{n} 个。'],
fileRootsUnknown:['Directory-link status is unavailable from this service.','当前服务未提供目录关联状态。'],
fileRootsWorkspaceUnknown:['This project page has no verified checkout context; active file roots are not inferred from registered project paths.','当前项目页面没有已核实的 checkout 上下文；不会从登记的项目路径推定实际文件根。'],
fileRootsReload:['A saved directory choice requires a host reload before capture uses it.','已保存的目录选择需要重载宿主后才能用于采集。'],
fileRootsReloadUnknown:['Whether a host reload is needed is unknown.','是否需要重载宿主尚未知。'],
fileRootsGuide:['To link an engineering directory, explicitly choose it through the local CLI, confirm the configuration, then reload the host.','需要关联工程目录时，请在本地 CLI 中明确选择目录并确认配置，然后重载宿主。'],
fileRootsBoundary:['Directory links only allow file-history association; they do not merge projects or knowledge stores, grant execution permission, or scan files.','目录关联仅用于文件历史关系；不会合并项目或知识库、授予执行权限或扫描文件。'],
fileCaptureHeading:['Recent file-association diagnostics (read only)','近期文件关系诊断（只读）'],
fileCaptureCounts:['Sampled events {events} · parsed targets {targets} · passed filtering {accepted} · filtered {filtered}.','抽样事件 {events} · 解析目标 {targets} · 通过过滤 {accepted} · 已过滤 {filtered}。'],
fileCaptureUnknown:['File-association diagnostics are unavailable from this service. Missing observations remain unknown.','当前服务未提供文件关系诊断；缺少的观察保持未知。'],
fileCaptureLimited:['Only a bounded recent sample is shown; these counts are not a full capture audit.','这里只展示有界近期样本，计数不代表完整采集审计。'],
fileCaptureNote:['Passing filtering does not confirm that a file link was saved. Filtered targets were excluded. This view does not expose tool text or prove task success or user acceptance.','通过过滤不表示文件关系已成功保存；已过滤目标被排除。此处不展示工具原文，也不证明任务成功或用户验收。'],
fileCaptureReason:['{reason}: {n}','{reason}：{n}'],
fileCapture_outside_workspace:['Outside the allowed directories','超出允许目录'],
fileCapture_unsupported_arguments:['Unsupported argument structure','参数结构不支持'],
fileCapture_no_structured_target:['No supported structured target','没有受支持的结构化目标'],
fileCapture_sensitive_target:['Sensitive target excluded','敏感目标已排除'],
fileCapture_invalid_target:['Invalid target','目标无效'],
fileCapture_symlink_target:['Linked target excluded','链接目标已排除'],
fileCapture_target_limit:['Target limit reached','达到目标数量上限'],
fileCapture_duplicate_target:['Duplicate target','重复目标'],
fileCapture_unsupported_tool:['Unsupported tool','工具不支持']
});
function renderEffectiveReviewMode(data){
 const root=$('phase-current-policy');if(!root)return;root.replaceChildren();
 const cfg=data?.phase_review?.policy,mode={phase:'phaseModeStage',legacy:'phaseModeLegacy'}[cfg?.mode];
 const setting=cfg?.configured===true?'reviewModeExplicit':cfg?.configured===false?'reviewModeDefault':'unknown';
 root.append(node('p',t('reviewModeCurrent',{mode:t(mode||'unknown'),setting:t(setting)})));
 const flag=(value,yes,no)=>t(value===true?yes:value===false?no:'unknown');
 const budget=data?.admission?.admission_status;
 root.append(node('p',t('reviewModeAdmission',{
  enabled:flag(data?.enabled,'reviewModeEnabled','reviewModePaused'),
  budget:t(budget==='available'?'reviewModeAvailable':['budget_exhausted','token_budget_exhausted','concurrency_limited'].includes(budget)?'reviewModeBlocked':'unknown'),
  executor:flag(data?.routing?.ready,'reviewModeAvailable','reviewModeBlocked'),
  running:flag(data?.runner?.running,'reviewModeRunning','reviewModeStopped')})));
 if(mode&&$('phase-mode').value!==cfg.mode)root.append(node('p',t('reviewModeDraft'),'note'));
 root.append(node('p',t('reviewModeNote'),'muted'));
}
function renderLinkedFileRoots(root,status){
 const section=node('details');section.append(node('summary',t('fileRootsHeading')));
 if(!status||!Array.isArray(status.linked_workspaces))section.append(node('p',t('fileRootsUnknown'),'muted'));
 else{
  const paths=status.linked_workspaces.filter(value=>typeof value==='string');
  section.append(node('p',t(paths.length?'fileRootsCount':'fileRootsEmpty',{n:paths.length})));
  for(const path of paths)section.append(node('p',path,'muted'));
  if(status.workspace_verified!==true)section.append(node('p',t('fileRootsWorkspaceUnknown'),'note'));
  if(status.reload_required===true)section.append(node('p',t('fileRootsReload'),'note'));
  else if(status.reload_required!==false)section.append(node('p',t('fileRootsReloadUnknown'),'muted'));
 }
 section.append(node('p',t('fileRootsGuide'),'muted'),node('p',t('fileRootsBoundary'),'note'));root.append(section);
}
function renderFileCaptureDiagnostics(root,value){
 const section=node('details');section.append(node('summary',t('fileCaptureHeading')));
 const count=value=>Number.isSafeInteger(value)&&value>=0?value:t('unknown');
 if(value?.available!==true)section.append(node('p',t('fileCaptureUnknown'),'muted'));
 else{
  section.append(node('p',t('fileCaptureCounts',{events:count(value.events),targets:count(value.target_count),accepted:count(value.accepted_count),filtered:count(value.filtered_count)})));
  // Only controlled reason names and integer counts may become visible text.
  for(const reason of ['outside_workspace','unsupported_arguments','no_structured_target','sensitive_target','invalid_target','symlink_target','target_limit','duplicate_target','unsupported_tool']){
   const n=value.reasons?.[reason];if(Number.isSafeInteger(n)&&n>0)section.append(node('p',t('fileCaptureReason',{reason:t('fileCapture_'+reason),n}),'muted'));
  }
 }
 section.append(node('p',t('fileCaptureLimited'),'muted'),node('p',t('fileCaptureNote'),'note'));root.append(section);
}
const baseDiagnosticQueue=renderWorkloadQueue;
renderWorkloadQueue=function(data){baseDiagnosticQueue(data);if(data.queue)renderLinkedFileRoots($('admin-queue-details'),data.queue.workspace_links);};
const baseDiagnosticRecovery=renderAdminRecovery;
renderAdminRecovery=function(data){baseDiagnosticRecovery(data);if(data.recovery)renderFileCaptureDiagnostics($('admin-recovery'),data.recovery.file_capture_diagnostics);};
function lockUnsupportedPhaseFields(){if(!(state.adminFormBase??state.adminData)?.phase_review?.policy)for(const id of PHASE_FIELDS)$(id).disabled=true;}
const baseDiagnosticState=renderAdminState;
renderAdminState=function(){baseDiagnosticState();if(state.adminData)renderEffectiveReviewMode(state.adminData);lockUnsupportedPhaseFields();};
const baseDiagnosticControls=controls;
controls=function(){baseDiagnosticControls();lockUnsupportedPhaseFields();};
'''
