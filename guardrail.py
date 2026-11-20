"""服务网格故障注入实验护栏(纯决策逻辑,不执行任何真实注入)。

根据目标选择器、依赖拓扑、实验比例、故障类型、当前 SLO 与跨窗口
burn rate,判断实验允许开始(ALLOW)、缩小(SHRINK)、阻止(BLOCK)
还是终止并回滚(TERMINATE)。重叠实验按合并后的影响范围统一评估,
撤销计划按依赖顺序生成,支持部分回滚以避免流量骤变。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


class Action(Enum):
    ALLOW = "allow"          # 按请求比例允许开始
    SHRINK = "shrink"        # 允许,但需缩小到批准比例
    BLOCK = "block"          # 不允许开始(无在运行实验需要撤销)
    TERMINATE = "terminate"  # 预算耗尽,终止并撤销相关在运行实验


@dataclass(frozen=True)
class FaultSpec:
    """故障类型:延迟注入或错误注入。"""

    latency_ms: int = 0
    error_type: Optional[str] = None  # 如 "http_500"、"grpc_unavailable"
    error_rate: float = 0.0           # 命中流量中注入错误的比例

    def impact_weight(self) -> float:
        """故障严重度权重:错误注入按错误率计,纯延迟按延迟量级折算。"""
        if self.error_type is not None:
            return min(1.0, max(0.0, self.error_rate))
        if self.latency_ms > 0:
            # 延迟故障折算:1s 及以上视为完全影响。
            return min(1.0, self.latency_ms / 1000.0)
        return 0.0


@dataclass(frozen=True)
class Selector:
    """目标选择器。critical_entry 标记关键入口服务。"""

    service: str
    namespace: str = "default"
    critical_entry: bool = False

    @property
    def key(self) -> str:
        return "%s/%s" % (self.namespace, self.service)


@dataclass(frozen=True)
class Experiment:
    """一次故障注入实验(请求的或已在运行的)。"""

    experiment_id: str
    selector: Selector
    ratio: float                              # 作用流量比例 [0, 1]
    fault: FaultSpec
    dependencies: Tuple[str, ...] = ()        # 依赖服务名(同命名空间)
    active: bool = True

    def impact(self) -> float:
        """实验影响 = 流量比例 × 故障严重度。"""
        return min(1.0, max(0.0, self.ratio) * self.fault.impact_weight())


@dataclass(frozen=True)
class SloStatus:
    """当前 SLO 状态与跨窗口 burn rate(相对月预算的消耗速率)。"""

    target: float                       # 如 0.999
    current: float                      # 当前实测可用性
    burn_rates: Mapping[str, float]     # 如 {"1h": 14.5, "6h": 3.0}


@dataclass(frozen=True)
class GuardrailPolicy:
    """护栏阈值策略。"""

    fast_window: str = "1h"
    fast_burn_threshold: float = 14.4   # 快速窗口:约 2 天烧完月预算
    slow_window: str = "6h"
    slow_burn_threshold: float = 6.0    # 慢速窗口:约 5 天烧完月预算
    budget_consumption_cap: float = 0.1     # 实验合计影响最多占剩余预算的比例
    critical_entry_max_ratio: float = 0.05  # 关键入口实验比例上限


@dataclass(frozen=True)
class RollbackCapability:
    """回滚能力:是否支持回滚、是否支持按比例部分回滚。"""

    supported: bool = True
    partial: bool = True
    max_step_ratio: float = 0.25        # 部分回滚每步最大比例变化


@dataclass(frozen=True)
class RollbackStep:
    """撤销计划中的一步:把某实验比例从 from_ratio 调整到 to_ratio。"""

    experiment_id: str
    service: str
    from_ratio: float
    to_ratio: float
    reason: str


@dataclass(frozen=True)
class Decision:
    """护栏判定结果。"""

    action: Action
    approved_ratio: float
    reasons: Tuple[str, ...] = ()
    rollback_plan: Tuple[RollbackStep, ...] = ()


def _combined_impact(impacts: Sequence[float]) -> float:
    """合并同一路径上多个实验的影响:1 - Π(1 - impact_i)。"""
    miss = 1.0
    for impact in impacts:
        miss *= 1.0 - min(1.0, max(0.0, impact))
    return 1.0 - miss


class Guardrail:
    """故障注入实验护栏决策器。"""

    def __init__(self, policy: Optional[GuardrailPolicy] = None):
        self.policy = policy or GuardrailPolicy()

    # ------------------------------------------------------------------
    # 预算评估
    # ------------------------------------------------------------------
    def _budget_state(self, slo: SloStatus) -> Tuple[str, List[str]]:
        """返回 (exhausted|strained|healthy, 原因列表)。"""
        reasons: List[str] = []
        if slo.current < slo.target:
            reasons.append(
                "错误预算已耗尽:当前 SLO %.4f 低于目标 %.4f"
                % (slo.current, slo.target)
            )
            return "exhausted", reasons
        fast = slo.burn_rates.get(self.policy.fast_window, 0.0)
        if fast >= self.policy.fast_burn_threshold:
            reasons.append(
                "错误预算即将耗尽:%s 窗口 burn rate %.2f ≥ 阈值 %.2f"
                % (self.policy.fast_window, fast, self.policy.fast_burn_threshold)
            )
            return "exhausted", reasons
        slow = slo.burn_rates.get(self.policy.slow_window, 0.0)
        if slow >= self.policy.slow_burn_threshold:
            reasons.append(
                "预算消耗偏快:%s 窗口 burn rate %.2f ≥ 阈值 %.2f"
                % (self.policy.slow_window, slow, self.policy.slow_burn_threshold)
            )
            return "strained", reasons
        return "healthy", reasons

    # ------------------------------------------------------------------
    # 重叠实验合并
    # ------------------------------------------------------------------
    @staticmethod
    def _overlap_group(
        requested: Experiment, active: Sequence[Experiment]
    ) -> List[Experiment]:
        """找出与请求实验处于同一路径上的所有在运行实验(含请求本身)。

        两个实验共享服务、或一方的依赖与另一方的服务/依赖相交时视为
        重叠;通过传递闭包合并成组,整组按合并影响统一评估。
        """
        group: List[Experiment] = [requested]
        touched: Set[str] = {requested.selector.service} | set(requested.dependencies)
        remaining = list(active)
        changed = True
        while changed:
            changed = False
            for exp in list(remaining):
                services = {exp.selector.service} | set(exp.dependencies)
                if services & touched:
                    group.append(exp)
                    touched |= services
                    remaining.remove(exp)
                    changed = True
        return group

    # ------------------------------------------------------------------
    # 撤销计划(按依赖顺序,支持部分回滚)
    # ------------------------------------------------------------------
    @staticmethod
    def _dependency_depths(experiments: Sequence[Experiment]) -> Dict[str, int]:
        """计算每个实验的依赖深度:依赖链越靠上游深度越大。"""
        by_service: Dict[str, List[Experiment]] = {}
        for exp in experiments:
            by_service.setdefault(exp.selector.service, []).append(exp)

        depths: Dict[str, int] = {}

        def depth(exp: Experiment, visiting: Set[str]) -> int:
            if exp.experiment_id in depths:
                return depths[exp.experiment_id]
            if exp.experiment_id in visiting:
                return 0  # 循环依赖时按叶子处理,保证可终止
            visiting = visiting | {exp.experiment_id}
            best = 0
            for dep in exp.dependencies:
                for dep_exp in by_service.get(dep, []):
                    if dep_exp.experiment_id != exp.experiment_id:
                        best = max(best, 1 + depth(dep_exp, visiting))
            depths[exp.experiment_id] = best
            return best

        for exp in experiments:
            depth(exp, set())
        return depths

    def _rollback_plan(
        self,
        experiments: Sequence[Experiment],
        capability: RollbackCapability,
        reason: str,
    ) -> Tuple[RollbackStep, ...]:
        """生成撤销计划:依赖(下游)先回滚,入口(上游)最后回滚,
        避免上游流量突然打到仍在故障的下游;支持部分回滚时按比例
        分步收缩,避免流量骤变。"""
        if not capability.supported:
            return ()
        depths = self._dependency_depths(experiments)
        ordered = sorted(
            experiments, key=lambda e: (depths[e.experiment_id], e.experiment_id)
        )
        steps: List[RollbackStep] = []
        for exp in ordered:
            if not exp.active or exp.ratio <= 0:
                continue
            if capability.partial and capability.max_step_ratio > 0:
                current = exp.ratio
                while current > 0:
                    nxt = max(0.0, current - capability.max_step_ratio)
                    steps.append(
                        RollbackStep(
                            experiment_id=exp.experiment_id,
                            service=exp.selector.service,
                            from_ratio=round(current, 6),
                            to_ratio=round(nxt, 6),
                            reason=reason,
                        )
                    )
                    current = nxt
            else:
                steps.append(
                    RollbackStep(
                        experiment_id=exp.experiment_id,
                        service=exp.selector.service,
                        from_ratio=round(exp.ratio, 6),
                        to_ratio=0.0,
                        reason=reason,
                    )
                )
        return tuple(steps)

    # ------------------------------------------------------------------
    # 主判定
    # ------------------------------------------------------------------
    def evaluate(
        self,
        requested: Experiment,
        slo: SloStatus,
        active_experiments: Sequence[Experiment] = (),
        rollback: RollbackCapability = RollbackCapability(),
    ) -> Decision:
        reasons: List[str] = []
        state, state_reasons = self._budget_state(slo)
        reasons.extend(state_reasons)

        group = self._overlap_group(requested, active_experiments)
        running = [
            e for e in group
            if e.active and e.experiment_id != requested.experiment_id
        ]

        # 1. 预算耗尽:终止,撤销同路径上的在运行实验。
        if state == "exhausted":
            plan = self._rollback_plan(running, rollback, reasons[0])
            if running and not rollback.supported:
                reasons.append(
                    "回滚能力缺失:无法自动撤销 %d 个在运行实验" % len(running)
                )
            reasons.append("终止:错误预算耗尽,不允许新的注入并撤销相关实验")
            return Decision(Action.TERMINATE, 0.0, tuple(reasons), plan)

        # 2. 关键入口保护。
        if requested.selector.critical_entry:
            if state != "healthy":
                reasons.append("关键入口保护:预算不健康,禁止在入口服务注入")
                return Decision(Action.BLOCK, 0.0, tuple(reasons))
            if requested.ratio > self.policy.critical_entry_max_ratio:
                reasons.append(
                    "关键入口保护:比例 %.3f 超过上限 %.3f,缩小到上限"
                    % (requested.ratio, self.policy.critical_entry_max_ratio)
                )
                return Decision(
                    Action.SHRINK,
                    self.policy.critical_entry_max_ratio,
                    tuple(reasons),
                )
            reasons.append("关键入口:比例在上限内,允许")
            return Decision(Action.ALLOW, requested.ratio, tuple(reasons))

        # 3. 重叠实验合并影响 vs 剩余预算。
        headroom = max(0.0, slo.current - slo.target)
        cap = headroom * self.policy.budget_consumption_cap
        if state == "strained":
            cap /= 2.0
            reasons.append("预算紧张:可用实验影响上限减半至 %.6f" % cap)

        existing_impacts = [e.impact() for e in running]
        base = _combined_impact(existing_impacts)
        weight = requested.fault.impact_weight()

        if running:
            reasons.append(
                "合并 %d 个重叠实验的影响范围统一评估(基础影响 %.6f)"
                % (len(running), base)
            )

        if base >= cap:
            reasons.append(
                "重叠实验合并影响 %.6f 已达上限 %.6f,不允许新增注入"
                % (base, cap)
            )
            return Decision(Action.BLOCK, 0.0, tuple(reasons))

        if weight <= 0:
            reasons.append("故障严重度为 0,按请求比例允许")
            return Decision(Action.ALLOW, requested.ratio, tuple(reasons))

        # 求解最大批准比例 r:1-(1-base)*(1-r*w) <= cap
        max_ratio = (1.0 - (1.0 - cap) / (1.0 - base)) / weight
        max_ratio = max(0.0, min(1.0, max_ratio))

        if requested.ratio <= max_ratio:
            reasons.append(
                "合并影响在预算上限 %.6f 内,按请求比例 %.3f 允许"
                % (cap, requested.ratio)
            )
            return Decision(Action.ALLOW, requested.ratio, tuple(reasons))
        if max_ratio <= 0:
            reasons.append("预算余量不足,无法批准任何注入比例")
            return Decision(Action.BLOCK, 0.0, tuple(reasons))
        reasons.append(
            "请求比例 %.3f 超出预算余量,缩小到 %.3f" % (requested.ratio, max_ratio)
        )
        return Decision(Action.SHRINK, round(max_ratio, 6), tuple(reasons))
