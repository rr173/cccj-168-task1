"""规则引擎:安全事件按严重度 + 可评估状态进入,输出允许的决策动作。

规则集带版本(rulesets 表),修订产生新版本;决策会话钉住开会时的
rule_version,修订后旧会话立即过期,防止跨规则版本拼接结论。

默认设计为经典 3+3:
  0/3 DLT          -> 允许递增 ESCALATE
  1/3 DLT          -> 允许扩展 EXPAND(扩至 6 例)
  >=2 DLT          -> 必须停止 STOP
  扩展后 <=1/6 DLT -> 允许递增
STOP / HOLD 始终允许(保守方向永远可行)。
"""
from __future__ import annotations

DEFAULT_RULESET_V1 = {
    "design": "3+3",
    "dlt_min_severity": 3,      # CTCAE >= 3 级且可评估且相关 -> DLT
    "require_related": True,
    "initial_size": 3,
    "expanded_size": 6,
}


def is_dlt(ruleset: dict, event: dict) -> bool:
    """单条事件是否构成 DLT:严重度达标 + 可评估 + (按需)相关。"""
    return (
        event["evaluability"] == "EVALUABLE"
        and int(event["severity"]) >= int(ruleset["dlt_min_severity"])
        and (bool(event["related"]) or not ruleset.get("require_related", True))
    )


def evaluate(ruleset: dict, subject_views: list[dict], expanded: bool) -> dict:
    """对受试者视图求值,返回允许动作与依据。

    subject_views 元素:
      counts:          是否计入可评估集合(被替补且无 DLT、未完成窗者不计)
      window_complete: 观察窗是否完整
      data_status:     PENDING / COMPLETE
      dlt:             是否发生 DLT
    """
    initial = int(ruleset["initial_size"])
    full = int(ruleset["expanded_size"])

    counted = [v for v in subject_views if v["counts"]]
    evaluable = [
        v for v in counted
        if v["window_complete"] and v["data_status"] == "COMPLETE"
    ]
    dlt_n = sum(1 for v in counted if v["dlt"])
    ev_n = len(evaluable)

    permitted = {"HOLD", "STOP"}
    rationale: list[str] = []

    if dlt_n >= 2:
        rationale.append(f"{dlt_n} 例 DLT(>=2):必须停止递增")
    elif not expanded:
        if dlt_n == 0 and ev_n >= initial:
            permitted.add("ESCALATE")
            rationale.append(f"0/{ev_n} DLT:允许递增")
        elif dlt_n == 1:
            permitted.add("EXPAND")
            rationale.append(f"1/{max(ev_n, 1)} DLT:允许扩展至 {full} 例后再评估")
        else:
            rationale.append(f"可评估 {ev_n}/{initial} 例:数据不足,暂不可递增")
    else:
        if dlt_n <= 1 and ev_n >= full:
            permitted.add("ESCALATE")
            rationale.append(f"扩展队列 {dlt_n}/{ev_n} DLT(<=1/{full}):允许递增")
        else:
            rationale.append(
                f"扩展队列 {dlt_n} 例 DLT / 可评估 {ev_n}/{full}:暂不可递增"
            )

    return {
        "dlt_count": dlt_n,
        "evaluable_count": ev_n,
        "counted_count": len(counted),
        "permitted": sorted(permitted),
        "rationale": rationale,
    }
