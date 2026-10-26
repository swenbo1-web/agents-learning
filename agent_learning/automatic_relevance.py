"""Conservative automatic-only filtering of generic requests and delivery status.

This is a material-selection rule, not an expiry, source-health or permission
decision. Manual recall, concrete topics and independent context notices retain
their existing contracts.
"""
from __future__ import annotations

import re


_ZH_ACTIONS = ("推送", "安装", "部署", "提交", "更新", "升级", "测试", "验证", "检查",
               "核对", "修复", "优化", "分析")
_ZH_FILLERS = ("本地", "深度", "详细", "仔细", "认真", "进一步", "继续", "接着", "接下来",
               "当前", "现在", "目前", "这次", "本轮", "做好", "完成", "执行", "进行",
               "帮我", "帮忙", "我们", "这个", "这些", "一下", "一遍", "一轮", "代码",
               "插件", "项目", "流程", "功能", "内容", "所有", "全部", "整体", "重新",
               "并且", "然后", "如何", "怎么", "怎样", "是否", "可以", "需要", "确认",
               "确定", "按此", "方法", "说明", "操作", "查询", "查找", "查看", "看看",
               "先", "再", "请", "吧", "好", "和", "并", "及", "的")
_ZH_GENERIC = re.compile("|".join(re.escape(word) for word in sorted(
    set(_ZH_ACTIONS + _ZH_FILLERS), key=lambda word: (-len(word), word))))
_EN_ACTIONS = (r"push(?:ed|ing)?|install(?:ed|ing|ation)?|deploy(?:ed|ing|ment)?|commit(?:ted|ting)?|"
               r"updat(?:e|ed|ing)|upgrad(?:e|ed|ing)|test(?:ed|ing|s)?|verif(?:y|ied|ying|ication)|"
               r"check(?:ed|ing|s)?|fix(?:ed|ing|es)?|repair(?:ed|ing)?|optimi[sz](?:e|ed|ing|ation)|"
               r"analy[sz](?:e|ed|ing)|analysis|improv(?:e|ed|ing|ement)|refin(?:e|ed|ing|ement)")
_EN_GENERIC = re.compile(r"(?<![a-z0-9_./:\\-])(?:" + _EN_ACTIONS + r"|"
    r"please|help|me|us|our|my|we|this|the|a|an|it|and|then|with|to|of|for|on|in|"
    r"continue|keep|resume|again|now|current|local(?:ly)?|deep(?:ly)?|carefully|thorough(?:ly)?|"
    r"first|next|all|whole|project|plugin|code|flow|workflow|feature|content|confirm(?:ed)?|"
    r"how|guide|procedure|instructions|steps|can|could|you|do|does|need|is|are)(?![a-z0-9_./:\\-])", re.I)
_ACTION = re.compile("|".join(_ZH_ACTIONS) + r"|(?<![a-z0-9_./:\\-])(?:" + _EN_ACTIONS
                     + r")(?![a-z0-9_./:\\-])", re.I)

# Require an identifiable snapshot. Ambiguous material is left to the original
# relevance gates; a generic action is not a reason to hide all project facts.
_STATE_TITLE = re.compile(r"(?:交付|发布|部署|安装|测试|升级|更新|提交)(?:状态|记录|回执|结果)|"
    r"\b(?:delivery|release|deployment|installation|test|upgrade)\s+(?:status|record|receipt|result|report)\b", re.I)
_STATE_CLAIM = re.compile(r"(?:已经|已|尚未|还未|未)(?:完成)?(?:推送|安装|部署|提交|升级|更新)|"
    r"(?:推送|安装|部署|提交|升级|更新)(?:已完成|完成了|完毕)|(?:测试|验收|检查)(?:已)?(?:通过|完成)|"
    r"\b(?:has|have|had|was|were|is|are)\s+(?:not\s+)?(?:yet\s+)?(?:been\s+)?"
    r"(?:pushed|installed|deployed|committed|updated|upgraded)\b|"
    r"\b(?:already|not yet)\s+(?:pushed|installed|deployed|committed|updated|upgraded)\b|"
    r"\b(?:tests?|checks?|validation)\s+(?:have\s+)?(?:passed|completed)\b", re.I)
_GUIDANCE = re.compile(r"指南|步骤|操作顺序|应当|应该|必须|不得|不要|禁止|务必|需要先|如果|否则|若已|若未|"
    r"(?:应|需)(?:先|核对|检查|验证|执行|保持|保留|确认|运行)|"
    r"(?:发布|部署|安装|测试|升级|更新)(?:前|后|时)[\s，,：:]{0,4}(?:应|需|先|必须)|"
    r"\b(?:guide|procedure|steps?|should|must|never|how to|if|when|unless|otherwise)\b", re.I)
_CONSTRAINT = re.compile(r"必须|禁止|不要|不得|保持|保留|仅限|只允许|务必|统一|偏好|以后|希望|"
                         r"(?:\b(?:must|never|always|keep|preserve|only|require|use)\b)", re.I)


def has_constraint(text):
    """Shared with work-history selection so mixed status cannot hide a constraint."""
    return bool(_CONSTRAINT.search(text))


def automatic_action_plan(query):
    """Use the original bounded prompt, before scoring removes request words.

    Remove whole phrases before tokenization: Chinese bridge bigrams such as
    '送安' cannot turn a sequence of generic actions into a concrete subject.
    History markers, versions, errors, paths and domain words are not fillers.
    """
    text = query.casefold()
    remainder = _ZH_GENERIC.sub(" ", _EN_GENERIC.sub(" ", text))
    remainder = re.sub(r"[\s。.!！?？,，;；:：、`'\"()（）]+", "", remainder)
    return {"action_only": bool(_ACTION.search(text)) and not remainder}


def automatic_state_exclusion(body, intent):
    if not intent.get("action_only") or body.get("validation_level") not in {"reported", "model_reported"}:
        return None
    title = body.get("title", "") if isinstance(body.get("title", ""), str) else ""
    content = body.get("content", "") if isinstance(body.get("content", ""), str) else ""
    text = title + "\n" + content
    if _GUIDANCE.search(text) or has_constraint(text):
        return None
    if _STATE_TITLE.search(title) or _STATE_CLAIM.search(content):
        return "generic_delivery_status"
    return None
