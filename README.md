<p align="center">
  <img src="assets/branding/medautopractice-logo.png" alt="Med Auto Practice 标志" width="132" />
</p>

<p align="center">
  <a href="./README.md"><strong>中文</strong></a> | <a href="./README.en.md">English</a>
</p>

<h1 align="center">Med Auto Practice 临床工作智能体</h1>

<p align="center"><strong>将医生的工作意图，转化为可核验的临床工作流程。</strong></p>
<p align="center">患者资料 · 医嘱协作 · 病历记录 · 出院质控</p>
<p align="center"><strong>MAP · 开发中</strong></p>

> 当前已有项目骨架、独立医学提案路由、一期九项任务的通用依赖计划和使用合成数据的任务内核。产品通过可替换的 IP KVM 与医院系统适配工作；实际医院、厂家、版本和 UI 资格资料由部署方在仓库外配置，不属于开源源码。当前没有可调用的临床能力。

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>面向谁</strong><br/>
      在门诊和病房中，需要反复整理患者资料、准备医嘱、书写与审核病历的医生。
    </td>
    <td width="33%" valign="top">
      <strong>解决什么</strong><br/>
      资料散落在不同系统，日常操作重复，临床事实、草稿、待办与完成状态难以连贯维护。
    </td>
    <td width="33%" valign="top">
      <strong>怎样协作</strong><br/>
      医生说明目标，Agent 归集事实、组织草稿和拟变更；关键临床决定由医生复核，再推进获准操作。
    </td>
  </tr>
</table>

<p align="center">
  <img src="assets/branding/medautopractice-overview.png" alt="MAP 一期规划：临床任务、资料归集、病历与医嘱草拟、医生审阅、系统录入、结果核验；全程关联患者与就诊、来源与时间、权限与复核" width="100%" />
</p>

## 为什么是 Med Auto Practice

临床工作需要连续判断，也包含大量重复操作：从既往记录找到关键信息，核对新结果和护理记录，把诊疗计划整理成医嘱，完成门诊与住院文书，并在出院后核查缺项和时限。

**MAP 围绕这些日常工作组织能力。** 它的目标是把患者资料、医生意图、草稿、复核意见和系统结果联系起来，让一次任务能够继续推进、核对和恢复。

MAP 通过 Codex 主 Skill 接收医生或其他 Codex Agent 的任务，不要求医生再使用一套独立界面。医生可以在 Codex 中审阅草稿和差异；首版仅通过 IP KVM 操作院内 Windows，不假设医院开放 API/CLI。MAP 可独立安装和调用，并遵循 OPL Agent Apps 的共用基座原则：可复用知识使用 Framework 公共来源入口，设备连接与凭证由原设备服务管理。具体复用范围与临床数据边界见[架构文档](docs/architecture.md#agent-共用基座)。

## 希望怎样使用

完成接入后，可以通过宿主这样发起任务：

- “整理这位患者既往门诊与住院资料，生成带来源的时间线，列出缺失和矛盾信息。”
- “根据今天实际完成的查房和已有记录，准备病程记录草稿，把需要我补充的内容列出来。”
- “按照我确认的诊疗计划准备新收医嘱，列出需要核对的药品规格、用法和检查项目。”
- “核对这位出院患者的病历，整理待补记录、审核问题和即将到期的事项。”

任务不必从零开始。规划中的 MAP 会保留已经核对的事实和任务状态，只获取必要的新资料；临床情况变化时重新核对，而不是机械沿用上次结论。

## 核心能力规划

<table width="100%">
<tr>
<td width="50%" valign="top">

<strong>让患者资料形成连续脉络</strong>

归集既往门诊与住院病历，整理诊断、手术、病理、用药和检查结果。保留时间与来源，区分缺失、冲突和已经核实的信息。

</td>
<td width="50%" valign="top">

<strong>让医嘱准备更有条理</strong>

维护门诊与住院组套，准备新收医嘱、调整提案和出院带药草稿。将拟变更与依据交给医生审阅，再执行明确获准的录入。

</td>
</tr>
<tr>
<td width="50%" valign="top">

<strong>让病历记录贴合实际工作</strong>

围绕真实临床事件生成住院记录和门诊结构化病历草稿。按照院内现行规范提示关键信息，不把未知内容补写成已发生的事实。

</td>
<td width="50%" valign="top">

<strong>让出院整理与时效可追踪</strong>

整理出院后病历，核查缺项、冲突、签署和归档状态，形成修订提案及到期待办。将“准备完成”和“系统中已完成”分开核验。

</td>
</tr>
</table>

## 一期要完成什么

一期以当下临床工作为主，同时完成既往病历整理；以下范围都属于一期，不以单个演示替代完整交付。

| 工作 | 一期范围 |
|---|---|
| 既往资料 | 门诊与住院病历收集、结构化、来源和完整性核对 |
| 医嘱组套 | 门诊与住院组套建立、动态更新、版本审阅 |
| 患者医嘱 | 新收患者医嘱；结合检验检查结果和护理记录的调整；出院带药 |
| 日常病历 | 与十八项核心制度及院内规范对应的住院记录；门诊结构化记录 |
| 出院后工作 | 病历整理、审核、修订提案和时效提醒 |

专病队列建立、数据整理与动态随访放在后续阶段，复用一期的患者标识、事实来源和结构化数据。

首批真实贯通目标是当前住院患者的病程记录和结合检验检查结果、护理记录的医嘱调整。医嘱录入前医生按批确认具体变更，录入后仍查看系统回读与一手截图；中文长文输入未验收前不开放自动病历回填。

## AI 与医生怎样分工

MAP 负责组织资料、草稿、待办和操作结果。医生负责临床判断、方案确认及正式签署；医院系统保留正式病历与医嘱状态。

普通查询、导航和草拟可在明确任务范围内连续执行，关键复核集中在具体草稿和拟变更上。患者、就诊或事实版本变化后需要重新核对；医嘱已开立不等于治疗已执行，保存按钮被点击也不等于文档已保存。

真实使用前须落实院方允许的远程接入、患者资料处理与存储范围。现阶段没有医院临床验收；PACS 操作规划也不等于具备基于远控画面的自动影像诊断能力。

## 在 Med Auto 系列中的位置

| Agent | 关注的工作 |
|---|---|
| **Med Auto Science · MAS** | 医学研究、数据分析、证据与论文交付 |
| **Med Auto Grant · MAG** | 医学基金规划、撰写、评审与申请材料 |
| **Med Auto Cast · MAC** | 医学科普视频的规划、制作、复核与交付 |
| **Med Auto Practice · MAP** | 日常临床资料、医嘱协作、病历与出院质控 |

MAP 与 `glkvm-native` 分仓维护：MAP 负责医院系统语义和临床流程，GLKVM 后端负责设备连接、观察、键鼠、等待和人工接管。MAP 不复制设备控制代码，也不把底层远控可用视为临床任务已验收。

## 当前进度

| 已有 | 尚待实现 |
|---|---|
| Agent 项目目录、品牌与双语项目说明；开发态主 Skill 与插件清单源码 | 真实临床执行入口与医院资格验收 |
| 一期能力目录、领域与调用契约；独立医学提案路由、九项业务流程依赖计划、合成数据任务内核、审计及断点恢复测试 | 临床医学提案实现、各部署的系统语义适配及目标对象回读 |
| 带来源与覆盖范围的合成患者事实、KVM 界面原语、住院病历和医嘱调整合成验证 | 院方认可的患者资料存储、有效规则包和全部一期工作流 |
| 只读项目检查、共享 KVM 服务状态入口、通用站点依赖模板及本地 profile 加载入口 | 具体部署的系统 profile、中文长文可靠回填、真实系统回读、获准的数据存储及临床验收 |

当前版本为 **`0.0.2`**。能力目录中的临床能力均标记为 `planned`、`callable=false`；主 Skill 仅用于规划与合成开发，软件发行与临床能力资格分别管理。已接入 OPL Framework 公共知识来源的 direct-consumer 薄入口，提案修订保留 `knowledge_refs`。本仓库按公开源码和参考实现维护，不包含任何医院部署档案或真实患者资料。设备后端的实测范围与未解问题记录在[调研与证据](docs/research.md)。

## 软件身份与公开源码

软件目录中的正式名称为 **Med Auto Practice**，ID 为 `med-autopractice`，版本为 **0.0.2**。本仓库提供公开源码、通用合同、合成数据和只读检查入口。`opl-package.json` 保留软件身份和内容锁，当前使用 `development_only` 源码模式，不依赖私有 Package、私有 registry 或仓库外凭据。部署方可以在自己的受控载体中复用这些源码，但部署 profile、患者资料和证据仍须放在仓库外。详情见[公开范围与部署边界](docs/public-scope.md)和[软件管理与临床资格](docs/architecture.md#软件管理与临床资格)。

`python3 scripts/project_status.py --json` 在源码或物化后的插件根中均可只读检查本包身份、版本、内容完整性及能力资格。软件可检查，与临床能力是否开放分别判断；当前临床能力仍为 `planned`、`callable=false`。卸载插件不触碰仓库外的部署 profile、患者资料及任务库。

## 本地开发起点

使用 Python 3.10+，当前状态检查不需要第三方依赖，也不会连接设备或医院系统。

```sh
git clone https://github.com/gaofeng21cn/med-autopractice-reference.git
cd med-autopractice-reference
python3 scripts/project_status.py --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`python3 scripts/site_plan.py revise_orders` 可只读查看通用依赖模板的阻断项，不连接设备。部署方可用 `MAP_SITE_PROFILE=/absolute/path/to/private-profile.json` 指定仓库外的本地配置；测试仅用合成数据。Codex 主 Skill 的临床执行须在界面适配与验收后逐项开放。

统一知识来源入口示例（不复制正文，不连接医院系统）：

```sh
python3 scripts/map_knowledge.py --workspace /path/to/workspace sources
python3 scripts/map_knowledge.py --workspace /path/to/workspace --project /path/to/workspace/projects/example search --query "method" --limit 10
python3 scripts/map_knowledge.py --workspace /path/to/workspace read-source --id owner.example
```

`scripts/map_runtime.py` 是主 Skill 调用的本地任务入口，当前只支持合成系统与 `demo-`／`synthetic-` 身份，没有连接院内配置的开关：

```sh
python3 scripts/map_runtime.py start --store /tmp/map-tasks.sqlite --request request.json
python3 scripts/map_runtime.py show  --store /tmp/map-tasks.sqlite --task-id <task_id>
```

`scripts/structure_record.py` 把仓库外的采集包清洗成结构化病历，并进一步抽成可跨病例合并的研究数据集（固定列长表 + 归一化编码 + 研究阅读版）；规则、边界与命令见[病历收集与结构化](docs/clinical-record.md)和[研究数据集](docs/research-dataset.md)：

```sh
PYTHONPATH=src python3 scripts/structure_record.py \
  --collection <私有采集包.json> --out-dir <私有目录> --record-id <id> \
  --review-notes <私有核实注记.json>
```

```text
agent/                         Agent 标识与当前建设状态
.codex-plugin/                 Codex 插件清单源码
contracts/                     能力目录与调用合同
src/med_autopractice/
  agent/                       任务入口、编排与恢复
  domain/                      患者、就诊、事实与提案
  medicine/                    医学提案接口与按能力路由
  adapters/                    医院系统和 GLKVM 适配
  workflows/                   业务工作流与首批任务要求
  records/                     采集包清洗与结构化病历
rules/                         制度、模板与组套资产
skills/med-autopractice/       唯一 Codex 主 Skill 源码
assets/branding/               系列风格的品牌图与生成记录
tests/fixtures/                合成或获准脱敏样例
```

## 文档与开发路线

| 文档 | 继续了解 |
|---|---|
| [产品范围](docs/product.md) | 已确认的一期目标与后置能力 |
| [架构与领域模型](docs/architecture.md) | 模块与状态归属的 SSOT；医学判断和业务工作流的边界 |
| [工作流与规则](docs/workflows.md) | 各项任务的输入、产物与复核 |
| [Agent 调用契约草案](docs/agent-contract.md) | 请求、结果、授权与完成语义 |
| [实施顺序与验收](docs/roadmap.md) | 从系统调查到一期完整验收 |
| [KVM 界面资格规范](docs/kvm-ui-qualification.md) | 按部署进行界面适配、回读与开放验收 |
| [部署档案配置](docs/site-integration.md) | 通用依赖合同与本地私有 profile 的分界和加载方式 |
| [病历收集与结构化](docs/clinical-record.md) | 采集包怎样清洗成结构化病历，以及不补造、不覆盖、可回溯三条约束 |
| [研究数据集](docs/research-dataset.md) | 字段抽取与归一化、九张研究长表的列合同、去标识与跨病例合并约定 |
| [调研与证据](docs/research.md) | 现有 GLKVM 能力、实操经验与成本基线 |
| [能力目录](contracts/capabilities.json) | 各能力当前的机器可读状态 |
| [公开范围与部署边界](docs/public-scope.md) | 可公开的规范与私有部署材料 |
| [开发约定](AGENTS.md) | 代码、数据、验证及授权边界 |

患者资料、截图、凭据和运行日志不进入版本库。品牌图采用系列统一的手绘风格，示意图使用英文；中文项目说明保持自然中文。图中展示的是产品目标与一期规划，不能代替已实现能力或临床验证记录。

## 许可证

源码和文档采用 Apache License 2.0。项目标识与素材来源见 [NOTICE](NOTICE)，贡献和私密问题报告见 [贡献指南](CONTRIBUTING.md)及[安全政策](SECURITY.md)。
