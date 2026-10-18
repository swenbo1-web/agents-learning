"""Offline error presentation; keep server diagnostics and write-receipt contracts intact."""

ERROR_JAVASCRIPT = r'''
Object.assign(strings,{
query_budget_exceeded:['Local query budget exceeded. Narrow this view or retry; model-call allowances are separate.','当前视图查询超限，请缩小筛选范围或稍后重试。这与模型调用额度无关。'],
database_read_unavailable:['The database is temporarily unavailable for reading. Retry this view later.','数据库暂时无法读取，请稍后刷新当前视图。'],
browser_read_unavailable:['This view could not be read or reached its local query limit. Narrow the view or retry.','当前视图暂时无法读取或查询超限，请缩小范围或稍后重试。'],
browser_access_expired:['This page connection expired. Reopen it from the current project panel.','此页面连接已失效，请从当前项目面板重新打开。'],
access_key_required:['A valid access code is required. Reopen the current project panel.','需要有效访问码，请从当前项目面板重新打开。'],
response_budget_exceeded:['This response is too large. Reduce the page size or narrow the view.','返回内容过多，请减少每页条数或缩小筛选范围。'],
stored_json_invalid:['Stored data could not be decoded. Keep the records and inspect the source before retrying.','存储资料暂时无法解析，请保留记录并检查来源后重试。'],
stored_contract_mismatch:['Stored data does not match this page version. Check the installed runtime version.','存储资料与当前页面版本不匹配，请核对已安装的运行版本。'],
view_item_not_found:['This item is unavailable in the current project. Refresh the list.','当前项目中没有可读取的此项内容，请刷新列表。'],
view_read_only:['This page is read-only. Open the review or administrator entry for changes.','此页面为只读，请从审批或管理员入口进行修改。'],
api_failure_detail:['Request failed: {message}','请求未完成：{message}']
});
const browserErrorCodes=new Set(['query_budget_exceeded','database_read_unavailable','browser_read_unavailable','browser_access_expired','access_key_required','response_budget_exceeded','stored_json_invalid','stored_contract_mismatch','view_item_not_found','view_read_only']);
const legacyBrowserErrors=new Map([
 ['query budget exceeded; narrow this view or retry','query_budget_exceeded'],['query budget exceeded','query_budget_exceeded'],
 ['database read unavailable','database_read_unavailable'],['read unavailable or query budget exceeded','browser_read_unavailable'],
 ['browser access expired; reopen the current project panel','browser_access_expired'],['access key required','access_key_required'],
 ['response exceeds browser budget; reduce page size','response_budget_exceeded'],['stored data is not valid JSON','stored_json_invalid'],
 ['stored data does not match the browser contract','stored_contract_mismatch'],['item not found in this project','view_item_not_found'],['browser is read-only','view_read_only']
]);
const browserErrors={page:null,plan:null,poll:null};
function errorText(error){
 if(!error)return '';
 if(error.uiCause)return errorText(error.uiCause)+' '+t(error.uiNotice);
 if(error.uiKey)return t(error.uiKey,error.uiArgs||{});
 const message=typeof error==='string'?error:String(error.message||error);
 const key=legacyBrowserErrors.get(message);return key?t(key):message;
}
function uiFailure(key,args={}){const error=new Error(t(key,args));error.uiKey=key;error.uiArgs=args;return error;}
function apiFailure(data,status){
 data=data&&typeof data==='object'?data:{};
 const raw=typeof data.error==='string'?data.error:'';
 const key=browserErrorCodes.has(data.reason)?data.reason:legacyBrowserErrors.get(raw);
 const error=key?uiFailure(key):raw?uiFailure('api_failure_detail',{message:raw}):uiFailure('requestFailed');
 error.status=status;error.diagnostic=raw;return error;
}
function withErrorNotice(error,key){const result=new Error(errorText(error)+' '+t(key));result.uiCause=error;result.uiNotice=key;result.status=error.status;return result;}
function renderBrowserErrors(){
 setError(browserErrors.page);planError(browserErrors.plan);
 $('admin-poll-error').textContent=browserErrors.poll?t('adminPollError',{message:errorText(browserErrors.poll)}):'';
 $('admin-poll-error').hidden=!browserErrors.poll;
}
'''
