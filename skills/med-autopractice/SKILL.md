---
name: med-autopractice
description: 使用 Med Auto Practice（MAP）开始或恢复医生主导的临床工作，包括整理已有病历、准备病程、审阅医嘱、出院质控，以及按当前任务接入本院系统。用户只说“启动 MAP”时也从这里开始。
---

# Med Auto Practice

围绕医生此刻要完成的工作组织资料、提案、审阅和系统回读。以用户显式指定的 `MAP_SOURCE_ROOT` 或本 Skill 目录上两级的插件根为内容根。单独安装 Skill 的宿主使用用户选定的 MAP checkout。切换内容根时用 `python3 scripts/project_status.py --json` 核对包 ID、版本和内容完整性。以下命令从该根执行，读取用户文件时使用其原始路径。

## 开始或继续任务

按用户已经给出的目标选择入口，能做的步骤由你执行。

- **已有任务、草稿或待审医嘱。** 先从原任务存储读回任务和当前版本，呈现待审差异、依据与下一步。公开合成任务用原 `--store`、`--task-id` 调用 `scripts/map_runtime.py show`。真实任务使用站点登记的任务入口。结果不确定时先查找原提交并独立回读。仅在缺失原任务位置或对象有歧义时请医生定位。
- **已有采集包或病历资料。** 直接在获准工作区整理，按下文“整理已有资料”交付。只有医生要求补取院内来源时才接入对应系统。
- **需要本院系统，或医生只说“启动 MAP”。** 立即使用随包的[医院适配 Skill](../map-site-adaptation/SKILL.md)，把本轮目标、已知部署位置、可用连接和已有成果带过去。即使用户只说“我想用检验结果写今天的病程”，也主动检查这个任务需要的接入。未给目标时先读已选配置，再用一个具体问题确定第一项工作。
- **明确要求演示或检查安装。** 检查安装用 `python3 scripts/project_status.py --json` 回读身份、版本、内容完整性和能力。明确要看合成医嘱时运行 `python3 scripts/map_demo.py`，说明模拟审批与回读结果。分步体验见[快速上手](../../docs/getting-started.md)。

## 整理已有资料

对采集包使用 `scripts/structure_record.py`，保留每项事实的时间、来源和原文。阅读版依次给出结构化病历、问题清单、病历原文。需要跨病例汇总时再导出固定列研究长表及归一化编码。缺项留空，冲突并列，诊断仅按原文归并。复核注记保存在获准的工作目录，`--review-notes` 可将对应条目标为“※需核实”。[病历结构化](../../docs/clinical-record.md) · [研究数据集](../../docs/research-dataset.md)

## 在同一任务中复核

按工作流绑定患者与就诊，记录事实来源、时间和覆盖范围。医学组件基于这份快照产生带版本的候选提案。医生对确切版本批准医嘱或组套变更。通过获准的部署入口提交后，重新读取目标系统的对象、状态与内容，附版本化资源证据或一手截图，交医生接受或修订。临床判断和签署由有资质的医生完成。[任务与恢复契约](../../docs/agent-contract.md)

公开的 `scripts/map_runtime.py` 配合合成系统与合成存储，只接受 `demo-` 或 `synthetic-` 身份。站点工作流的开放状态见[能力目录](../../contracts/capabilities.json)，部署配置和操作资格见[医院适配 Skill](../map-site-adaptation/SKILL.md)。

## 按需使用知识和开发资料

需要通用 Markdown 来源时使用 `scripts/map_knowledge.py --workspace <workspace> [--project <project>] sources|search|read-source`。维护知识走同一入口的 `register`、`propose`、`review`、`apply` 或 `review-source`。在启动或修订提案时显式记录 `Proposal.knowledge_refs`，复用前以 `verify-dependencies --dependencies <JSON 文件>` 核对 `knowledge_dependencies.valid`。患者记录和站点资料由其原有受控存储保管。[知识来源与部署](../../docs/site-integration.md)

开发工作流、中间件或宿主适配时按需读取[架构](../../docs/architecture.md)、[业务工作流](../../docs/workflows.md)与[适配器开发指南](../../docs/adapter-development.md)。
