---
name: med-autopractice
description: 使用 Med Auto Practice（MAP）处理医生主导的既往病历、医嘱、门诊和住院病历及出院质控任务；具体部署能力以仓库外配置及逐项资格验证为准。
---

# Med Auto Practice

这是 MAP 的 Codex 主 Skill，也是将来其他 Codex Agent 调用 MAP 的入口。Skill 负责识别医生任务、选择能力和组织复核；任务状态、字段校验、审计和医院系统操作由 MAP 代码及获准适配器承担。MAP 可独立安装和调用，并遵循 OPL Agent Apps 的共用基座原则：通用知识走 Framework 来源接口，设备连接和凭证走原设备 owner，其他外部连接按实际 capability 复用 Connect。实际与待接入边界见 [Agent 共用基座](../../docs/architecture.md#agent-共用基座)。

先定位 MAP 内容根。用户已显式指定 `MAP_SOURCE_ROOT` 时使用该目录，否则使用本 Skill 上两级的插件根。独立 Skill 可使用用户提供并确认的源码 checkout，包括自有 fork。核对根目录的 `opl-package.json`（`package_id=med-autopractice`）、`contracts/capabilities.json` 与 `scripts/project_status.py`，用 `python3 scripts/project_status.py --json` 回读身份、版本、内容完整性和能力状态。公开能力目录中的真实临床能力为 `planned`、`callable=false`。

用户要求入门、试跑或验证安装时，从内容根执行 `python3 scripts/map_demo.py`。该命令使用固定合成请求，自动走完模拟审批、回读和验收。向用户报告 `mode`、状态序列及回读对象。需要分步体验、部署演练或自定义提案时，参阅[快速上手](../../docs/getting-started.md)。

接入后的任务顺序为：锁定患者和本次就诊，按业务工作流要求读取带来源与时间的事实，调用独立医学知识组件形成带生成者版本的提案，执行获准的系统操作，独立回读院内对象，取得版本化资源证据或一手截图，交医生接受或提出修订。医学模型可按能力分别配置，但不接收医院界面写入权限；工作流不得擅自改写医学提案。医嘱、组套发布等临床关键变更在提交前核对医生对确切提案的批准；提交后仍需医生最终验收。不得编造临床事件或把点击成功当作系统结果。

用户询问如何适配本院时，先选一项业务操作和目标系统，优先核对原生接口或院内中间件；需要界面时选择直接 Computer Use，隔离终端可选 IP KVM。运行 `examples/synthetic/configure_site.py` 生成外部合成 profile，再用 `scripts/site_plan.py` 查看每个依赖的通道和资格状态。检验原生取数可运行 `python3 examples/synthetic/fhir_read.py`。站点连接与凭据由部署提供者持有。[医院接入与验证](../../docs/hospital-adapters.md)

首批院内贯通当前住院患者的病程记录和根据结果、护理记录调整医嘱。医嘱批次在录入前需医生确认确切提案，录入后仍需系统回读与医生验收。纯 KVM 中文长文尚未通过资格测试，病历可生成带来源草稿，但不得声称已自动回填。`python3 scripts/kvm_status.py` 只读检查本机共享服务，不连接设备或医院系统。

规划或开发时，从已核对的源码仓库按需读取 `docs/architecture.md`、`docs/workflows.md`、`docs/agent-contract.md` 和 `docs/roadmap.md`，不要一次性加载全部资料。

任务状态和推进由 `src/med_autopractice/runtime.py` 与 `scripts/map_runtime.py` 承担，返回 `status`、`next_action`、`missing_information`、`proposal`、`snapshot` 与 `execution.readback`。该入口当前只组合合成系统与合成存储，只接受 `demo-`／`synthetic-` 身份；不得改造成默认指向院内 profile 的通道。真实院内执行仍须先完成对应系统的适配与逐项资格。

可复用 Markdown 知识由 OPL Framework 统一管理。需要列出来源、检索或读取获授权正文时，使用 `scripts/map_knowledge.py --workspace <workspace> [--project <project>] sources|search|read-source`；需要维护已有正文时使用同一入口的 `register`、`propose`、`review`、`apply` 或 `review-source`，把精确的 reviewer、review ref、时间和摘要交给 Framework。MAP 不读取或复制患者资料、院内 profile/playbook 到个人知识库；这些仍由医院系统和部署 owner 管理。知识引用须在启动或修订提案时显式放入 `Proposal.knowledge_refs`，旧引用在未换版本前会保留。重新使用依据前，以 `verify-dependencies --dependencies <JSON 文件>` 核对知识 ID 与 SHA-256；响应的 `knowledge_dependencies.valid` 必须为 `true`，进程退出码为 0 本身不表示依赖有效。

需要盘点业务界面时，先确认站点获准的浏览器、桌面或设备会话，按[院内系统调研与操作档案](../../docs/site-survey.md)记录对象定位、观察和恢复步骤。Computer Use 优先使用可访问性或局部观察，选择 IP KVM 时复用 `glkvm-native` 的 OCR、等待和人工接管。一手画面保存在获准的证据目录。分别验证只读、草稿保存、医嘱写入和独立回读。

高度特定的现场操作步骤（例如门户登录、角色与科室选择）存放在仓库外的私有 playbook 中，用 `scripts/site_playbook.py` 读取与展开；格式见 `contracts/site-playbook.schema.json`，说明见 [院内系统调研与操作档案](../../docs/site-survey.md#私有-playbook把高度特定的操作固化下来)。playbook 只描述“在这套界面里怎么走”，不承载医学判断，也不得写入密码、令牌或患者内容；含 `reviewed_write` 步骤的 playbook 必须先取得对应用户授权再执行。

整理既往病历并需要跨病例统一管理时，按 [病历收集与结构化](../../docs/clinical-record.md) 与 [研究数据集](../../docs/research-dataset.md) 执行：`scripts/structure_record.py` 先把采集包清洗成结构化病历（不补造、不覆盖、逐节可回溯），再抽成固定列的研究长表并补上与原文并列的归一化编码；缺失一律留空，诊断只按文字归并，需要人工确认的条目写入仓库外的私有注记，由 `--review-notes` 就地标成 `※需核实 N`。阅读版顺序固定为结构化病历、问题清单、病历原文。归一化结果不是诊断结论，正式出具或发表前仍须按院方政策完成核对与审批。
