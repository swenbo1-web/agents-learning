"""Responsive offline presentation for the standalone knowledge browser.

The DSH composer uses its host tokens. This separate local document follows the
browser color scheme and keeps its own finite palette; no remote assets load.
"""

BROWSER_THEME_CSS = """
:root{
 --al-bg:#f5f7fa;--al-surface:#fff;--al-soft:#f1f4f8;--al-hover:#e9eef5;
 --al-text:#1c283b;--al-muted:#52627a;--al-border:#dbe2eb;--al-strong-border:#b5c3d5;
 --al-brand:#235bc5;--al-brand-hover:#1c49a1;--al-on-brand:#fff;--al-brand-soft:#edf3ff;
 --al-success:#246747;--al-success-bg:#edf7f1;--al-warn:#855500;--al-warn-bg:#fff7e7;
 --al-error:#a33435;--al-error-bg:#fff0ef;--al-backdrop:#14233980;
 color-scheme:light;
}
@media(prefers-color-scheme:dark){:root{
 --al-bg:#12171f;--al-surface:#1a212c;--al-soft:#222c3a;--al-hover:#2a3749;
 --al-text:#e4eaf3;--al-muted:#a6b5ca;--al-border:#334155;--al-strong-border:#566c87;
 --al-brand:#9cbdff;--al-brand-hover:#bed3ff;--al-on-brand:#11234a;--al-brand-soft:#233657;
 --al-success:#95d6ad;--al-success-bg:#20382d;--al-warn:#f1cc83;--al-warn-bg:#3b3020;
 --al-error:#ffacaa;--al-error-bg:#40282d;--al-backdrop:#070b12b3;
 color-scheme:dark;
}}
*{box-sizing:border-box}[hidden]{display:none!important}
html{scrollbar-gutter:stable;scroll-padding-top:24px}
body{margin:0;background:var(--al-bg);color:var(--al-text);font:14px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header,main,footer{width:100%;max-width:1280px;margin:0 auto;padding:20px 32px}
header{display:flex;align-items:center;justify-content:space-between;gap:28px;padding-top:30px;padding-bottom:16px}
header>div:first-child{min-width:0}h1{font-size:28px;line-height:1.25;letter-spacing:-.65px;font-weight:700;margin:7px 0}
h2{font-size:20px;line-height:1.4;margin:0 0 8px;font-weight:650}h3{font-size:16px;margin:24px 0 12px}h4{margin:20px 0 8px}p{margin:8px 0}
header p{color:var(--al-muted);max-width:660px;margin-bottom:0;font-size:13px}
.eyebrow{display:block;font-size:10px;font-weight:700;letter-spacing:1.6px;color:var(--al-muted);overflow-wrap:anywhere}
.header-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;flex-shrink:0}
.header-controls label{font-size:11px;color:var(--al-muted)}.header-controls select{font-size:12px;min-height:34px;padding:5px 9px}
main{padding-top:4px}.panel{background:var(--al-surface);border:1px solid var(--al-border);border-radius:12px;padding:24px;margin:16px 0;box-shadow:none;min-width:0}
.project-line{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:18px 20px;border:1px solid var(--al-border);border-left:3px solid var(--al-brand);border-radius:10px;background:var(--al-surface)}
.project-line>div:first-child{min-width:0}.project-line strong{display:block;font-size:18px;margin-top:2px;overflow-wrap:anywhere}
#project-context{font-size:11px;color:var(--al-muted);overflow-wrap:anywhere;margin:3px 0 0;max-width:780px}
.button-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.button-row button{white-space:normal}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:14px 0 10px}
.stat{background:var(--al-surface);border:1px solid var(--al-border);border-radius:10px;padding:15px 18px;min-width:0;box-shadow:none}
.stat span{display:block;color:var(--al-muted);font-size:12px}.stat strong{display:block;font-size:27px;line-height:1.35;font-weight:650;letter-spacing:-.6px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.note,.muted{color:var(--al-muted);font-size:12px;overflow-wrap:anywhere}.note{line-height:1.75}#mode-note{margin:8px 2px 16px}
.badge,.tag{display:inline-flex;align-items:center;gap:4px;background:var(--al-soft);color:var(--al-muted);border:1px solid var(--al-border);border-radius:6px;padding:2px 8px;font-size:11px;line-height:1.6;white-space:normal;overflow-wrap:anywhere}
.badge.review{background:var(--al-brand-soft);border-color:var(--al-border);color:var(--al-brand)}.badge[data-tone=warning]{background:var(--al-warn-bg);color:var(--al-warn)}
label{color:var(--al-muted)}input[type=checkbox]{accent-color:var(--al-brand)}#query{min-width:0}
button,input,select,textarea{font:inherit;color:var(--al-text);border:1px solid var(--al-strong-border);border-radius:7px;padding:8px 11px;background:var(--al-surface);max-width:100%;min-width:0}
input,select,textarea{font-size:13px;accent-color:var(--al-brand)}button{cursor:pointer;font-size:12px;font-weight:550;line-height:1.5;transition:background .12s,border-color .12s}
button:hover{background:var(--al-hover);border-color:var(--al-strong-border)}button:disabled{opacity:.5;cursor:not-allowed}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--al-brand);outline-offset:3px}
button.primary{background:var(--al-brand);border-color:var(--al-brand);color:var(--al-on-brand)}button.primary:hover{background:var(--al-brand-hover);border-color:var(--al-brand-hover)}
button.quiet{background:transparent;border-color:transparent;color:var(--al-muted)}button.quiet:hover{background:var(--al-soft);border-color:var(--al-border)}button.danger-quiet{color:var(--al-error);background:var(--al-surface)}
nav{display:flex;gap:4px;overflow-x:auto;flex-wrap:nowrap;padding:5px;border:1px solid var(--al-border);border-radius:9px;background:var(--al-soft);margin:18px 0 14px;scrollbar-width:thin}
nav button{background:transparent;border:1px solid transparent;border-radius:6px;color:var(--al-muted);white-space:nowrap;padding:8px 13px;flex-shrink:0}
nav button.selected{background:var(--al-surface);border-color:var(--al-border);color:var(--al-brand);box-shadow:none;font-weight:650}
.view-intro{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}.view-intro h2{font-size:18px;margin:0 0 3px}.view-intro p{margin:0;max-width:750px}
.filters{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;padding:14px;background:var(--al-soft);border:1px solid var(--al-border);border-radius:9px;margin-bottom:18px}
.filter-field{display:flex;flex-direction:column;gap:5px;flex:0 1 155px;min-width:130px}.filter-field label{font-size:11px;font-weight:550;color:var(--al-muted)}.filter-field input,.filter-field select{width:100%;margin:0;min-height:38px}.filter-field.search-field{flex:1 1 290px;min-width:180px}
.filters>button{min-height:38px}.page-line{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:16px 0 12px}.page-line .button-row{flex-shrink:0}#page-label{font-variant-numeric:tabular-nums}
#list{display:flex;flex-direction:column;gap:12px}.card{padding:18px;margin:0;border:1px solid var(--al-border);border-radius:9px;background:var(--al-surface);box-shadow:none;min-width:0;transition:border-color .12s}
.card:hover{border-color:var(--al-strong-border)}.card-body{min-width:0;flex:1}.card-title{display:block;appearance:none;background:none;border:0;color:var(--al-brand);padding:0;text-align:left;font-size:15px;line-height:1.6;font-weight:650;overflow-wrap:anywhere;max-width:100%}.card-title:hover{color:var(--al-brand);background:none;text-decoration:underline;border:0}
.tags{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin:8px 0;color:var(--al-muted);font-size:11px}.card-preview{margin:10px 0;white-space:pre-wrap;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;color:var(--al-text);font-size:13px}
.card-footer{font-size:10.5px;color:var(--al-muted);overflow-wrap:anywhere;margin-top:10px;font-variant-numeric:tabular-nums}.candidate-card{display:flex;align-items:flex-start;gap:12px}.candidate-card>input[type=checkbox]{flex-shrink:0;margin-top:7px;width:16px;height:16px;padding:0}
.empty-state{padding:44px 20px;text-align:center;border:1px dashed var(--al-strong-border);border-radius:9px;background:var(--al-soft)}.empty-state strong{font-size:15px}.empty-state p{max-width:520px;margin:8px auto 0}
#knowledge-health{padding:13px 18px;margin-top:12px;margin-bottom:12px}#knowledge-health>summary{font-size:12px;font-weight:600;cursor:pointer;line-height:1.8}#health-state{margin-left:12px;font-size:10.5px;font-weight:450;vertical-align:middle}#health-result .card{margin:12px 0}.health-stale{background:var(--al-warn-bg);border-left:3px solid var(--al-warn);border-radius:6px;padding:10px 12px;color:var(--al-warn)}
.checks{padding:12px;border:1px solid var(--al-border);border-radius:8px;background:var(--al-soft);margin:12px 0;color:var(--al-text);overflow-wrap:anywhere}.checks.ready{background:var(--al-success-bg);border-color:var(--al-border);color:var(--al-success)}.checks.blocked{background:var(--al-warn-bg);border-color:var(--al-border);color:var(--al-warn)}.warning{color:var(--al-warn)}
.card:first-child{border-top:1px solid var(--al-border)}.checks .warning{color:var(--al-warn)}
.selection-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px;background:var(--al-soft);border-color:var(--al-border)}.check-label{display:flex;align-items:center;gap:8px}.check-label input{margin:0;width:16px;height:16px}.review-actions{margin:12px 0}#review-reason{width:100%}
.login{max-width:600px;margin:26px auto}.login label{display:block;margin-top:16px;font-size:12px}.login input{width:100%;display:block;margin:6px 0 14px}.login .primary{min-width:110px}
#loading{position:sticky;top:8px;z-index:6;width:fit-content;padding:8px 14px;margin:8px auto;background:var(--al-brand-soft);color:var(--al-brand);border:1px solid var(--al-border);border-radius:8px;font-size:12px}
#error,#plan-error,#detail-error,#receipt-error{background:var(--al-error-bg);color:var(--al-error);border:1px solid var(--al-border);padding:12px;border-radius:8px;overflow-wrap:anywhere;margin:10px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--al-soft);color:var(--al-text);border:1px solid var(--al-border);border-radius:8px;padding:16px;font:12px/1.85 ui-monospace,SFMono-Regular,Consolas,monospace;max-width:100%;overflow:auto}
#detail-text{font-size:14px;line-height:1.85;padding:20px;tab-size:4}.history-item,.evidence-item{background:var(--al-soft);border:1px solid var(--al-border);border-radius:8px;padding:12px 14px;margin:10px 0;overflow-wrap:anywhere}.history-item button{max-width:100%;text-align:left}
details>summary{cursor:pointer;color:var(--al-muted)}.section-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0 10px}.section-tabs button.selected{background:var(--al-brand-soft);border-color:var(--al-brand);color:var(--al-brand)}
dialog{background:var(--al-surface);color:var(--al-text);border:1px solid var(--al-strong-border);border-radius:14px;padding:24px;width:min(820px,calc(100% - 32px));max-height:90vh;overflow:auto;box-shadow:none}dialog::backdrop{background:var(--al-backdrop)}
.dialog-heading,.dialog-footer{display:flex;align-items:center;justify-content:space-between;gap:16px}.dialog-footer{justify-content:flex-end;padding-top:18px;border-top:1px solid var(--al-border);margin-top:18px;background:var(--al-surface);bottom:-24px}
#detail-dialog,#receipt-dialog{background:var(--al-surface);color:var(--al-text)}.reader-heading,#receipt-dialog .receipt-heading{background:var(--al-surface);border-color:var(--al-border)}.reader-heading{padding-bottom:14px}#detail-dialog #detail{background:var(--al-surface)}
.receipt{border-color:var(--al-strong-border)}.admin-status-line{border-color:var(--al-border)}
.admin-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:32px;margin:22px 0;border-color:var(--al-border)}.admin-form-grid>div{min-width:0}.admin-form-grid label,.admin-limit-grid label{display:block;font-size:12px;margin:12px 0 5px}.admin-form-grid input:not([type=checkbox]),.admin-form-grid select{display:block;width:100%}
.admin-limit-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.admin-limit-grid input{width:100%}.admin-details{border-top:1px solid var(--al-border);padding-top:16px;margin:18px 0}.admin-details>summary{font-size:13px;font-weight:600}.admin-command-row{padding:14px 0;border-top:1px solid var(--al-border)}.admin-summary-switch{align-items:flex-start!important}.admin-summary-switch input[type=checkbox]{margin-top:3px!important;flex-shrink:0}.budget-scope{padding:10px;background:var(--al-warn-bg);color:var(--al-warn);border-color:var(--al-border);border-radius:7px;font-size:12px}
.admin-stats{margin:16px 0}.admin-stats .stat{background:var(--al-soft);padding:12px 14px}.admin-stats .stat strong{font-size:23px}.toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:12px 0}
footer{padding-top:8px;padding-bottom:24px;color:var(--al-muted);font-size:11px;border-top:1px solid var(--al-border);margin-top:16px}
@media(max-width:900px){header,main,footer{padding-left:22px;padding-right:22px}.header-controls{max-width:200px}.admin-form-grid{gap:22px}.stat{padding:12px 14px}}
@media(max-width:650px){header,main,footer{padding-left:14px;padding-right:14px}header{align-items:flex-start;flex-direction:column;gap:14px;padding-top:22px}h1{font-size:25px}.header-controls{max-width:none;justify-content:flex-start}main{padding-top:0}.panel{padding:16px}.project-line{padding:14px;align-items:flex-start;flex-direction:column;gap:10px}.project-line .button-row{align-self:flex-end}.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.stat strong{font-size:24px}nav{border-radius:8px}nav button{padding:7px 10px}.filters{padding:11px;gap:8px}.filter-field{flex:1 1 120px;min-width:0}.filter-field.search-field{flex-basis:100%;min-width:0}.filters>button{flex:1 1 100px}.card{padding:14px}.admin-form-grid{grid-template-columns:1fr;gap:8px}.admin-limit-grid{gap:10px}.page-line{align-items:flex-start;flex-wrap:wrap}.view-intro{margin-bottom:14px}.dialog-heading{align-items:flex-start}dialog{padding:16px}#detail-text{padding:14px;font-size:13px}#health-state{margin-left:7px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;scroll-behavior:auto!important}}
"""
