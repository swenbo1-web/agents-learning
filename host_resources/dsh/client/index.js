// Browser half of the agent-learning DSH plugin:
// - Composer toolbar button (`conversation.input.left`) shown ONLY for the bound/installed project
// - Quick administrator frequency presets (Off / Light / Balanced / Active / Minimal + fine-tuning page)
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

    function stageReview(summary) {
      var value = summary?.phase_review || summary?.panel_snapshot?.phase_review;
      var host = summary?.host_scope || summary?.panel_snapshot?.host_scope;
      return value?.policy?.mode === "phase" && value.host_scope === host ? value : null;
    }

    function actionError(error, labels) {
      var message = String((error && error.message) || labels.failed);
      return message.includes("stage review uses token budgets") ? labels.tokenEstimate : message;
    }

    Object.assign(TEXT.zh, {reason:"当前原因",dispatch_failed_call_requires_review:"旧失败调用待确认，可在审批与设置中选择恢复操作。",dispatch_unknown_call_requires_review:"上次调用结果待确认，请先核对后再决定是否重试。"});
    Object.assign(TEXT.en, {reason:"Current reason",dispatch_failed_call_requires_review:"A previous call failed. Review recovery options in Approval & settings.",dispatch_unknown_call_requires_review:"A previous call outcome is unknown. Verify it before deciding whether to retry."});
    Object.assign(TEXT.zh, {dispatch_protected_history_only:"历史调用受保护，暂无可审核新工作；新任务仍可正常审核，旧调用不会自动重试。"});
    Object.assign(TEXT.en, {dispatch_protected_history_only:"Historical calls remain protected; no new reviewable work. New tasks can still be reviewed; old calls will not retry automatically."});

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
      return fetch(url).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok || !body.ok) {
            throw new Error((body && body.error) || "status error");
          }
          return body;
        });
      });
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
      if (!summary || !summary.admin_admission_enabled) return "off";
      if (stageReview(summary)) return "custom";
      var daily = summary.project_usage?.scope === "project" && summary.project_usage.project_id === summary.project_id ? summary.project_usage.max_calls_per_day : null;
      if (daily === 5) return "minimal";
      if (daily === 15) return "light";
      if (daily === 100) return "balanced";
      if (daily === 300) return "active";
      return "custom";
    }

    function buildControlPanel(react, localeRef) {
      var h = react.createElement;

      return function AgentLearningPanel(props) {
        var cwd = props && props.cwd ? props.cwd : "";
        var sessionId = props && props.sessionId ? props.sessionId : "";
        var activeLocale = localeRef && localeRef.current ? localeRef.current.getSnapshot().active : "";
        var t = getLabels(activeLocale);
        var statePair = react.useState(props && props.initialSummary ? props.initialSummary : null);
        var notePair = react.useState("");
        var busyPair = react.useState(false);
        var requestCell = react.useState({ current: null, busy: false })[0];
        var summary = statePair[0];
        var setSummary = statePair[1];
        var note = notePair[0];
        var setNote = notePair[1];
        var busy = busyPair[0];
        var setBusy = busyPair[1];

        var reload = react.useCallback(
          function () {
            fetchStatus(cwd, sessionId)
              .then(function (data) {
                setSummary(data);
              })
              .catch(function (err) {
                setNote(actionError(err, t));
              });
          },
          [cwd, sessionId, t.failed]
        );

        react.useEffect(
          function () {
            reload();
          },
          [reload]
        );

        function trigger(action, startMsg, doneFn, extra) {
          if (requestCell.busy || !summary || !summary.project_id) return;
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
          setBusy(true);
          setNote(startMsg);
          postAction(action, cwd, sessionId, extra)
            .then(function (res) {
              requestCell.busy = false;
              if (action === "set_admin_preset") requestCell.current = null;
              setBusy(false);
              setNote(doneFn(res));
              reload();
            })
            .catch(function (err) {
              requestCell.busy = false;
              setBusy(false);
              setNote(actionError(err, t));
              if (action === "set_admin_preset") {
                postAction("admin_receipt", cwd, sessionId, {project_id: extra.project_id,
                  installation_id: extra.installation_id, request_id: extra.request_id})
                  .then(function (receipt) {
                    if (receipt.found) { requestCell.current = null; setNote(doneFn(receipt.response)); reload(); }
                    else if (err.status === 400 || err.status === 409) {
                      requestCell.current = null; setNote(actionError(err, t)); reload();
                    } else { setNote(t.unresolved); reload(); }
                  }).catch(function () { setNote(t.unresolved); });
              }
            });
        }

        var btnStyle = function (primary) {
          return {
            appearance: "none",
            font: "inherit",
            fontSize: "12.5px",
            lineHeight: 1.4,
            cursor: busy ? "default" : "pointer",
            borderRadius: "8px",
            padding: "7px 11px",
            textAlign: "left",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "8px",
            width: "100%",
            border: primary
              ? "1px solid transparent"
              : "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.3))",
            background: primary
              ? "var(--dsw-alias-label-primary, #1f6feb)"
              : "var(--dsw-alias-bg-layer-3, rgba(127,127,127,0.08))",
            color: primary
              ? "var(--dsw-alias-bg-layer-1, #ffffff)"
              : "var(--dsw-alias-label-primary, inherit)",
            opacity: busy ? 0.6 : 1,
          };
        };

        var statCell = function (label, val) {
          return h(
            "div",
            {
              style: {
                padding: "6px 8px",
                borderRadius: "6px",
                background: "var(--dsw-alias-bg-layer-3, rgba(127,127,127,0.07))",
                fontSize: "12px",
              },
            },
            h(
              "div",
              { style: { color: "var(--dsw-alias-label-tertiary, rgba(127,127,127,0.85))", fontSize: "11px" } },
              label
            ),
            h("div", { style: { fontWeight: 600, marginTop: "2px" } }, String(val))
          );
        };

        var projectUsage = summary?.project_usage?.scope === "project" && summary.project_usage.project_id === summary.project_id ? summary.project_usage : null;
        var globalUsage = summary?.global_usage || {};
        var display = function (value) { return value ?? t.unknown; };
        var staged = stageReview(summary);
        var tokenMode = !!staged || projectUsage?.budget_mode === "tokens";
        var activePreset = currentPresetOf(summary);
        var presetChips = [
          ["off", t.freqOff],
          ["minimal", t.freqMinimal],
          ["light", t.freqLight],
          ["balanced", t.freqBalanced],
          ["active", t.freqActive],
        ];
        if (staged) presetChips = [["off", t.freqOff]];
        var phaseNote = "";
        if (staged) {
          var phases = staged.phases || {}, latest = staged.recent_batches?.[0];
          phaseNote = t.stageFrequency.replace("{n}", staged.policy.every_phases) + "\n" + t.stageQueue + ": " + ["awaiting_stop","sealed","queued","partial","completed"].map(function (key) { return phases[key] || 0; }).join(" / ");
          var deferred = staged.locally_deferred_phase_count ?? phases.routine_deferred ?? 0;
          if (deferred) phaseNote += "\n" + t.stageLocallyDeferred.replace("{n}", deferred);
          if (latest) phaseNote += "\n" + t.stageCoverage + ": " + (latest.coverage_complete === true ? t.coverageFull : latest.coverage_complete === false ? t.coveragePartial : t.unknown);
        }

        return h(
          "div",
          {
            style: {
              display: "flex",
              flexDirection: "column",
              gap: "10px",
              fontSize: "13px",
              color: "var(--dsw-alias-label-primary, inherit)",
            },
          },
          h(
            "div",
            { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px" } },
            h(
              "div",
              { style: { fontWeight: 600, fontSize: "13.5px", display: "flex", alignItems: "center", gap: "6px" } },
              bookIcon(h, 15),
              h("span", null, t.title)
            ),
            h(
              "button",
              {
                type: "button",
                onClick: reload,
                style: {
                  appearance: "none",
                  border: "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.3))",
                  background: "transparent",
                  color: "var(--dsw-alias-label-secondary, inherit)",
                  borderRadius: "6px",
                  padding: "2px 8px",
                  fontSize: "11.5px",
                  cursor: "pointer",
                },
              },
              t.refresh
            )
          ),
          summary && summary.project_name
            ? h(
                "div",
                {
                  style: {
                    fontSize: "11.5px",
                    color: "var(--dsw-alias-label-secondary, rgba(127,127,127,0.9))",
                    padding: "5px 8px",
                    borderRadius: "6px",
                    background: "var(--dsw-alias-bg-layer-3, rgba(127,127,127,0.06))",
                    wordBreak: "break-all",
                  },
                },
                h("strong", null, summary.project_name),
                " · ",
                h("span", { style: { opacity: 0.8 } }, summary.workspace || "")
              )
            : null,
          summary
            ? h(
                "div",
                {
                  style: {
                    display: "grid",
                    gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                    gap: "6px",
                  },
                },
                statCell(t.knowledge, summary.active_knowledge ?? 0),
                statCell(t.workMemory, summary.work_memories ?? 0),
                statCell(t.pendingCases, summary.pending_review_cases ?? "—"),
                statCell(
                  t.admin,
                  summary.admin_admission_enabled ? t.adminOn : t.adminOff
                ),
                statCell(t.attempts, display(projectUsage?.counted_attempts)),
                statCell(t.completed, display(projectUsage?.completed_reviews)),
                statCell(t.failedAttempts, display(projectUsage?.failed_attempts)),
                tokenMode ? statCell(t.reportedTokens, display(projectUsage?.reported_tokens)) : statCell(t.remaining, projectUsage ? display(projectUsage.remaining_calls) + "/" + display(projectUsage.max_calls_per_day) : t.unknown),
                tokenMode ? statCell(t.reservedTokens, display(projectUsage?.token_reservations)) : null,
                tokenMode ? statCell(t.remainingTokens, display(projectUsage?.remaining_reported_tokens) + "/" + display(projectUsage?.max_reported_tokens_per_day)) : null
              )
            : h(
                "div",
                { style: { fontSize: "12px", color: "var(--dsw-alias-label-tertiary, rgba(127,127,127,0.8))" } },
                t.loading
              ),
          summary ? h("div", { style: { fontSize: "11.5px", opacity: 0.85, whiteSpace: "pre-wrap" } },
            (projectUsage ? t.scopeNote : t.projectUnavailable) + "\n" +
            (phaseNote ? phaseNote + "\n" : "") + (tokenMode ? t.tokenEstimate + "\n" : "") +
            t.globalCalls + ": " + display(globalUsage.counted_attempts ?? globalUsage.used_calls) + " · " +
            t.globalRemaining + ": " + display(globalUsage.remaining_calls) +
            (projectUsage?.next_reset_at ? "\n" + t.reset + ": " + new Date(projectUsage.next_reset_at * 1000).toLocaleString(activeLocale || undefined) : "") +
            (summary.admission?.blocking_scope ? "\n" + summary.admission.blocking_scope : "") +
            (summary.diagnostic?.reason ? "\n" + t.reason + ": " + reasonText(summary.diagnostic.reason, t) : "")) : null,
          h(
            "div",
            { style: { display: "flex", flexDirection: "column", gap: "5px" } },
            h(
              "div",
              { style: { fontSize: "11.5px", color: "var(--dsw-alias-label-tertiary, rgba(127,127,127,0.85))" } },
              t.freqTitle
            ),
            h(
              "div",
              { style: { display: "flex", flexWrap: "wrap", gap: "4px" } },
              presetChips.map(function (pair) {
                var key = pair[0];
                var label = pair[1];
                var selected = activePreset === key;
                return h(
                  "button",
                  {
                    key: key,
                    type: "button",
                    disabled: busy || !projectUsage || !summary.panel_snapshot,
                    onClick: function () {
                      if (!projectUsage) return;
                      trigger(
                        "set_admin_preset",
                        t.opening,
                        function () {
                          return t.freqSaved + label;
                        },
                        { preset: key }
                      );
                    },
                    style: {
                      appearance: "none",
                      font: "inherit",
                      fontSize: "11.5px",
                      padding: "4px 8px",
                      borderRadius: "6px",
                      cursor: busy ? "default" : "pointer",
                      border: selected
                        ? "1px solid var(--dsw-alias-label-primary, #1f6feb)"
                        : "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.3))",
                      background: selected
                        ? "var(--dsw-alias-bg-layer-2, rgba(31,111,235,0.18))"
                        : "transparent",
                      color: "var(--dsw-alias-label-primary, inherit)",
                      fontWeight: selected ? 600 : 400,
                    },
                  },
                  label
                );
              })
            )
          ),
          h(
            "div",
            { style: { display: "flex", flexDirection: "column", gap: "6px" } },
            h(
              "button",
              {
                type: "button",
                disabled: busy,
                style: btnStyle(true),
                onClick: function () {
                  trigger("open_review", t.opening, function () {
                    return t.openedReview;
                  });
                },
              },
              h("span", null, t.openReview),
              h("span", { style: { fontSize: "11px", opacity: 0.85 } }, "↗")
            ),
            h(
              "button",
              {
                type: "button",
                disabled: busy,
                style: btnStyle(false),
                onClick: function () {
                  trigger("open_browser", t.opening, function () {
                    return t.openedBrowser;
                  });
                },
              },
              h("span", null, t.openBrowser),
              h("span", { style: { fontSize: "11px", opacity: 0.75 } }, "↗")
            ),
            props && props.onSendPrompt
              ? h(
                  "button",
                  {
                    type: "button",
                    disabled: busy,
                    style: btnStyle(false),
                    onClick: function () {
                      props.onSendPrompt(
                        "使用 $learning-debug 只读检查当前项目的 Agent Learning 连接、本轮会话绑定、知识目录和管理员状态，给我简短结果。"
                      );
                    },
                  },
                  h("span", null, t.runDebug),
                  h("span", { style: { fontSize: "11px", opacity: 0.75 } }, "↵")
                )
              : null,
            h(
              "button",
              {
                type: "button",
                disabled: busy,
                style: btnStyle(false),
                onClick: function () {
                  trigger("archive_history", t.archiving, function (res) {
                    return ["continue_existing","no_changes"].includes(res.preparation?.action) ? t.reusedHistory : t.archivedPrefix + String(res.cases ?? 0);
                  });
                },
              },
              h("span", null, t.archiveHistory),
              h("span", { style: { fontSize: "11px", opacity: 0.75 } }, "⚡")
            ),
            summary && summary.browser_running
              ? h(
                  "button",
                  {
                    type: "button",
                    disabled: busy,
                    style: btnStyle(false),
                    onClick: function () {
                      trigger("stop_browser", t.opening, function (res) {
                        if (res.running === false && res.stopped === true) return t.stoppedBrowser;
                        if (res.running === false && res.stopped === false) return t.browserNotRunning;
                        return t.browserStopUnconfirmed;
                      });
                    },
                  },
                  h("span", null, t.stopBrowser),
                  h("span", { style: { fontSize: "11px", opacity: 0.75 } }, "■")
                )
              : null
          ),
          note
            ? h(
                "div",
                {
                  role: "status",
                  style: {
                    fontSize: "11.5px",
                    color: "var(--dsw-alias-label-secondary, rgba(127,127,127,0.9))",
                    paddingTop: "2px",
                  },
                },
                note
              )
            : null
        );
      };
    }

    function createComposerButton(react, localeRef) {
      var h = react.createElement;
      var Panel = buildControlPanel(react, localeRef);

      return function ComposerLearningButton(props) {
        var activeLocale = localeRef && localeRef.current ? localeRef.current.getSnapshot().active : "";
        var t = getLabels(activeLocale);
        var sessionId = props && props.sessionId ? String(props.sessionId) : "";
        var sessionCwd =
          props && typeof props.useSessions === "function" && sessionId
            ? props.useSessions(function (s) {
                return s && s.byId && s.byId[sessionId] && s.byId[sessionId].cwd
                  ? String(s.byId[sessionId].cwd)
                  : "";
              })
            : "";

        var checkPair = react.useState(null);
        var summary = checkPair[0];
        var setSummary = checkPair[1];
        var openPair = react.useState(false);
        var open = openPair[0];
        var setOpen = openPair[1];
        var wrapRef = react.useRef(null);

        react.useEffect(
          function () {
            if (!sessionCwd && !sessionId) {
              setSummary(null);
              setOpen(false);
              return;
            }
            var cancelled = false;
            fetchStatus(sessionCwd, sessionId)
              .then(function (data) {
                if (!cancelled) {
                  setSummary(data && data.installed ? data : null);
                }
              })
              .catch(function () {
                if (!cancelled) {
                  setSummary(null);
                }
              });
            return function () {
              cancelled = true;
            };
          },
          [sessionCwd, sessionId]
        );

        react.useEffect(
          function () {
            if (!open) return;
            function onDocClick(ev) {
              if (wrapRef.current && !wrapRef.current.contains(ev.target)) {
                setOpen(false);
              }
            }
            document.addEventListener("mousedown", onDocClick);
            return function () {
              document.removeEventListener("mousedown", onDocClick);
            };
          },
          [open]
        );

        // 仅在已安装绑定的项目中显示「知识库」按钮；其他未安装项目隐藏
        if (!summary || !summary.installed) {
          return null;
        }

        var handleSendPrompt = function (promptText) {
          setOpen(false);
          if (props && props.inputActions && typeof props.inputActions.setText === "function") {
            props.inputActions.setText(promptText);
          }
        };

        return h(
          "div",
          {
            ref: wrapRef,
            style: { position: "relative", display: "inline-flex", alignItems: "center" },
          },
          h(
            "button",
            {
              type: "button",
              title: t.title + (summary.project_name ? " (" + summary.project_name + ")" : ""),
              "aria-expanded": open,
              onClick: function () {
                setOpen(!open);
              },
              style: {
                appearance: "none",
                display: "inline-flex",
                alignItems: "center",
                gap: "5px",
                height: "28px",
                padding: "0 10px",
                borderRadius: "999px",
                border: "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.3))",
                background: open
                  ? "var(--dsw-alias-bg-layer-2, rgba(127,127,127,0.16))"
                  : "var(--dsw-alias-bg-layer-3, rgba(127,127,127,0.06))",
                color: "var(--dsw-alias-label-secondary, inherit)",
                font: "inherit",
                fontSize: "12.5px",
                fontWeight: 500,
                cursor: "pointer",
                userSelect: "none",
              },
            },
            bookIcon(h, 14),
            h("span", null, t.button)
          ),
          open
            ? h(
                "div",
                {
                  style: {
                    position: "absolute",
                    bottom: "calc(100% + 8px)",
                    left: 0,
                    width: "335px",
                    padding: "12px",
                    borderRadius: "12px",
                    border: "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.35))",
                    background: "var(--dsw-alias-bg-layer-1, #1e2024)",
                    boxShadow: "0 8px 28px rgba(0,0,0,0.32)",
                    zIndex: 1000,
                  },
                },
                h(Panel, {
                  cwd: sessionCwd,
                  sessionId: sessionId,
                  initialSummary: summary,
                  onSendPrompt: handleSendPrompt,
                })
              )
            : null
        );
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
              border: "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.32))",
              background: "var(--dsw-alias-bg-layer-3, rgba(127,127,127,0.05))",
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
