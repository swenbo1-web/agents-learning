"""Local detail and receipt readers that retain the originating list position."""

READER_CSS = """
html{scrollbar-gutter:stable;overflow-anchor:none}html.reading-open{overflow:hidden}
#detail-dialog,#receipt-dialog{padding:0;max-height:90vh;max-height:90dvh;overflow:auto;overscroll-behavior:contain;overflow-anchor:none}
#detail-dialog{width:min(1000px,calc(100% - 32px))}
#detail-dialog #detail{margin:0;border:0;border-radius:inherit;box-shadow:none;padding-top:0}
.reader-heading{position:sticky;top:0;z-index:2;background:#fff;padding:18px 0 12px;border-bottom:1px solid #e0e8e2}
.reader-heading .page-line{margin:0}.reader-heading h2{margin:0;overflow-wrap:anywhere;max-height:4.1em;overflow:auto}
#detail-title:focus{outline:none}
.reader-heading #close-detail{flex-shrink:0}.reader-heading #detail-review-actions{margin:12px 0 0}
#detail-receipt-host{max-height:210px;overflow:auto;overscroll-behavior:contain}
#detail-receipt-host .receipt{margin:12px 0 0;padding:12px;box-shadow:none}
#detail-receipt-host .receipt h2{font-size:16px}#detail-receipt-host .receipt .page-line{margin:0}
#receipt-dialog{width:min(780px,calc(100% - 32px))}
#receipt-dialog .receipt{border:0;margin:0;box-shadow:none}
#receipt-dialog .receipt-heading{position:sticky;top:-24px;z-index:1;background:#fff;padding:12px 0}
#detail-error,#receipt-error{background:#fff1eb;color:#8b3d29;border:1px solid #e0b5a7;padding:10px;border-radius:8px;overflow-wrap:anywhere}
@media(max-width:700px){#detail-dialog,#receipt-dialog{width:calc(100% - 16px);max-height:94vh;max-height:94dvh}.reader-heading{padding-top:12px}#detail-receipt-host{max-height:26vh}#receipt-dialog .receipt-heading{top:-17px}}
"""

READER_JAVASCRIPT = r'''
Object.assign(strings,{
viewReceipt:['View last decision result','查看上次审批结果'],
detailReading:['Read content and evidence here. Closing returns to the same list position.','在此查看正文和证据，关闭后返回原列表位置。']
});
const readingDialogIds=['detail-dialog','review-dialog','receipt-dialog'];
const handledReadingCloses={};
let readingAnchor=null;
function rememberReadingPosition(opener){
 if(readingAnchor)return;
 const target=opener||document.activeElement;
 readingAnchor={x:window.scrollX||0,y:window.scrollY||0,target,key:target?.dataset?.detailKey||null};
}
function syncReadingDialogs(){
 const opened=readingDialogIds.some(id=>$(id).open);
 document.documentElement.classList?.toggle('reading-open',opened);
 if(opened||!readingAnchor)return;
 const anchor=readingAnchor;readingAnchor=null;
 let target=anchor.target;
 if(!target?.isConnected||target.disabled)target=anchor.key?Array.from(document.querySelectorAll('[data-detail-key]')).find(e=>e.dataset.detailKey===anchor.key&&!e.disabled):null;
 if(!target?.isConnected||target.disabled)target=$('list');
 target?.focus?.({preventScroll:true});
 window.scrollTo({left:anchor.x,top:anchor.y,behavior:'instant'});
}
function openReadingDialog(id,opener){
 rememberReadingPosition(opener);
 const dialog=$(id);if(!dialog.open)dialog.showModal();
 syncReadingDialogs();
}
function closeReadingDialog(id,restore=true){
 const dialog=$(id);
 if(dialog.open){handledReadingCloses[id]=(handledReadingCloses[id]||0)+1;dialog.close();}
 if(restore)syncReadingDialogs();
}
function closeReceipt(){
 const top=$('detail-dialog').scrollTop;
 $('receipt').hidden=true;
 closeReadingDialog('receipt-dialog',false);
 if($('detail-dialog').open){$('detail-title').focus({preventScroll:true});$('detail-dialog').scrollTop=top;}
 syncReadingDialogs();
}
function closeDetail(){
 $('detail').hidden=true;state.detail=null;state.detailData=null;
 closeReadingDialog('detail-dialog',false);
 if($('receipt').parentElement===$('detail-receipt-host')){
  $('receipt').hidden=true;$('receipt-dialog').append($('receipt'));
 }
 syncReadingDialogs();
}
function showReceipt(){
 if(!state.receipt)return;
 renderReceipt();
 if($('detail-dialog').open){
  const top=$('detail-dialog').scrollTop;
  $('detail-receipt-host').append($('receipt'));$('receipt').hidden=false;$('detail-receipt-host').scrollTop=0;
  $('detail-dialog').scrollTop=top;
 }else{
  $('receipt-dialog').append($('receipt'));$('receipt').hidden=false;
  openReadingDialog('receipt-dialog');$('receipt-dialog').scrollTop=0;$('close-receipt').focus({preventScroll:true});
 }
}
function resetReadingDialogs(){
 readingAnchor=null;
 for(const id of readingDialogIds)closeReadingDialog(id,false);
 $('receipt-dialog').append($('receipt'));$('receipt').hidden=true;
 syncReadingDialogs();
}
async function showDetail(kind,id,revision,opener){
 closeReceipt();rememberReadingPosition(opener);
 state.detail={kind,id,revision};state.detailData=null;state.fieldOffset=0;state.section='content';
 state.evidenceOffset=0;state.evidenceNext=null;state.pages={};state.history={};
 try{
  await loadField();
  if(kind==='knowledge')for(const name of ['revisions','actions','events'])await loadHistory(name);
  if(!state.detail)return;
  openReadingDialog('detail-dialog',opener);$('detail-dialog').scrollTop=0;
  $('detail-title').focus({preventScroll:true});
 }catch(error){closeDetail();throw error;}
}
for(const id of readingDialogIds){
 $(id).addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
 $(id).addEventListener('close',()=>{
  // Our explicit close already applied its state changes. Its queued event must
  // not erase another item that is now loading in the same reader.
  if(handledReadingCloses[id]){handledReadingCloses[id]--;return;}
  // A close event can arrive after the dialog was reopened. Do not clear it.
  if($(id).open)return;
  if(id==='detail-dialog')closeDetail();
  if(id==='receipt-dialog')$('receipt').hidden=true;
  syncReadingDialogs();
 });
}
$('view-receipt').addEventListener('click',()=>run(showReceipt));
'''
