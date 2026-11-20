# service-mesh-fault-injection-guardrail
限制故障注入实验范围并校验回滚与影响预算

## 概述

`guardrail.py` 提供服务网格故障注入实验的护栏决策逻辑(Python 3.8.2+)。
针对"从 1% 流量逐步扩大到全量"的实验场景,在每次扩量前评估:

- **预算耗尽 → 终止(TERMINATE)**:当前 SLO 低于目标,或快速窗口
  burn rate 超过阈值(默认 1h 窗口 ≥ 14.4),判定错误预算耗尽,
  拒绝新实验并对同路径在运行实验生成撤销计划。
- **预算紧张 → 缩小(SHRINK)**:慢速窗口 burn rate 偏高(默认 6h
  窗口 ≥ 6.0)时,可用实验影响上限减半,请求比例超出余量时缩小到
  批准比例。
- **重叠实验合并评估**:共享服务或依赖相交(含传递闭包)的实验视为
  同一路径,合并影响按 `1 - Π(1 - impact)` 计算,整组共用一份预算
  上限(默认剩余预算的 10%),不允许分别按预算放行。
- **关键入口保护**:`critical_entry` 标记的入口服务在预算不健康时
  禁止注入(BLOCK),健康时比例也不得超过上限(默认 5%)。
- **撤销计划按依赖顺序执行**:下游依赖先回滚、上游入口最后回滚;
  支持部分回滚时按 `max_step_ratio` 分步收缩比例直至 0,避免立即
  全量回滚造成流量骤变。

## 输出

`Guardrail.evaluate()` 返回 `Decision`:

- `action`:ALLOW / SHRINK / BLOCK / TERMINATE
- `approved_ratio`:批准比例(阻止或终止时为 0)
- `reasons`:阻止/缩小/终止原因列表
- `rollback_plan`:`RollbackStep` 序列,按依赖顺序排列,每步给出
  `from_ratio → to_ratio`

## 重要说明

**本项目不会实际注入任何故障。** 它只是纯决策引擎:不连接
Kubernetes、Istio/Envoy 或任何服务网格控制面,不修改网络配置,
只根据输入的 SLO/burn rate/实验拓扑计算允许、缩小或终止的判定与
撤销计划,由调用方自行执行。

## 测试

```bash
python -m unittest
```

覆盖预算耗尽(含快速 burn rate)、重叠实验合并、关键入口保护和
部分/非部分回滚等场景。
