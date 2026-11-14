// Browser half of the agent-learning DSH plugin:
// - Composer toolbar button (`conversation.input.left`) shown ONLY for the bound/installed project
// - Compact overview, opt-in recommendations and collapsed advanced controls
// - Plugins page configuration card (`plugins.bundle.config` / `settings.plugin.item`)
window.__ModuleLoader__.load({
  id: "agent-learning",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    var PACKAGE_NAME = "agent-learning";
    var ROUTE = "/agent-learning/panel-v2";

    var TEXT = {
      zh: {
        button: "知识库",
        title: "Agent Learning 项目记忆与知识库",
        subtitle: "查看项目知识、工作历史、候选审批与管理员设置",
        knowledge: "正式知识",
        workMemory: "工作观察",
        pendingCases: "待审核案例",
        admin: "自动管理员",
        adminOn: "已开启",
        adminOff: "已关闭",
        attempts: "项目今日审核尝试", completed: "有效审核结论", failedAttempts: "失败尝试", remaining: "项目剩余额度", globalCalls: "所有项目今日合计尝试", globalRemaining: "全局保护剩余", projectUnavailable: "项目统计暂不可用，请更新运行端", scopeNote: "项目额度独立、同项目各宿主共用；另受全局保护限制", reset: "按 UTC 日统计；本地重置", unknown: "未知",
        freqTitle: "管理员调用频率方案（一键切换）",
        freqOff: "关闭",
        freqMinimal: "按需 (5/天)",
        freqLight: "省流 (15/天)",
        freqBalanced: "均衡 (100/天)",
        freqActive: "积极 (300/天)",
        freqSaved: "已应用管理员频率方案：",
        openBrowser: "打开可视化知识库（只读）",
        openReview: "打开知识审批与高级细致参数设置",
        runDebug: "发送 $learning-debug 调试检查",
        archiveHistory: "归档项目历史（不调用模型）",
        stopBrowser: "停止后台知识库服务",
        refresh: "刷新",
        loading: "正在读取状态…",
        projectRequired: "请从已安装项目的对话打开知识库",
        opening: "正在处理…",
        openedBrowser: "已在浏览器打开只读可视化知识库",
        openedReview: "已在浏览器打开知识审批与细致参数设置",
        stoppedBrowser: "已停止后台知识库服务",
        browserNotRunning: "当前项目没有正在运行的知识库服务",
        browserStopUnconfirmed: "停止结果待核对，请刷新状态",
        archiving: "正在扫描并归档项目历史…",
        archivedPrefix: "仅归档完成，尚未整理或发布；案例数 ",
        reusedHistory: "已复用历史批次；请打开审批查看进度并继续主审。未调用模型。",
        failed: "操作失败",
        confirmAdmin: "开启管理员会使用平台模型额度。首次开启默认分级发布：项目事实保留来源且未验证，争议转人工；已有发布设置保留。是否应用此频率？",
        unresolved: "上次设置结果待核对；再次点击可用相同请求重试。",
      },
      en: {
        button: "Memory",
        title: "Agent Learning Project Memory",
        subtitle: "Inspect knowledge, work history, candidate review and administrator settings",
        knowledge: "Published Knowledge",
        workMemory: "Work Observations",
        pendingCases: "Awaiting Review",
        admin: "Administrator",
        adminOn: "Enabled",
        adminOff: "Disabled",
        attempts: "Project attempts today", completed: "Valid review results", failedAttempts: "Failed attempts", remaining: "Project allowance left", globalCalls: "All-project attempts today", globalRemaining: "Global protection left", projectUnavailable: "Project usage unavailable; update the runtime", scopeNote: "Independent project allowance across hosts; global protection also applies", reset: "UTC day; local reset", unknown: "Unknown",
        freqTitle: "Administrator Frequency Preset",
        freqOff: "Off",
        freqMinimal: "Minimal (5/d)",
        freqLight: "Light (15/d)",
        freqBalanced: "Balanced (100/d)",
        freqActive: "Active (300/d)",
        freqSaved: "Applied frequency preset: ",
        openBrowser: "Open Visual Knowledge Browser (Read-Only)",
        openReview: "Open Review & Advanced Fine-Tuning",
        runDebug: "Send $learning-debug Check",
        archiveHistory: "Archive project history (no model call)",
        stopBrowser: "Stop Background Browser Server",
        refresh: "Refresh",
        loading: "Loading status...",
        projectRequired: "Open the knowledge panel from a conversation in an installed project",
        opening: "Working...",
        openedBrowser: "Opened read-only knowledge browser",
        openedReview: "Opened review & advanced fine-tuning page",
        stoppedBrowser: "Stopped background browser server",
        browserNotRunning: "No knowledge browser server is running for this project",
        browserStopUnconfirmed: "Stop result is unconfirmed; refresh the status",
        archiving: "Scanning and archiving project history...",
        archivedPrefix: "Archived only, not reviewed or published; cases ",
        reusedHistory: "Existing history batch reused. Open review for progress; no model call.",
        failed: "Operation failed",
        confirmAdmin: "Enabling the administrator uses platform model quota. On first enable, tiered publication records sourced project facts as unverified references; disputes need review. Existing publication choices are preserved. Apply this preset?",
        unresolved: "Previous settings result is unconfirmed. Click again to retry the same request.",
      },
    };

    Object.assign(TEXT.zh, {stageFrequency:"每 {n} 个交付阶段集中审核", stageQueue:"阶段：等待收尾 / 待审 / 处理中 / 部分 / 完成", stageCoverage:"最近一批覆盖", coverageFull:"完整", coveragePartial:"部分，保留后续处理", reportedTokens:"今日已报告 token", reservedTokens:"待确认用量预留", remainingTokens:"估算可用 / 每日预算", tokenEstimate:"用量未完整报告时保留估算；预留不等于实际消耗。阶段频率与 token 预算请在审批与设置中调整。"});
    Object.assign(TEXT.en, {stageFrequency:"Review every {n} delivered stages together", stageQueue:"Stages: awaiting close / review / queued / partial / completed", stageCoverage:"Latest batch coverage", coverageFull:"Complete", coveragePartial:"Partial; further work is retained", reportedTokens:"Reported tokens today", reservedTokens:"Usage awaiting confirmation: reserved", remainingTokens:"Estimated available / daily budget", tokenEstimate:"Incomplete usage retains an estimate; reservations are not actual consumption. Change stage frequency and token budgets in Review & settings."});

    Object.assign(TEXT.zh, {stageLocallyDeferred:"纯状态检查本地暂存：{n} 个阶段，仍有未审核资料。仅余下资料尚未送审，不算审核完成。后续正常阶段可按剩余预算合批；在管理员页点击“检查并继续整理”可主动补审。"});
    Object.assign(TEXT.en, {stageLocallyDeferred:"Pure status checks retained locally: {n} stages; some material remains unreviewed. Only the remaining material has not been submitted; these stages are not complete. A later normal stage may include it within its remaining budget. Use “Check and continue” in administrator settings to request review."});

    Object.assign(TEXT.zh,{"stageProtectedBacklog":"{batches} 个受保护失败批次关联 {phases} 个阶段待处理；新工作可继续，旧调用不会自动重试。","stageRegistration_task_missing":"最近完整公开回复未登记阶段：当时没有任务；若为阶段交付，请先登记任务。","stageRegistration_task_not_delivered":"最近完整公开回复未登记为交付阶段：任务当时尚未交付。","stageRegistration_delivery_already_registered":"最近完整公开回复未新增阶段：对应任务交付已经登记。","stageRegistration_delivery_not_registered":"最近完整公开回复已有任务交付，但未找到对应阶段；请在审批与设置核对记录。"});
    Object.assign(TEXT.en,{"stageProtectedBacklog":"{batches} protected failed batches cover {phases} stages awaiting action. New work can proceed; old calls will not retry automatically.","stageRegistration_task_missing":"The latest complete public reply did not register a stage: it had no task. Register a task first when delivering a stage.","stageRegistration_task_not_delivered":"The latest complete public reply did not register a delivery stage: the task was not delivered at that point.","stageRegistration_delivery_already_registered":"The latest complete public reply added no stage: that task delivery was already registered.","stageRegistration_delivery_not_registered":"The latest complete public reply has a delivered task but no matching stage. Check the record in Review & settings."});
    Object.assign(TEXT.zh,{"stageRegistration_awaiting_capture_seal": "最近完整公开回复仍待封存或采集恢复，尚不能确认阶段已登记完成。"});
    Object.assign(TEXT.en,{"stageRegistration_awaiting_capture_seal": "The latest complete public reply still awaits sealing or capture recovery; completed stage registration is not confirmed."});
    Object.assign(TEXT.zh,{"captureTransportPending": "采集未确认：{pending} 个待处理事件，其中 {retryable} 个可补传；{unresolved} 个仍未解决。仅在桥内存保留，宿主重启不能恢复。"});
    Object.assign(TEXT.en,{"captureTransportPending": "Capture is unconfirmed: {pending} pending events, {retryable} retryable; {unresolved} remain unresolved. Retained in bridge memory only; host restart cannot recover them."});
    function captureTransportLine(value,words){if(!value||value.persistence!=='bridge_memory_only'||!['pending_events','retryable_events','unresolved_events'].every(key=>Number.isSafeInteger(value[key])&&value[key]>=0)||value.retryable_events>value.pending_events||(!value.pending_events&&!value.unresolved_events))return '';return words.captureTransportPending.replace('{pending}',value.pending_events).replace('{retryable}',value.retryable_events).replace('{unresolved}',value.unresolved_events);}
    function phaseHealthLines(value,words){const lines=[],b=value?.protected_backlog,r=value?.latest_delivery_registration;if(Number.isSafeInteger(b?.batches)&&b.batches>0&&Number.isSafeInteger(b.phases)&&b.phases>=0)lines.push(words.stageProtectedBacklog.replace('{batches}',b.batches).replace('{phases}',b.phases));if(['not_registered','pending'].includes(r?.state)&&['task_missing','task_not_delivered','delivery_already_registered','delivery_not_registered','awaiting_capture_seal'].includes(r.reason))lines.push(words['stageRegistration_'+r.reason]);return lines;}
    function stageReview(summary) {
      var value = summary?.phase_review || summary?.panel_snapshot?.phase_review;
      var host = summary?.host_scope || summary?.panel_snapshot?.host_scope;
      return value?.policy?.mode === "phase" && value.host_scope === host ? value : null;
    }

    function actionError(error, labels) {
      var message = String((error && error.message) || labels.failed);
      return message.includes("stage review uses token budgets") ? labels.tokenEstimate : message;
    }

    Object.assign(TEXT.zh, {
      status_query_budget_exceeded: "状态读取超过本地查询预算，可刷新重试。这与模型额度无关。",
      status_database_read_unavailable: "暂时无法读取本地状态，可刷新重试。",
      status_host_scope_uninitialized: "当前宿主尚未初始化，请重新加载 Agent Learning 后刷新。",
      status_timeout: "本地状态读取超时，可刷新重试。"
    });
    Object.assign(TEXT.en, {
      status_query_budget_exceeded: "The local status query exceeded its read budget. Refresh to retry; this is unrelated to model quota.",
      status_database_read_unavailable: "Local status is temporarily unavailable. Refresh to retry.",
      status_host_scope_uninitialized: "This host is not initialized. Reload Agent Learning, then refresh.",
      status_timeout: "The local status request timed out. Refresh to retry."
    });

    function statusError(error, labels) {
      return labels["status_" + (error && error.reason)] || labels.status_database_read_unavailable;
    }

    Object.assign(TEXT.zh, {reason:"当前原因",dispatch_failed_call_requires_review:"旧失败调用待确认，可在审批与设置中选择恢复操作。",dispatch_unknown_call_requires_review:"上次调用结果待确认，请先核对后再决定是否重试。"});
    Object.assign(TEXT.en, {reason:"Current reason",dispatch_failed_call_requires_review:"A previous call failed. Review recovery options in Approval & settings.",dispatch_unknown_call_requires_review:"A previous call outcome is unknown. Verify it before deciding whether to retry."});
    Object.assign(TEXT.zh, {dispatch_protected_history_only:"现有调用保护仍保留，当前没有可直接送审的新工作；待处置阶段另列，旧调用不会自动重试。"});
    Object.assign(TEXT.en, {dispatch_protected_history_only:"Existing call protections remain; no new work is directly reviewable. Stages awaiting action are listed separately; old calls will not retry automatically."});

    Object.assign(TEXT.zh, {dispatch_configured_launcher_missing:"已配置的管理员启动器文件不存在。请通过 admin-configure 明确更新启动命令；仅切换模型或开启审核仍保留旧命令，插件不会自动替换。",dispatch_cli_permission_denied:"管理员启动器访问被拒绝，请检查文件权限；插件没有替换启动器。"});
    Object.assign(TEXT.en, {dispatch_configured_launcher_missing:"The configured administrator launcher no longer exists. Explicitly update its command with admin-configure; changing the model or enabling review keeps the old command. No launcher was substituted.",dispatch_cli_permission_denied:"Access to the administrator launcher was denied. Check its permissions; the launcher was not replaced."});

    function reasonText(value, labels) {
      return typeof value === "string" && Object.prototype.hasOwnProperty.call(labels, "dispatch_" + value)
        ? labels["dispatch_" + value] : value;
    }

    function getLabels(activeLocale) {
      var lang = String(
        activeLocale ||
          (typeof document !== "undefined" && document.documentElement && document.documentElement.lang) ||
          (typeof navigator !== "undefined" && navigator.language) ||
          "zh"
      ).toLowerCase();
      return lang.indexOf("en") === 0 ? TEXT.en : TEXT.zh;
    }

    function bookIcon(h, size) {
      var px = size || 15;
      return h(
        "svg",
        {
          width: px,
          height: px,
          viewBox: "0 0 16 16",
          fill: "none",
          style: { flex: "none" },
        },
        h("path", {
          d: "M2.5 3.2C2.5 2.54 3.04 2 3.7 2H7.2C7.64 2 8 2.36 8 2.8V13.2C8 12.76 7.64 12.4 7.2 12.4H3.7C3.04 12.4 2.5 11.86 2.5 11.2V3.2Z",
          stroke: "currentColor",
          strokeWidth: 1.35,
          strokeLinejoin: "round",
        }),
        h("path", {
          d: "M13.5 3.2C13.5 2.54 12.96 2 12.3 2H8.8C8.36 2 8 2.36 8 2.8V13.2C8 12.76 8.36 12.4 8.8 12.4H12.3C12.96 12.4 13.5 11.86 13.5 11.2V3.2Z",
          stroke: "currentColor",
          strokeWidth: 1.35,
          strokeLinejoin: "round",
        })
      );
    }

    function fetchStatus(cwd, sessionId) {
      var params = [];
      if (cwd) params.push("cwd=" + encodeURIComponent(cwd));
      if (sessionId) params.push("sessionId=" + encodeURIComponent(sessionId));
      var url = params.length ? ROUTE + "?" + params.join("&") : ROUTE;
      var read = fetch(url).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok || !body.ok) {
            var error = new Error("project status unavailable");
            error.reason = body && body.reason;
            throw error;
          }
          return body;
        });
      });
      if (typeof setTimeout !== "function" || typeof clearTimeout !== "function") return read;
      var timer;
      var deadline = new Promise(function (_resolve, reject) {
        timer = setTimeout(function () {
          var error = new Error("project status timed out");
          error.reason = "timeout";
          reject(error);
        }, 15000);
      });
      return Promise.race([read, deadline]).finally(function () { clearTimeout(timer); });
    }

    function postAction(action, cwd, sessionId, extra) {
      return fetch(ROUTE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(
          Object.assign({ action: action, cwd: cwd || undefined, sessionId: sessionId || undefined }, extra || {})
        ),
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok || !body.ok) {
            var error = new Error((body && body.error) || "action error");
            error.status = r.status;
            throw error;
          }
          return body;
        });
      });
    }

    function currentPresetOf(summary) {
      if (summary?.admin_admission_enabled === false) return "off";
      if (summary?.admin_admission_enabled !== true) return "custom";
      if (stageReview(summary)) return "custom";
      var daily = summary.project_usage?.scope === "project" && summary.project_usage.project_id === summary.project_id ? summary.project_usage.max_calls_per_day : null;
      if (daily === 5) return "minimal";
      if (daily === 15) return "light";
      if (daily === 100) return "balanced";
      if (daily === 300) return "active";
      return "custom";
    }

    function installedProject(summary) {
      return !!summary && summary.ok === true && summary.installed === true &&
        typeof summary.project_id === "string" && summary.project_id.trim().length > 0;
    }


    Object.assign(TEXT.zh, {
      title: "项目知识库", subtitle: "本地记忆 · 按需检索 · 可追溯证据",
      openBrowser: "浏览知识库（只读）", openReview: "审批与管理设置", historyPending: "历史待办（需独立授权）",
      overview: "项目概览", awaitingApproval: "待批准", reviewStatus: "自动整理状态",
      admissionOn: "允许新审核", admissionOff: "新审核已暂停", policy: "策略开关",
      route: "模型路由", budget: "预算准入", reviewMode: "审核方式", execution: "执行状态",
      ready: "已就绪", notReady: "未就绪", available: "可用", unavailable: "受限",
      phaseMode: "阶段集中审核", legacyMode: "逐案审核（兼容）", defaultPolicy: "默认策略",
      idle: "空闲", worker_reserved: "已请求工作器", worker_claimed: "工作器已启动",
      model_reserved: "模型调用已预留", model_launching: "模型进程启动中", model_started: "模型进程已启动",
      today: "今日审核", usageDetails: "用量与队列详情", reviewSettings: "审核设置与频率",
      maintenance: "维护与诊断", queueNote: "待审、待补证、待批准分别统计，不等于立即可运行的调用数。",
      awaitingEvidence: "待补证 / 本地暂缓", observedAt: "状态读取于", staleStatus: "读取失败，保留上次成功状态",
      refreshHint: "打开时读取一次；刷新不会启动审核。", safetyNote: "打开面板不会启用管理员、归档历史或发布知识。",
      close: "关闭知识库面板", version: "插件版本", technical: "项目与安装信息",
      dispatch_dsh_profile_unavailable: "当前查询环境没有可用的 DSH 模型配置快照。",
      dispatch_dsh_profile_source_unbound: "来源会话尚未绑定 DSH profile，等待后续受信事件。",
      dispatch_dsh_profile_source_mismatch: "DSH profile 与来源会话不一致，不能借用其他配置。",
      dispatch_disabled: "新审核准入已关闭；已有工作记录仍可检索。",
      dispatch_no_increment: "当前没有新的可审核材料。", dispatch_queue_not_checked: "本次未检查审核队列。",
      dispatch_model_missing: "等待可确认的模型选择。", dispatch_available: "预算可用",
      dispatch_concurrency_limited: "已有审核占用并发额度。", dispatch_token_budget_exhausted: "今日 token 预算不足。"
    });
    Object.assign(TEXT.en, {
      title: "Project knowledge", subtitle: "Local memory · On-demand retrieval · Traceable evidence",
      openBrowser: "Browse knowledge (read-only)", openReview: "Review and settings", historyPending: "Historical units (separate authorization)",
      overview: "Project overview", awaitingApproval: "Awaiting approval", reviewStatus: "Automatic review",
      admissionOn: "New reviews allowed", admissionOff: "New reviews paused", policy: "Policy switch",
      route: "Model routing", budget: "Budget admission", reviewMode: "Review method", execution: "Execution",
      ready: "Ready", notReady: "Not ready", available: "Available", unavailable: "Limited",
      phaseMode: "Stage batches", legacyMode: "Case review (legacy)", defaultPolicy: "Default policy",
      idle: "Idle", worker_reserved: "Worker requested", worker_claimed: "Worker started",
      model_reserved: "Model call reserved", model_launching: "Model process starting", model_started: "Model process started",
      today: "Reviews today", usageDetails: "Usage and queue details", reviewSettings: "Review settings and frequency",
      maintenance: "Maintenance and diagnostics", queueNote: "Review, evidence and approval waits are separate, not immediately runnable calls.",
      awaitingEvidence: "Evidence wait / locally deferred", observedAt: "Status read at", staleStatus: "Read failed; showing the last successful status",
      refreshHint: "Read once on open; refreshing starts no review.", safetyNote: "Opening this panel enables no reviewer, history import or publication.",
      close: "Close knowledge panel", version: "Plugin version", technical: "Project and installation",
      dispatch_dsh_profile_unavailable: "This query environment has no available DSH model configuration snapshot.",
      dispatch_dsh_profile_source_unbound: "The source chat has no DSH profile binding yet; waiting for a trusted event.",
      dispatch_dsh_profile_source_mismatch: "The DSH profile differs from the source chat; another profile cannot be borrowed.",
      dispatch_disabled: "New review admission is off; recorded work remains searchable.",
      dispatch_no_increment: "No new reviewable material.", dispatch_queue_not_checked: "The review queue was not checked.",
      dispatch_model_missing: "Waiting for a confirmed model selection.", dispatch_available: "Budget available",
      dispatch_concurrency_limited: "Another review occupies the concurrency allowance.", dispatch_token_budget_exhausted: "The daily token budget is insufficient."
    });

    Object.assign(TEXT.zh, {
      recommendedSettings: "推荐设置", advancedSettings: "高级设置", currentSettings: "当前方案", freqCustom: "自定义", workspace: "工作区",
      recommendedLight: "省流整理（推荐）", recommendedOff: "暂停 AI 整理，保留本地记忆",
      recommendationHint: "省流方案：最多 15 次/天、每批 2 次。只是预选；确认后才会启用 AI 整理并使用模型额度。",
      applyRecommendation: "确认并应用", recommendationApplied: "当前已是此方案",
      preserveStage: "建议保留当前审核方式、频率与 token 预算，不套用按次数方案。需要调整时打开管理页。",
      routeNotReady: "模型路由尚未就绪，请先在高级设置中核对。推荐方案不会修复模型配置。",
      confirmPause: "暂停新的 AI 整理？本地记录、知识检索和已有知识保留。",
      simpleSafety: "打开面板或查看推荐，不会更改设置或启动 AI 整理。"
    });
    Object.assign(TEXT.en, {
      recommendedSettings: "Recommended settings", advancedSettings: "Advanced settings", currentSettings: "Current preset", freqCustom: "Custom", workspace: "Workspace",
      recommendedLight: "Light review (recommended)", recommendedOff: "Pause AI review; keep local memory",
      recommendationHint: "Light: at most 15 calls/day, 2 per batch. This is only a suggestion; confirmation enables AI review and uses model quota.",
      applyRecommendation: "Confirm and apply", recommendationApplied: "Already using this preset",
      preserveStage: "Keep the current review mode, frequency and token budget. Call-count presets are not applied; use management to adjust them.",
      routeNotReady: "Model routing is not ready. Check Advanced settings first; applying a preset cannot fix model configuration.",
      confirmPause: "Pause new AI reviews? Local recording, retrieval and existing knowledge are retained.",
      simpleSafety: "Opening this panel or viewing recommendations changes no settings and starts no AI review."
    });

    var PANEL_CSS = `
.al-panel{display:flex;flex-direction:column;gap:16px;min-width:0;color:var(--dsw-alias-label-primary);font-size:13px;line-height:1.5;font-family:inherit}
.al-panel *{box-sizing:border-box}.al-panel button{font:inherit;cursor:pointer;color:var(--dsw-alias-label-primary)}
.al-panel button:disabled{opacity:.55;cursor:default}.al-panel button:focus-visible,.al-panel summary:focus-visible,.al-panel select:focus-visible,.al-trigger:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:3px}
.al-panel-head,.al-section-head,.al-status-row,.al-actions button{display:flex;align-items:center;justify-content:space-between;gap:12px}
.al-panel-title{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:650}.al-caption{font-size:11.5px;color:var(--dsw-alias-label-secondary);overflow-wrap:anywhere}
.al-head-actions{display:flex;gap:6px;flex-shrink:0}.al-panel .al-quiet{border:1px solid var(--dsw-alias-border-l1);background:var(--dsw-alias-bg-layer-1);border-radius:7px;padding:4px 8px;font-size:11.5px}
.al-panel .al-quiet:hover,.al-actions button:hover{background:var(--dsw-alias-bg-layer-2)}
.al-project{border-left:3px solid var(--dsw-alias-brand-primary);padding-left:10px;min-width:0}.al-project strong{display:block;font-size:14px;overflow-wrap:anywhere}.al-project-path{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.al-section-head{font-size:12px;font-weight:600;margin-bottom:8px}.al-overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}
.al-stat{border:1px solid var(--dsw-alias-border-l1);border-radius:9px;padding:10px 8px;background:var(--dsw-alias-bg-layer-1);min-width:0}.al-stat span{display:block;font-size:10.5px;color:var(--dsw-alias-label-secondary);overflow-wrap:anywhere}.al-stat strong{display:block;font-size:21px;line-height:1.4;font-weight:650;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.al-compact-overview{display:flex;align-items:center;justify-content:space-between;gap:16px}.al-compact-overview .al-stat{border:0;background:transparent;padding:0}.al-compact-status{display:flex;flex-direction:column;gap:5px;align-items:flex-end;text-align:right;min-width:0}
.al-recommendation{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;padding:12px;background:var(--dsw-alias-bg-layer-1);display:flex;flex-direction:column;gap:10px}.al-recommendation label{font-size:12px}.al-recommendation select{display:block;width:100%;margin-top:5px;min-width:0;border:1px solid var(--dsw-alias-border-l2);border-radius:7px;padding:8px;background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary);font:inherit}
.al-advanced>.al-details-body{gap:16px}
.al-review{border:1px solid var(--dsw-alias-border-l1);border-radius:10px;padding:12px;background:var(--dsw-alias-bg-layer-1)}
.al-status-row{padding:4px 0;font-size:12px;align-items:baseline}.al-status-row>span:first-child{color:var(--dsw-alias-label-secondary);flex-shrink:0}.al-status-row>span:last-child{text-align:right;overflow-wrap:anywhere;min-width:0}
.al-badge{display:inline-flex;align-items:center;border:1px solid var(--dsw-alias-border-l1);border-radius:20px;padding:2px 8px;font-size:11px;font-weight:500;color:var(--dsw-alias-label-secondary);max-width:100%;overflow-wrap:anywhere}
.al-badge[data-tone=ready]{color:var(--dsw-alias-state-success-primary)}.al-badge[data-tone=warn]{color:var(--dsw-alias-state-warn-primary)}
.al-notice{border-left:2px solid var(--dsw-alias-state-warn-primary);padding:5px 9px;margin-top:8px;font-size:11.5px;overflow-wrap:anywhere;white-space:pre-wrap;color:var(--dsw-alias-label-secondary)}
.al-usage-line{display:flex;flex-wrap:wrap;gap:5px 14px;font-size:12px;font-variant-numeric:tabular-nums}.al-usage-line strong{font-weight:650}.al-panel details{border-top:1px solid var(--dsw-alias-border-l1);padding-top:10px;min-width:0}.al-panel summary{font-size:12px;font-weight:550;cursor:pointer;color:var(--dsw-alias-label-secondary)}.al-details-body{padding-top:10px;display:flex;flex-direction:column;gap:8px}
.al-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.al-actions button{appearance:none;width:100%;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;background:var(--dsw-alias-bg-layer-1);padding:10px;font-size:12px;text-align:left;line-height:1.5}.al-actions .al-primary{background:var(--dsw-alias-brand-primary);color:var(--dsw-alias-bg-base);border-color:var(--dsw-alias-brand-primary)}.al-actions .al-primary:hover{opacity:.9}.al-actions.al-maintenance{grid-template-columns:1fr}
.al-presets{display:flex;gap:5px;flex-wrap:wrap}.al-presets button{appearance:none;background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-border-l1);padding:5px 8px;border-radius:7px;font-size:11px}.al-presets button[aria-pressed=true]{border-color:var(--dsw-alias-brand-primary);background:var(--dsw-alias-bg-layer-2);font-weight:600}
.al-panel-foot{font-size:10.5px;color:var(--dsw-alias-label-secondary)}.al-panel-note{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;border-left:2px solid var(--dsw-alias-brand-primary);padding-left:9px}
.al-trigger{display:inline-flex;align-items:center;gap:5px;height:28px;padding:0 10px;border-radius:999px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-secondary);font:inherit;font-size:12.5px;cursor:pointer}.al-trigger:hover,.al-trigger[aria-expanded=true]{background:var(--dsw-alias-bg-layer-2)}
.al-dialog{width:min(460px,calc(100vw - 24px));max-height:calc(100vh - 32px);max-height:calc(100dvh - 32px);margin:auto;overflow:auto;overscroll-behavior:contain;padding:20px;border-radius:14px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-overlay);color:var(--dsw-alias-label-primary);font:inherit}.al-dialog::backdrop{background:color-mix(in srgb,var(--dsw-alias-bg-base) 70%,transparent)}
@media(max-width:380px){.al-dialog{padding:14px}.al-overview{grid-template-columns:repeat(2,minmax(0,1fr))}.al-actions{grid-template-columns:1fr}}
`;

    function reviewRows(summary, t) {
      var a = summary?.administrator || {}, admitted = a.admission_enabled ?? summary?.admin_admission_enabled;
      var binary = function (value, yes, no) { return value === true ? yes : value === false ? no : t.unknown; };
      var budget = summary?.admission?.admission_status;
      var mode = a.review_mode || summary?.phase_review?.policy?.mode || summary?.panel_snapshot?.phase_review?.policy?.mode;
      var rows = [
        [t.policy, binary(a.policy_enabled, t.adminOn, t.adminOff)],
        [t.route, binary(a.routing_ready, t.ready, t.notReady)],
        [t.budget, budget === 'available' ? t.available : budget ? t.unavailable : t.unknown],
        [t.reviewMode, mode === 'phase' ? t.phaseMode : mode === 'legacy' ? t.legacyMode : t.unknown],
        [t.execution, t[a.phase] || (a.phase ? a.phase : t.unknown)]
      ];
      return { admitted: admitted, label: binary(admitted, t.admissionOn, t.admissionOff), rows: rows,
        reason: a.routing_reason, model: a.model, configured: a.review_configured, mode: mode };
    }

    function buildControlPanel(react, localeRef) {
      var h = react.createElement;

      return function AgentLearningPanel(props) {
        var cwd = props && props.cwd ? props.cwd : "";
        var sessionId = props && props.sessionId ? props.sessionId : "";
        var activeLocale = localeRef && localeRef.current ? localeRef.current.getSnapshot().active : "";
        var t = getLabels(activeLocale);
        var scopeKey = JSON.stringify([cwd, sessionId]);
        // A new mount must not relabel an outer component's cached summary.
        var initialSummary = props && props.initialScope === scopeKey ? props.initialSummary : null;
        var statePair = react.useState(initialSummary || null);
        var notePair = react.useState("");
        var busyPair = react.useState(false);
        var localRequestCell = react.useState({ current: null, busy: false })[0];
        var requestCell = props.requestCell || localRequestCell;
        var statusCell = react.useState({ scope: scopeKey, summaryScope: initialSummary ? scopeKey : null, request: 0, failed: false, failedScope: null })[0];
        var noteScopePair = react.useState(null);
        var refreshPair = react.useState(false);
        var refreshing = refreshPair[0], setRefreshing = refreshPair[1];
        var recommendationPair = react.useState({scope: scopeKey, open: false, preset: "light"});
        var recommendation = recommendationPair[0].scope === scopeKey ? recommendationPair[0] : {scope: scopeKey, open: false, preset: "light"};
        var setRecommendation = function (change) {
          if (statusCell.scope === scopeKey) recommendationPair[1](Object.assign({}, recommendation, change));
        };
        statusCell.scope = scopeKey;
        var summary = statusCell.summaryScope === scopeKey ? statePair[0] : null;
        var setSummary = statePair[1];
        var note = noteScopePair[0] === scopeKey ? notePair[0] : "";
        if (!note && requestCell.current) note = t.unresolved;
        var setNote = function (value) {
          if (statusCell.scope !== scopeKey) return;
          noteScopePair[1](scopeKey);
          notePair[1](value);
        };
        var busy = busyPair[0];
        var setBusy = busyPair[1];
        var pendingSettings = !!requestCell.current;

        var reload = react.useCallback(
          function () {
            var request = ++statusCell.request;
            setRefreshing(true);
            return fetchStatus(cwd, sessionId)
              .then(function (data) {
                if (statusCell.scope !== scopeKey || statusCell.request !== request) return;
                statusCell.summaryScope = scopeKey;
                setSummary(data);
                if (statusCell.failed) setNote("");
                statusCell.failed = false;
                statusCell.failedScope = null;
              })
              .catch(function (err) {
                if (statusCell.scope !== scopeKey || statusCell.request !== request) return;
                statusCell.failed = true;
                statusCell.failedScope = scopeKey;
                setNote(statusError(err, t));
              }).finally(function () {
                if (statusCell.scope === scopeKey && statusCell.request === request) setRefreshing(false);
              });
          },
          [cwd, sessionId, t.failed]
        );

        react.useEffect(
          function () {
            reload();
            return function () { ++statusCell.request; };
          },
          [reload]
        );

        react.useEffect(function () {
          recommendationPair[1](function (old) {
            return old.scope === scopeKey ? old : {scope: scopeKey, open: false, preset: "light"};
          });
        }, [scopeKey]);

        function trigger(action, startMsg, doneFn, extra) {
          if (statusCell.scope !== scopeKey || requestCell.busy || !installedProject(summary)) return;
          extra = Object.assign({project_id: summary.project_id,
            installation_id: summary.panel_snapshot?.installation_id}, extra || {});
          if (action === "set_admin_preset") {
            if (!summary.panel_snapshot) { setNote(t.projectUnavailable); return; }
            if (requestCell.current) {
              if (requestCell.current.preset !== extra.preset) { setNote(t.unresolved); return; }
              extra = requestCell.current;
            } else {
              if (extra.preset !== "off" && !window.confirm(t.confirmAdmin)) return;
              extra = Object.assign(extra, {
                panel_snapshot: JSON.parse(JSON.stringify(summary.panel_snapshot)), confirm: true,
                request_id: window.crypto?.randomUUID ? window.crypto.randomUUID() : "panel-" + Date.now() + "-" + Math.random().toString(16).slice(2),
              });
              requestCell.current = extra;
            }
          }
          requestCell.busy = true;
          if (props.onLockChange) props.onLockChange(true);
          setBusy(true);
          setNote(startMsg);
          postAction(action, cwd, sessionId, extra)
            .then(function (res) {
              requestCell.busy = false;
              if (action === "set_admin_preset" && requestCell.current?.request_id === extra.request_id) requestCell.current = null;
              if (props.onLockChange) props.onLockChange(false);
              setBusy(false);
              setNote(doneFn(res));
              reload();
            })
            .catch(function (err) {
              requestCell.busy = false;
              setBusy(false);
              setNote(actionError(err, t));
              if (props.onLockChange) props.onLockChange(false);
              if (action === "set_admin_preset") {
                postAction("admin_receipt", cwd, sessionId, {project_id: extra.project_id,
                  installation_id: extra.installation_id, request_id: extra.request_id})
                  .then(function (receipt) {
                    if (receipt.found) {
                      if (requestCell.current?.request_id === extra.request_id) requestCell.current = null;
                      setNote(doneFn(receipt.response)); reload();
                    }
                    else if (err.status === 400 || err.status === 409) {
                      if (requestCell.current?.request_id === extra.request_id) requestCell.current = null;
                      setNote(actionError(err, t)); reload();
                    } else { setNote(t.unresolved); reload(); }
                  }).catch(function () { setNote(t.unresolved); });
              }
            });
        }


        var display = function (value) { return value ?? t.unknown; };
        var statCell = function (label, value) {
          return h("div", {className: "al-stat"}, h("span", null, label), h("strong", null, String(display(value))));
        };
        var row = function (label, value, key) {
          return h("div", {className: "al-status-row", key: key || label}, h("span", null, label), h("span", null, String(display(value))));
        };
        var actionButton = function (label, action, done, primary) {
          return h("button", {type: "button", "data-action": action, disabled: busy || pendingSettings, className: primary ? "al-primary" : undefined,
            onClick: function () { trigger(action, action === "archive_history" ? t.archiving : t.opening, done); }},
            h("span", null, label), h("span", {"aria-hidden": true}, action === "stop_browser" ? "■" : "↗"));
        };
        var projectUsage = summary?.project_usage?.scope === "project" && summary.project_usage.project_id === summary.project_id ? summary.project_usage : null;
        var globalUsage = summary?.global_usage || {};
        var staged = stageReview(summary), tokenMode = !!staged || projectUsage?.budget_mode === "tokens";
        var activePreset = currentPresetOf(summary), review = reviewRows(summary, t);
        var phaseMode = !!staged || review.mode === "phase";
        var preserveRecommendation = phaseMode || tokenMode;
        var presetChips = phaseMode ? [["off", t.freqOff]] : [
          ["off", t.freqOff], ["minimal", t.freqMinimal], ["light", t.freqLight], ["balanced", t.freqBalanced], ["active", t.freqActive]
        ];
        var phaseNotes = [];
        if (staged) {
          var phases = staged.phases || {}, latest = staged.recent_batches?.[0];
          phaseNotes.push(t.stageFrequency.replace("{n}", staged.policy.every_phases));
          phaseNotes.push(t.stageQueue + ": " + ["awaiting_stop","sealed","queued","partial","completed"].map(function (key) { return phases[key] ?? t.unknown; }).join(" / "));
          var deferred = staged.locally_deferred_phase_count ?? phases.routine_deferred ?? 0;
          if (deferred) phaseNotes.push(t.stageLocallyDeferred.replace("{n}", deferred));
          phaseNotes.push.apply(phaseNotes, phaseHealthLines(staged, t));
          if (latest) phaseNotes.push(t.stageCoverage + ": " + (latest.coverage_complete === true ? t.coverageFull : latest.coverage_complete === false ? t.coveragePartial : t.unknown));
        }
        var captureNote = captureTransportLine(summary?.capture_transport, t);
        var readFailed = statusCell.failed && statusCell.failedScope === scopeKey;
        var panelHeader = h("div", {className: "al-panel-head"},
          h("div", {className: "al-panel-title"}, bookIcon(h, 18), t.title),
          h("div", {className: "al-head-actions"},
            h("button", {type: "button", onClick: reload, disabled: refreshing, className: "al-quiet", title: t.refreshHint}, t.refresh),
            props.onClose ? h("button", {type: "button", onClick: props.onClose, disabled: busy, className: "al-quiet", "aria-label": t.close}, "×") : null));
        if (!installedProject(summary)) {
          return h("div", {className: "al-panel"}, h("style", null, PANEL_CSS), panelHeader,
            h("div", {role: "status", className: "al-caption"}, readFailed ? note : summary ? t.projectRequired : t.loading));
        }
        var executionReason = summary.diagnostic?.reason;
        var notices = [];
        if (review.reason) notices.push(reasonText(review.reason, t));
        if (executionReason && executionReason !== review.reason && !["no_increment", "queue_not_checked"].includes(executionReason)) notices.push(reasonText(executionReason, t));
        if (summary.admission?.blocking_scope) notices.push(summary.admission.blocking_scope);
        if (captureNote) notices.push(captureNote);
        var presetName = function (preset) {
          return ({off: t.freqOff, minimal: t.freqMinimal, light: t.freqLight, balanced: t.freqBalanced, active: t.freqActive})[preset] || t.freqCustom;
        };
        var currentRecommendationName = phaseMode ? t.phaseMode : review.admitted === false ? t.freqOff :
          review.admitted !== true || projectUsage?.max_calls_per_day == null ? t.unknown : presetName(activePreset);
        var selectedRecommendation = pendingSettings && ["light", "off"].includes(requestCell.current.preset)
          ? requestCell.current.preset : recommendation.preset;
        var recommendationUnchanged = !pendingSettings && (selectedRecommendation === "off" ? review.admitted === false :
          review.admitted === true && [["max_calls_per_day", 15], ["max_calls_per_batch", 2], ["max_batch_seconds", 180], ["max_run_seconds", 300]]
            .every(function (pair) { return summary.panel_snapshot?.project_limits?.[pair[0]] === pair[1]; }));
        var recommendationBlocked = busy || !projectUsage || !summary.panel_snapshot ||
          (pendingSettings && requestCell.current.preset !== selectedRecommendation) ||
          (!pendingSettings && selectedRecommendation !== "off" && summary.administrator?.routing_ready === false);
        return h("div", {className: "al-panel", "aria-busy": busy || refreshing},
          h("style", null, PANEL_CSS), panelHeader,
          h("div", {className: "al-project", title: summary.workspace || ""}, h("strong", null, summary.project_name || summary.project_id)),
          readFailed ? h("div", {className: "al-notice", role: "status"}, t.staleStatus) : null,
          h("section", {"aria-label": t.overview, className: "al-compact-overview"},
            statCell(t.knowledge, summary.active_knowledge),
            h("div", {className: "al-compact-status"}, h("span", {className: "al-caption"}, t.reviewStatus),
              h("span", {className: "al-badge", "data-tone": "neutral"}, review.label))),
          h("div", {className: "al-actions"},
            actionButton(t.openBrowser, "open_browser", function () { return t.openedBrowser; }, true),
            h("button", {type: "button", "data-action": "show_recommendations", "aria-expanded": recommendation.open,
              onClick: function () { setRecommendation({open: !recommendation.open}); }},
              h("span", null, t.recommendedSettings), h("span", {"aria-hidden": true}, recommendation.open ? "−" : "+"))),
          recommendation.open ? h("section", {className: "al-recommendation", "aria-label": t.recommendedSettings},
            h("div", {className: "al-caption"}, t.currentSettings + ": " + currentRecommendationName),
            preserveRecommendation ? h("div", {className: "al-caption"}, t.preserveStage) :
              h("label", null, t.recommendedSettings,
                h("select", {value: selectedRecommendation, disabled: busy || pendingSettings,
                  onChange: function (event) { setRecommendation({preset: event.target.value}); }},
                  h("option", {value: "light"}, t.recommendedLight), h("option", {value: "off"}, t.recommendedOff))),
            !preserveRecommendation ? h("div", {className: "al-caption"}, t.recommendationHint) : null,
            !preserveRecommendation && summary.administrator?.routing_ready === false ? h("div", {className: "al-notice"}, t.routeNotReady) : null,
            preserveRecommendation ? h("div", {className: "al-actions al-maintenance"},
              actionButton(t.openReview, "open_review", function () { return t.openedReview; })) :
              h("div", {className: "al-actions al-maintenance"}, h("button", {type: "button", "data-action": "apply_recommendation",
                disabled: recommendationBlocked || recommendationUnchanged,
                onClick: function () {
                  if (recommendationBlocked || recommendationUnchanged || statusCell.scope !== scopeKey) return;
                  if (selectedRecommendation === "off" && !pendingSettings && !window.confirm(t.confirmPause)) return;
                  trigger("set_admin_preset", t.opening, function () { return t.freqSaved + presetName(selectedRecommendation); }, {preset: selectedRecommendation});
                }}, recommendationUnchanged ? t.recommendationApplied : t.applyRecommendation))) : null,
          captureNote ? h("div", {className: "al-notice", role: "status"}, captureNote) : null,
          h("details", {className: "al-advanced"}, h("summary", null, t.advancedSettings), h("div", {className: "al-details-body"},
          h("div", {className: "al-overview"}, statCell(t.workMemory, summary.work_memories),
            statCell(t.pendingCases, summary.pending_review_cases), statCell(t.awaitingApproval, summary.awaiting_approval_cases)),
          h("div", {className: "al-actions al-maintenance"}, actionButton(t.openReview, "open_review", function () { return t.openedReview; })),
          h("section", {className: "al-review", "aria-label": t.reviewStatus},
            h("div", {className: "al-section-head"}, t.reviewStatus,
              h("span", {className: "al-badge", "data-tone": review.admitted === true ? "ready" : "neutral"}, review.label)),
            review.rows.map(function (pair) { return row(pair[0], pair[1]); }),
            review.configured === false ? h("div", {className: "al-caption"}, t.defaultPolicy) : null,
            review.model ? h("div", {className: "al-caption", title: review.model}, review.model) : null,
            notices.length ? h("div", {className: "al-notice"}, notices.join("\n")) : null),
          h("section", {"aria-label": t.today}, h("div", {className: "al-section-head"}, t.today),
            h("div", {className: "al-usage-line"},
              h("span", null, t.attempts + ": ", h("strong", null, String(display(projectUsage?.counted_attempts)))),
              h("span", null, t.completed + ": ", h("strong", null, String(display(projectUsage?.completed_reviews))))),
            row(tokenMode ? t.remainingTokens : t.remaining, projectUsage ?
              String(display(tokenMode ? projectUsage.remaining_reported_tokens : projectUsage.remaining_calls)) + " / " +
              String(display(tokenMode ? projectUsage.max_reported_tokens_per_day : projectUsage.max_calls_per_day)) : t.unknown)),
          h("details", null, h("summary", null, t.usageDetails), h("div", {className: "al-details-body"},
            row(t.failedAttempts, projectUsage?.failed_attempts),
            row(t.reportedTokens, projectUsage?.reported_tokens), row(t.reservedTokens, projectUsage?.token_reservations),
            row(t.globalCalls, globalUsage.counted_attempts ?? globalUsage.used_calls), row(t.globalRemaining, globalUsage.remaining_calls),
            row(t.awaitingEvidence, summary.awaiting_evidence_cases),
            row(t.historyPending, summary.historical_pending_units),
            h("div", {className: "al-caption"}, projectUsage ? t.scopeNote : t.projectUnavailable),
            h("div", {className: "al-caption"}, t.queueNote),
            projectUsage?.next_reset_at ? h("div", {className: "al-caption"}, t.reset + ": " + new Date(projectUsage.next_reset_at * 1000).toLocaleString(activeLocale || undefined)) : null,
            tokenMode ? h("div", {className: "al-caption"}, t.tokenEstimate) : null,
            phaseNotes.map(function (line, i) { return h("div", {className: "al-caption", key: i}, line); }))),
          h("details", null, h("summary", null, t.reviewSettings), h("div", {className: "al-details-body"},
            h("div", {className: "al-caption"}, staged ? t.tokenEstimate : t.freqTitle),
            h("div", {className: "al-presets"}, presetChips.map(function (pair) {
              return h("button", {key: pair[0], type: "button", "aria-pressed": activePreset === pair[0],
                disabled: busy || !projectUsage || !summary.panel_snapshot || (pendingSettings && requestCell.current.preset !== pair[0]),
                onClick: function () { trigger("set_admin_preset", t.opening, function () { return t.freqSaved + pair[1]; }, {preset: pair[0]}); }}, pair[1]);
            })))),
          h("details", null, h("summary", null, t.maintenance), h("div", {className: "al-details-body al-actions al-maintenance"},
            props.onSendPrompt ? h("button", {type: "button", disabled: busy || pendingSettings, onClick: function () {
              props.onSendPrompt("使用 $learning-debug 只读检查当前项目的 Agent Learning 连接、本轮会话绑定、知识目录和管理员状态，给我简短结果。");
            }}, h("span", null, t.runDebug), h("span", {"aria-hidden": true}, "↵")) : null,
            actionButton(t.archiveHistory, "archive_history", function (res) {
              return ["continue_existing","no_changes"].includes(res.preparation?.action) ? t.reusedHistory : t.archivedPrefix + String(res.cases ?? 0);
            }),
            summary.browser_running ? actionButton(t.stopBrowser, "stop_browser", function (res) {
              if (res.running === false && res.stopped === true) return t.stoppedBrowser;
              if (res.running === false && res.stopped === false) return t.browserNotRunning;
              return t.browserStopUnconfirmed;
            }) : null)),
          h("details", null, h("summary", null, t.technical), h("div", {className: "al-details-body al-caption"},
            row("Project ID", summary.project_id), row("Host", summary.host_scope), row(t.workspace, summary.workspace), row(t.version, summary.plugin_version),
            summary.observed_at ? row(t.observedAt, new Date(summary.observed_at * 1000).toLocaleString(activeLocale || undefined)) : null)))),
          note ? h("div", {role: "status", className: "al-panel-note", "aria-live": "polite"}, note) : null,
          h("div", {className: "al-panel-foot"}, t.simpleSafety));

      };
    }

    function createComposerButton(react, localeRef) {
      var h = react.createElement, Panel = buildControlPanel(react, localeRef);
      return function ComposerLearningButton(props) {
        var activeLocale = localeRef?.current ? localeRef.current.getSnapshot().active : "";
        var t = getLabels(activeLocale), sessionId = props?.sessionId ? String(props.sessionId) : "";
        var sessionCwd = typeof props?.useSessions === "function" && sessionId
          ? props.useSessions(function (s) { return s?.byId?.[sessionId]?.cwd ? String(s.byId[sessionId].cwd) : ""; }) : "";
        var checkPair = react.useState(null), scopeKey = JSON.stringify([sessionCwd, sessionId]);
        var checked = checkPair[0], summary = checked?.scope === scopeKey ? checked.summary : null;
        var setChecked = checkPair[1], openPair = react.useState(false), open = openPair[0], setOpen = openPair[1];
        var dialogRef = react.useRef(null), buttonRef = react.useRef(null), lockRef = react.useRef(false);
        // An uncertain setting request survives closing/reopening this project dialog.
        // Never relabel its frozen snapshot as belonging to another session.
        var pendingRef = react.useRef({scope: null, cell: null});
        if (pendingRef.current.scope !== scopeKey) pendingRef.current = {scope: scopeKey, cell: {current: null, busy: false}};
        function closePanel() { if (!lockRef.current) setOpen(false); }
        react.useEffect(function () {
          setChecked(null); setOpen(false); lockRef.current = false;
          if (!sessionCwd && !sessionId) return;
          var cancelled = false;
          fetchStatus(sessionCwd, sessionId).then(function (data) {
            if (!cancelled) setChecked({scope: scopeKey, summary: installedProject(data) ? data : null});
          }).catch(function () { if (!cancelled) setChecked(null); });
          return function () { cancelled = true; };
        }, [sessionCwd, sessionId]);
        react.useEffect(function () {
          if (!open || !dialogRef.current) return;
          var dialog = dialogRef.current;
          // A native modal enters the browser top layer, outside composer clipping.
          // The browser owns focus trapping, inert background and focus restoration.
          if (typeof dialog.showModal === "function") dialog.showModal();
          else dialog.setAttribute("open", "");
          return function () {
            if (dialog.open && typeof dialog.close === "function") dialog.close();
            buttonRef.current?.focus?.({preventScroll: true});
          };
        }, [open, sessionCwd, sessionId]);
        if (!summary?.installed) return null;
        return h("div", {style: {display: "inline-flex", alignItems: "center"}},
          h("style", null, PANEL_CSS),
          h("button", {type: "button", ref: buttonRef, className: "al-trigger", "aria-haspopup": "dialog", "aria-expanded": open,
            title: t.title + (summary.project_name ? " (" + summary.project_name + ")" : ""),
            onClick: function () { if (open) closePanel(); else setOpen(true); }}, bookIcon(h, 14), h("span", null, t.button)),
          open ? h("dialog", {ref: dialogRef, className: "al-dialog", "aria-label": t.title,
            onCancel: function (event) { event.preventDefault(); closePanel(); },
            onClick: function (event) { if (event.target === event.currentTarget) closePanel(); }},
            h(Panel, {cwd: sessionCwd, sessionId: sessionId, initialSummary: summary, initialScope: scopeKey,
              requestCell: pendingRef.current.cell, onClose: closePanel,
              onLockChange: function (value) { if (pendingRef.current.scope === scopeKey) lockRef.current = value; },
              onSendPrompt: function (prompt) {
                closePanel();
                if (typeof props?.inputActions?.setText === "function") props.inputActions.setText(prompt);
              }})) : null);
      };
    }

    function createSettingsCard(react, localeRef) {
      var h = react.createElement;
      var Panel = buildControlPanel(react, localeRef);
      return function SettingsCard(props) {
        var activeLocale = localeRef && localeRef.current ? localeRef.current.getSnapshot().active : "";
        var t = getLabels(activeLocale);
        if (props && props.view === "summary") {
          return t.subtitle;
        }
        return h(
          "div",
          {
            style: {
              padding: "14px 16px",
              borderRadius: "12px",
              border: "1px solid var(--dsw-alias-border-l2)",
              background: "var(--dsw-alias-bg-layer-1)",
            },
          },
          h(Panel, null)
        );
      };
    }

    function apply(ctx) {
      if (typeof ctx.inject !== "function") return;
      var localeRef = { current: null };
      ctx.inject(["locale"], function (scope) {
        localeRef.current = scope.locale;
        if (typeof scope.effect === "function") {
          scope.effect(function () {
            return function () {
              localeRef.current = null;
            };
          }, "agent-learning: locale");
        }
      });

      ctx.inject(["slots"], function (scope) {
        var react;
        try {
          react = require("react");
        } catch {
          return;
        }
        var ComposerBtn = createComposerButton(react, localeRef);
        var Card = createSettingsCard(react, localeRef);

        scope.slots.inject("conversation.input.left", function* () {
          yield scope.slots.register(
            { name: "conversation.input.left", id: "agent-learning-button", order: 20, label: "知识库" },
            ComposerBtn
          );
        });
        scope.slots.inject("plugins.bundle.config", function* () {
          yield scope.slots.register({ name: "plugins.bundle.config", key: PACKAGE_NAME }, Card);
        });
        scope.slots.inject("settings.plugin.item", function* () {
          yield scope.slots.register(
            { name: "settings.plugin.item", id: PACKAGE_NAME, key: PACKAGE_NAME, order: 35 },
            Card
          );
        });
      });
    }

    exports.apply = apply;
    exports.inject = [];
    return module.exports;
  },
});
