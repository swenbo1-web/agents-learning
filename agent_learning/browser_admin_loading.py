"""Independent settings and bounded detail reads; a slow panel cannot lock settings."""

LOADING_JAVASCRIPT = r'''
Object.assign(strings,{
 adminReadRetry:['Reload basic settings','重新读取基本设置'],
 adminReadPaused:['Automatic refresh paused after repeated read failures. Use “Reload basic settings” to retry.','连续读取失败，已暂停自动刷新。请点击“重新读取基本设置”重试。'],
 adminReadStale:['Showing the last successful settings read at {time}. {message}','当前保留 {time} 的上次成功状态。{message}'],
 adminSectionLoading:['This section is loading separately; basic settings remain available.','此区域单独加载，基本设置仍可操作。'],
 adminSectionFailed:['This section could not load and automatic retries are paused. {message}','此区域暂未读取成功，已暂停自动重试。{message}'],
 adminSectionStale:['Keeping this section’s previous data from {time}.','保留此区域 {time} 的上次成功数据。'],
 adminSectionRetry:['Retry this section','重试此区域'],
 adminSectionChanged:['Settings changed during this read. Reload basic settings.','读取期间设置已变化，请重新读取基本设置。'],
 queue_not_checked:['Detailed queue checks load separately','详细队列检查单独加载']
});
const ADMIN_SECTIONS={queue:'queue',activity:'review_activity',recovery:'recovery'};
let adminReadScope='',adminReadSerial=0,adminCoreSerial=0,adminCoreInFlight=null,adminSections={};
function adminScope(data){return JSON.stringify([data.project_id,data.host_scope,adminVersions(data)]);}
function resetAdminReads(){adminReadScope='';adminReadSerial++;adminCoreSerial++;adminCoreInFlight=null;adminSections={};state.adminPollFailures=0;state.adminReadAt=0;}
function adoptAdminState(data){
 const scope=adminScope(data),previous=state.adminData;
 if(scope!==adminReadScope){adminReadSerial++;adminReadScope=scope;adminSections={};}
 else if(data.details_separate&&previous){for(const key of Object.values(ADMIN_SECTIONS))if(previous[key])data[key]=previous[key];}
 state.adminData=data;
}
function renderAdminReadNotice(root,section){
 if(!state.adminData?.details_separate)return true;
 const slot=adminSections[section]||{},data=state.adminData[ADMIN_SECTIONS[section]];
 if(slot.error){root.append(node('p',t('adminSectionFailed',{message:errorText(slot.error)}),'note'));if(data)root.append(node('p',t('adminSectionStale',{time:stamp(slot.updated/1000)}),'muted'));}
 else if(!data)root.append(node('p',t('adminSectionLoading'),'muted'));
 if(slot.error||!data){const retry=node('button',t('adminSectionRetry'));retry.disabled=Boolean(slot.inFlight);retry.addEventListener('click',()=>run(()=>loadAdminSection(section,true)));root.append(retry);}
 return Boolean(data);
}
async function loadAdminSection(section,force=false){
 if(!canAdmin()||!state.adminData?.details_separate)return;
 const epoch=state.epoch,scope=adminReadScope,serial=adminReadSerial;
 let slot=adminSections[section];if(!slot)slot=adminSections[section]={updated:0,error:null,inFlight:null};
 if(slot.inFlight)return slot.inFlight;
 if(!force&&(slot.error||Date.now()-slot.updated<30000))return;
 const current=()=>canAdmin()&&epoch===state.epoch&&scope===adminReadScope&&serial===adminReadSerial&&adminSections[section]===slot;
 const request=(async()=>{
  try{
   const result=await api('admin-control/details',{section});if(!current())return;
   if(result.section!==section||JSON.stringify([result.project_id,result.host_scope,result.versions])!==scope)throw uiFailure('adminSectionChanged');
   state.adminData[ADMIN_SECTIONS[section]]=result.data;slot.updated=Date.now();slot.error=null;
  }catch(error){if(current())slot.error=error;}
  finally{if(current()){slot.inFlight=null;renderAdminState();}}
 })();
 slot.inFlight=request;return request;
}
function loadAdminDetails(force=false){return Promise.all(Object.keys(ADMIN_SECTIONS).map(section=>loadAdminSection(section,force)));}
function renderAdminReadFailure(){
 const retry=$('admin-read-retry');if(retry)retry.hidden=!browserErrors.poll;
 if(!browserErrors.poll)return;
 const message=errorText(browserErrors.poll),lines=[];
 if(state.adminReadAt)lines.push(t('adminReadStale',{time:stamp(state.adminReadAt/1000),message}));else lines.push(message);
 if((state.adminPollFailures||0)>=3)lines.push(t('adminReadPaused'));
 $('admin-poll-error').textContent=lines.join(' ');$('admin-poll-error').hidden=false;
}
async function loadAdminCore(force=false){
 if(!canAdmin())throw uiFailure('adminNeedCapability');
 if(adminCoreInFlight)return adminCoreInFlight;
 const epoch=state.epoch,serial=++adminCoreSerial;
 const current=()=>epoch===state.epoch&&serial===adminCoreSerial&&canAdmin();
 const request=(async()=>{
  try{
   const data=await api('admin-control',{view:'settings'});if(!current())return;
   adoptAdminState(data);state.adminReadAt=Date.now();state.adminPollFailures=0;
   if(!state.adminPending&&(force||!state.adminFormBase||!adminEditing()))hydrateAdminForm(data);
   else if(state.adminFormBase)state.adminStale=JSON.stringify(adminVersions(state.adminFormBase))!==JSON.stringify(adminVersions(data));
   browserErrors.poll=null;$('admin-poll-error').hidden=true;$('admin-read-retry').hidden=true;
   renderAdminState();void loadAdminDetails(force);void loadAdminHistory(force);
  }catch(error){if(current()){state.adminPollFailures=(state.adminPollFailures||0)+1;browserErrors.poll=error;renderBrowserErrors();renderAdminReadFailure();}}
  finally{if(current()){adminCoreInFlight=null;scheduleAdminPoll();}}
 })();adminCoreInFlight=request;return request;
}
'''
