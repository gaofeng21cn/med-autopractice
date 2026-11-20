# 二次开发

先运行既有合成任务，确认预期状态、证据和医生复核点。然后替换与你的需求直接相关的模块，沿用同一个任务内核。

| 想改的内容 | 实现入口 | 验证结果 |
| --- | --- | --- |
| 草稿或候选提案 | 实现 [`ClinicalKnowledge.propose`](../src/med_autopractice/medicine/ports.py)，由 [`CapabilityKnowledgeRouter`](../src/med_autopractice/medicine/router.py) 按能力选择 | 提案保留事实引用、生成器版本和适用的知识版本 |
| 工作流所需资料与审阅点 | 修改 [工作流定义](../src/med_autopractice/workflows/catalog.py) 及对应的 [工作流说明](workflows.md) | 缺失资料、事实变化和医生核对分别进入约定状态 |
| 医院系统连接与回读 | 实现 [`ClinicalSystem`](../src/med_autopractice/adapters/ports.py)。使用 KVM 时复用 [`HospitalUI` 与 `KVMClinicalSystem`](../src/med_autopractice/adapters/kvm.py) | 快照绑定对象与来源范围，提交后从目标系统重新读取对象和内容 |

## 改造一份合成病程草稿

```sh
python3 examples/synthetic/custom_proposer.py
```

示例通过 `build_synthetic_runtime(..., knowledge=router)` 注入自定义提案生成器，沿用相同的合成适配器、状态判断与审计。输出包含 `generator_ref`、`cited_fact_ids` 和按来源排列的草稿。修改示例中的 `SourceIndexProposer.propose`，再查看提案正文和摘要如何变化。完整源码见 [自定义提案示例](../examples/synthetic/custom_proposer.py)。

## 对接自己的测试系统

1. 通过部署档案演练确定依赖 ID 和证据字段，再在受控目录记录自己的系统角色及读写资格。[部署档案配置](site-integration.md)
2. 从模拟器或已获准的测试环境验证 `read_context`。让 `Snapshot` 保留患者与就诊绑定、采集范围和来源证据。先跑资料缺失、对象不符及版本变化场景。[适配器接口](../src/med_autopractice/adapters/ports.py)
3. 在自有宿主通过 `build_runtime(knowledge, system, audit, store, expected_states)` 组装。该工厂为工作流目录中所有具有写入依赖的能力建立策略，因此 `expected_states` 要覆盖这些能力。站点通过适配器的 `can_apply` 逐项检查写入资格。[运行时组装](../src/med_autopractice/runtime.py)
4. 对每个候选写入场景验证提交前审批、提交后从目标系统回读、超时后的唯一对象回查，以及医生最终验收。任务库、审计和回读证据采用部署方获准的存储与访问控制。[界面资格规范](kvm-ui-qualification.md)

开发时用 `build_synthetic_runtime` 和随仓库提供的合成存储运行回归。公开 CLI 只加载合成系统。站点适配、配置和运行证据由部署方在仓库外管理。

修改纳入插件的文件后，维护者用 `python3 scripts/package_metadata.py --write-lock` 更新内容锁，再运行项目检查和相关测试。[贡献指南](../CONTRIBUTING.md)
