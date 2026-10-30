"""Shared, subject-preserving recognition of action-only follow-ups."""
from __future__ import annotations

import re


_CONTINUATION_ACKS = {
    "继续", "好的", "可以", "接着", "继续吧", "好", "确认", "确定", "同意", "是的", "没问题",
    "continue", "go on", "ok", "okay", "yes", "confirm", "confirmed",
}
_ZH_ACTION = r"(?:确认修复|验证|检查|测试|核对|确认|修复|复查|复验)"
_ZH_CONTINUATION = re.compile(
    r"(?:请|麻烦)?(?:继续|接着|接下来|再|先)?" + _ZH_ACTION
    + r"(?:(?:并且|然后|并|和|及|再|、|，|,)" + _ZH_ACTION + r")*"
    + r"(?:一下|一遍|一轮|下去)?(?:吧)?\Z")
_EN_ACTION = (r"(?:test(?:ing)?|check(?:ing)?|verify|verifying|verification|validate|validating|validation|"
              r"inspect(?:ing)?|fix(?:ing)?|repair(?:ing)?|confirm(?:ing)?(?: (?:the )?(?:fix|repair))?)")
_EN_CONTINUATION = re.compile(
    r"(?:please )?(?:(?:continue|keep|resume|go on|carry on)(?: with| to)? )?" + _EN_ACTION
    + r"(?: (?:and(?: then)?|then) " + _EN_ACTION + r")*(?: again)?\Z")

# New action families require an explicit follow-up marker. A bare install or
# deployment request starts its own query; continuation only preserves search
# focus and never resumes a task or extends its execution permission.
_ZH_FOLLOWUP_ACTION = (
    r"(?:(?:进一步|深入|深度|全面|详细|仔细|重新|彻底))*"
    r"(?:优化|分析|解析|推送|安装|部署|验证|检查|测试|核对|修复|复查|复验)"
    r"(?:一下|一遍|一轮|下去|好)?")
_ZH_FOLLOWUP = re.compile(
    r"(?:请|麻烦)?(?:继续|接着|接下来|再|确认|按你说的|按此|按上述方案)"
    r"(?:继续)?[，,、]*(?:对此|对其)?(?:进行)?" + _ZH_FOLLOWUP_ACTION
    + r"(?:(?:并且|然后|之后|并|和|及|再|、|，|,|；|;)?" + _ZH_FOLLOWUP_ACTION + r")*(?:吧)?\Z")
_EN_FOLLOWUP_ACTION = (
    r"(?:(?:deeply|carefully|thoroughly|further|fully) )*"
    r"(?:optimi[sz](?:e|ing|ation)|refin(?:e|ing|ement)|improv(?:e|ing|ement)|"
    r"analy[sz](?:e|ing)|analysis|push(?:ing)?|install(?:ing|ation)?|deploy(?:ing|ment)?|"
    + _EN_ACTION + r")(?: (?:deeply|carefully|thoroughly|further|fully|again))*")
_EN_FOLLOWUP = re.compile(
    r"(?:please )?(?:continue|keep|resume|go on|carry on|confirm)(?: with| to)? (?:the )?"
    + _EN_FOLLOWUP_ACTION
    + r"(?:(?: and(?: then)? | then |[,;] ?(?:and(?: then)? |then )?)"
    + _EN_FOLLOWUP_ACTION + r")*\Z")

# Acknowledgements and a completed host reload are common wrappers around a
# follow-up. Remove at most one of each, and still require the entire remainder
# to be action-only. Subjects, versions and negations are never discarded.
_ZH_PROGRESS = re.compile(
    r"(?:请|麻烦)?(?:按你说的|按此|按上述方案|按原计划)?"
    r"(?:继续|接着)(?:推进|进行|执行|完成)?(?:一下|下去)?(?:吧)?\Z")
_ZH_ACK_PREFIX = re.compile(r"^(?:是的|好的|没问题|可以|同意|确认|确定|好)[，,、;；]*")
_ZH_RESUMED_PREFIX = re.compile(
    r"^(?:(?:已经|已)(?:重启|重载|重连|重新连接|刷新)(?:完成|好了|了)?"
    r"|(?:重启|重载|重连|重新连接|刷新)(?:完成|好了|了))[，,、;；]*(?:之后|以后|后)?")
_EN_ACK_PREFIX = re.compile(r"^(?:yes|ok|okay|confirmed)[,;]?\s+")
_EN_RESUMED_PREFIX = re.compile(
    r"^(?:(?:already )?(?:restarted|reloaded|reconnected)"
    r"|(?:restart|reload|reconnect) (?:completed|finished|done))[,;]?\s+")


def is_continuation_query(value):
    """Recognize whole action-only prompts; remaining subject words start a query."""
    value = re.sub(r"\s+", " ", value.lower()).strip("。.!！?？,，;；:： ")
    candidates = [value]
    chinese = value.replace(" ", "")
    acknowledged = _ZH_ACK_PREFIX.sub("", chinese, count=1)
    english = _EN_ACK_PREFIX.sub("", value, count=1)
    for phrase in (chinese, acknowledged):
        candidates.extend((phrase, _ZH_RESUMED_PREFIX.sub("", phrase, count=1)))
    candidates.extend((english, _EN_RESUMED_PREFIX.sub("", english, count=1),
                       _EN_RESUMED_PREFIX.sub("", value, count=1)))
    return not value or any(phrase in _CONTINUATION_ACKS
               or bool(_ZH_CONTINUATION.fullmatch(phrase))
               or bool(_ZH_PROGRESS.fullmatch(phrase))
               or bool(_EN_CONTINUATION.fullmatch(phrase))
               or bool(_ZH_FOLLOWUP.fullmatch(phrase))
               or bool(_EN_FOLLOWUP.fullmatch(phrase)) for phrase in candidates)



def strip_confirmation(value):
    """Remove one leading acknowledgement without removing a subject or status."""
    # Query parsing has a final whole-action check. Material filtering does
    # not: require a delimiter so subjects such as "confirmation of a fix"
    # are not shortened into a generic imperative.
    chinese = re.match(r"^(?:是的|好的|没问题|可以|同意|确认|确定|好)"
                       r"(?=[\s，,、;；:：]|$)[\s，,、;；:：]*", value)
    if chinese:
        return value[chinese.end():]
    english = _EN_ACK_PREFIX.match(value.lower())
    if english:
        return value[english.end():].lstrip(" ，,、;；:：")
    return value

