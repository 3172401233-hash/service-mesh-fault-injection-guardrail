# -*- coding: utf-8 -*-
"""故障注入护栏决策逻辑的单元测试。"""

import unittest

from guardrail import (
    Action,
    Experiment,
    FaultSpec,
    Guardrail,
    GuardrailPolicy,
    RollbackCapability,
    Selector,
    SloStatus,
)


def make_experiment(exp_id, service, ratio, deps=(), error_rate=0.5,
                    critical=False):
    return Experiment(
        experiment_id=exp_id,
        selector=Selector(service=service, critical_entry=critical),
        ratio=ratio,
        fault=FaultSpec(error_type="http_500", error_rate=error_rate),
        dependencies=tuple(deps),
    )


HEALTHY_SLO = SloStatus(
    target=0.99, current=0.999, burn_rates={"1h": 0.5, "6h": 0.8}
)


class BudgetExhaustedTest(unittest.TestCase):
    """预算耗尽:终止新实验并按依赖顺序撤销在运行实验。"""

    def test_slo_below_target_terminates_and_plans_rollback(self):
        guard = Guardrail()
        exhausted = SloStatus(target=0.999, current=0.995,
                              burn_rates={"1h": 2.0, "6h": 3.0})
        db_exp = make_experiment("exp-db", "db", 0.4)
        api_exp = make_experiment("exp-api", "api", 0.2, deps=("db",))
        requested = make_experiment("exp-new", "api", 0.01, deps=("db",))

        decision = guard.evaluate(requested, exhausted, [db_exp, api_exp])

        self.assertEqual(Action.TERMINATE, decision.action)
        self.assertEqual(0.0, decision.approved_ratio)
        self.assertTrue(any("耗尽" in r for r in decision.reasons))

        plan = decision.rollback_plan
        self.assertTrue(plan, "预算耗尽时必须给出撤销计划")
        # 依赖(下游 db)先回滚,上游 api 后回滚。
        db_steps = [s for s in plan if s.experiment_id == "exp-db"]
        api_steps = [s for s in plan if s.experiment_id == "exp-api"]
        self.assertTrue(db_steps and api_steps)
        self.assertLess(plan.index(db_steps[-1]), plan.index(api_steps[0]))
        # 部分回滚:比例分步下降直至 0,避免流量骤变。
        self.assertGreater(len(db_steps), 1)
        self.assertEqual(0.4, db_steps[0].from_ratio)
        self.assertEqual(0.0, db_steps[-1].to_ratio)
        for prev, nxt in zip(db_steps, db_steps[1:]):
            self.assertEqual(prev.to_ratio, nxt.from_ratio)
            self.assertLess(nxt.to_ratio, nxt.from_ratio)

    def test_fast_burn_rate_terminates_even_when_slo_met(self):
        guard = Guardrail()
        burning = SloStatus(target=0.99, current=0.9995,
                            burn_rates={"1h": 20.0, "6h": 3.0})
        requested = make_experiment("exp-new", "api", 0.01)

        decision = guard.evaluate(requested, burning, [])

        self.assertEqual(Action.TERMINATE, decision.action)
        self.assertTrue(any("burn rate" in r for r in decision.reasons))

    def test_rollback_not_supported_reports_gap(self):
        guard = Guardrail()
        exhausted = SloStatus(target=0.999, current=0.995, burn_rates={})
        active = [make_experiment("exp-db", "db", 0.4)]
        requested = make_experiment("exp-new", "db", 0.01)
        no_rollback = RollbackCapability(supported=False, partial=False)

        decision = guard.evaluate(requested, exhausted, active, no_rollback)

        self.assertEqual(Action.TERMINATE, decision.action)
        self.assertEqual((), decision.rollback_plan)
        self.assertTrue(any("回滚能力缺失" in r for r in decision.reasons))


class OverlappingExperimentsTest(unittest.TestCase):
    """重叠实验按合并影响统一评估,不能分别按预算放行。"""

    def test_combined_impact_shrinks_request(self):
        guard = Guardrail()
        # 剩余预算 0.009,实验影响上限 0.0009。
        # 在运行实验影响 0.01 * 0.05 = 0.0005,单独看请求也能放下,
        # 但合并后 1-(1-0.0005)^2 约 0.00099 > 0.0009,必须缩小。
        existing = make_experiment("exp-a", "svc-a", 0.01, error_rate=0.05)
        requested = make_experiment(
            "exp-b", "svc-b", 0.01, deps=("svc-a",), error_rate=0.05
        )

        alone = guard.evaluate(requested, HEALTHY_SLO, [])
        self.assertEqual(Action.ALLOW, alone.action)

        decision = guard.evaluate(requested, HEALTHY_SLO, [existing])
        self.assertEqual(Action.SHRINK, decision.action)
        self.assertLess(decision.approved_ratio, requested.ratio)
        self.assertTrue(any("合并" in r for r in decision.reasons))
        # 批准比例应使合并影响恰好不超上限。
        weight = requested.fault.impact_weight()
        base = existing.impact()
        cap = (HEALTHY_SLO.current - HEALTHY_SLO.target) * 0.1
        combined = 1 - (1 - base) * (1 - decision.approved_ratio * weight)
        self.assertLessEqual(combined, cap + 1e-9)

    def test_transitive_overlap_blocks_when_budget_full(self):
        guard = Guardrail()
        # 两个在运行实验通过依赖传递重叠,合并影响已占满上限。
        e1 = make_experiment("exp-1", "svc-a", 0.01, error_rate=0.05)
        e2 = make_experiment("exp-2", "svc-b", 0.01, deps=("svc-a",),
                         error_rate=0.05)
        requested = make_experiment("exp-3", "svc-c", 0.01, deps=("svc-b",),
                                    error_rate=0.05)

        decision = guard.evaluate(requested, HEALTHY_SLO, [e1, e2])

        self.assertEqual(Action.BLOCK, decision.action)
        self.assertEqual(0.0, decision.approved_ratio)

    def test_non_overlapping_experiment_not_merged(self):
        guard = Guardrail()
        unrelated = make_experiment("exp-z", "svc-z", 0.01, error_rate=0.05)
        requested = make_experiment("exp-b", "svc-b", 0.01, error_rate=0.05)

        decision = guard.evaluate(requested, HEALTHY_SLO, [unrelated])

        self.assertEqual(Action.ALLOW, decision.action)
        self.assertEqual(requested.ratio, decision.approved_ratio)


class CriticalEntryTest(unittest.TestCase):
    """关键入口保护:预算不健康时禁止,健康时限制比例上限。"""

    def test_strained_budget_blocks_critical_entry(self):
        guard = Guardrail()
        strained = SloStatus(target=0.99, current=0.999,
                             burn_rates={"1h": 2.0, "6h": 8.0})
        requested = make_experiment("exp-gw", "gateway", 0.01, critical=True)

        decision = guard.evaluate(requested, strained, [])

        self.assertEqual(Action.BLOCK, decision.action)
        self.assertTrue(any("关键入口" in r for r in decision.reasons))

    def test_healthy_budget_caps_critical_entry_ratio(self):
        guard = Guardrail()
        requested = make_experiment("exp-gw", "gateway", 0.5, critical=True)

        decision = guard.evaluate(requested, HEALTHY_SLO, [])

        self.assertEqual(Action.SHRINK, decision.action)
        self.assertEqual(GuardrailPolicy().critical_entry_max_ratio,
                         decision.approved_ratio)

    def test_small_ratio_on_critical_entry_allowed(self):
        guard = Guardrail()
        requested = make_experiment("exp-gw", "gateway", 0.01, critical=True)

        decision = guard.evaluate(requested, HEALTHY_SLO, [])

        self.assertEqual(Action.ALLOW, decision.action)
        self.assertEqual(0.01, decision.approved_ratio)


class PartialRollbackTest(unittest.TestCase):
    """部分回滚:分步收缩;不支持部分回滚时一步归零。"""

    def test_partial_rollback_steps_down_gradually(self):
        guard = Guardrail()
        exhausted = SloStatus(target=0.999, current=0.99, burn_rates={})
        active = [make_experiment("exp-a", "svc-a", 0.6)]
        requested = make_experiment("exp-new", "svc-a", 0.01)
        capability = RollbackCapability(supported=True, partial=True,
                                        max_step_ratio=0.25)

        decision = guard.evaluate(requested, exhausted, active, capability)

        plan = decision.rollback_plan
        self.assertEqual(3, len(plan))  # 0.6 -> 0.35 -> 0.1 -> 0
        self.assertEqual(0.6, plan[0].from_ratio)
        self.assertEqual(0.0, plan[-1].to_ratio)
        for step in plan:
            self.assertLessEqual(step.from_ratio - step.to_ratio, 0.25 + 1e-9)

    def test_non_partial_rollback_single_step(self):
        guard = Guardrail()
        exhausted = SloStatus(target=0.999, current=0.99, burn_rates={})
        active = [make_experiment("exp-a", "svc-a", 0.6)]
        requested = make_experiment("exp-new", "svc-a", 0.01)
        capability = RollbackCapability(supported=True, partial=False)

        decision = guard.evaluate(requested, exhausted, active, capability)

        self.assertEqual(1, len(decision.rollback_plan))
        step = decision.rollback_plan[0]
        self.assertEqual(0.6, step.from_ratio)
        self.assertEqual(0.0, step.to_ratio)


if __name__ == "__main__":
    unittest.main()
