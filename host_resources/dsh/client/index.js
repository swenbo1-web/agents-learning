// Browser half of the agent-learning DSH plugin:
// - Composer toolbar button (`conversation.input.left`) shown ONLY for the bound/installed project
// - Plugins page configuration card (`plugins.bundle.config` / `settings.plugin.item`)
window.__ModuleLoader__.load({
  id: "agent-learning",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    var PACKAGE_NAME = "agent-learning";
    var ROUTE = "/agent-learning/project-ui";

    var TEXT = {
      zh: {
        button: "知识库",
        title: "Agent Learning 项目记忆与知识库",
        subtitle: "查看项目知识、工作历史、候选审批与管理员设置",
        currentProject: "当前绑定项目",
        knowledge: "正式知识",
        workMemory: "工作观察",
        pendingCases: "待审案例",
        admin: "自动管理员",
        adminOn: "已开启",
        adminOff: "已关闭",
        openBrowser: "打开可视化知识库（只读）",
        openReview: "打开知识审批与管理员设置",
        runDebug: "发送 $learning-debug 调试检查",
        archiveHistory: "一键吸收当前项目历史知识",
        stopBrowser: "停止后台知识库服务",
        refresh: "刷新",
        loading: "正在读取状态…",
        opening: "正在打开浏览器页面…",
        openedBrowser: "已在浏览器打开只读可视化知识库",
        openedReview: "已在浏览器打开知识审批与管理员设置",
        stoppedBrowser: "已停止后台知识库服务",
        archiving: "正在扫描并归档项目历史…",
        archivedPrefix: "历史吸收完成：已归档案例 ",
        failed: "操作失败",
      },
      en: {
        button: "Memory",
        title: "Agent Learning Project Memory",
        subtitle: "Inspect knowledge, work history, candidate review and administrator settings",
        currentProject: "Bound Project",
        knowledge: "Published Knowledge",
        workMemory: "Work Observations",
        pendingCases: "Pending Cases",
        admin: "Administrator",
        adminOn: "Enabled",
        adminOff: "Disabled",
        openBrowser: "Open Visual Knowledge Browser (Read-Only)",
        openReview: "Open Knowledge Review & Admin Settings",
        runDebug: "Send $learning-debug Check",
        archiveHistory: "Absorb Project History Now",
        stopBrowser: "Stop Background Browser Server",
        refresh: "Refresh",
        loading: "Loading status...",
        opening: "Opening in browser...",
        openedBrowser: "Opened read-only knowledge browser",
        openedReview: "Opened knowledge review & admin settings",
        stoppedBrowser: "Stopped background browser server",
        archiving: "Scanning and archiving project history...",
        archivedPrefix: "History archived: total cases ",
        failed: "Operation failed",
      },
    };

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

    function fetchStatus(cwd) {
      var url = cwd ? ROUTE + "?cwd=" + encodeURIComponent(cwd) : ROUTE;
      return fetch(url).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok || !body.ok) {
            throw new Error((body && body.error) || "status error");
          }
          return body;
        });
      });
    }

    function postAction(action, cwd) {
      return fetch(ROUTE, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: action, cwd: cwd || undefined }),
      }).then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok || !body.ok) {
            throw new Error((body && body.error) || "action error");
          }
          return body;
        });
      });
    }

    function buildControlPanel(react, localeRef) {
      var h = react.createElement;

      return function AgentLearningPanel(props) {
        var cwd = props && props.cwd ? props.cwd : "";
        var activeLocale = localeRef && localeRef.current ? localeRef.current.getSnapshot().active : "";
        var t = getLabels(activeLocale);
        var statePair = react.useState(props && props.initialSummary ? props.initialSummary : null);
        var notePair = react.useState("");
        var busyPair = react.useState(false);
        var summary = statePair[0];
        var setSummary = statePair[1];
        var note = notePair[0];
        var setNote = notePair[1];
        var busy = busyPair[0];
        var setBusy = busyPair[1];

        var reload = react.useCallback(
          function () {
            fetchStatus(cwd)
              .then(function (data) {
                setSummary(data);
              })
              .catch(function (err) {
                setNote(String((err && err.message) || t.failed));
              });
          },
          [cwd, t.failed]
        );

        react.useEffect(
          function () {
            reload();
          },
          [reload]
        );

        function trigger(action, startMsg, doneFn) {
          setBusy(true);
          setNote(startMsg);
          postAction(action, cwd)
            .then(function (res) {
              setBusy(false);
              setNote(doneFn(res));
              reload();
            })
            .catch(function (err) {
              setBusy(false);
              setNote(String((err && err.message) || t.failed));
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
                statCell(t.pendingCases, summary.pending_problems ?? 0),
                statCell(
                  t.admin,
                  (summary.admin_admission_enabled ? t.adminOn : t.adminOff) +
                    " (" +
                    (summary.remaining_calls ?? "-") +
                    ")"
                )
              )
            : h(
                "div",
                { style: { fontSize: "12px", color: "var(--dsw-alias-label-tertiary, rgba(127,127,127,0.8))" } },
                t.loading
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
                    return t.archivedPrefix + String(res.cases ?? 0);
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
                      trigger("stop_browser", t.opening, function () {
                        return t.stoppedBrowser;
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
        var sessionCwd =
          props && typeof props.useSession === "function"
            ? props.useSession(function (s) {
                return s && s.cwd ? s.cwd : "";
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
            if (!sessionCwd) {
              setSummary(null);
              setOpen(false);
              return;
            }
            var cancelled = false;
            fetchStatus(sessionCwd)
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
          [sessionCwd]
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

        // 未安装或未绑定 Agent Learning 的项目对话，完全隐藏知识库图标
        if (!sessionCwd || !summary || !summary.installed) {
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
                    width: "320px",
                    padding: "12px",
                    borderRadius: "12px",
                    border: "1px solid var(--dsw-alias-border-l2, rgba(127,127,127,0.35))",
                    background: "var(--dsw-alias-bg-layer-1, #1e2024)",
                    boxShadow: "0 8px 28px rgba(0,0,0,0.32)",
                    zIndex: 1000,
                  },
                },
                h(Panel, { cwd: sessionCwd, initialSummary: summary, onSendPrompt: handleSendPrompt })
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
