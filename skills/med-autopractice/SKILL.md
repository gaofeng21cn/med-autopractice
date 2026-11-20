---
name: med-autopractice
description: 使用 Med Auto Practice（MAP）处理医生主导的既往病历、医嘱、门诊和住院病历及出院质控任务；具体部署能力以仓库外配置及逐项资格验证为准。
---

# Med Auto Practice

这是 MAP 的 Codex 主 Skill，也是将来其他 Codex Agent 调用 MAP 的入口。Skill 负责识别医生任务、选择能力和组织复核；任务状态、字段校验、审计和医院系统操作由 MAP 代码及获准适配器承担。MAP 可独立安装和调用，并遵循 OPL Agent Apps 的共用基座原则：通用知识走 Framework 来源接口，设备连接和凭证走原设备 owner，其他外部连接按实际 capability 复用 Connect。实际与待接入边界见 [Agent 共用基座](../../docs/architecture.md#agent-共用基座)。

先定位 MAP 内容根：显式设置 `MAP_SOURCE_ROOT` 时使用该目录；否则检查本 Skill 上两级的插件根，须同时包含 `opl-package.json`（`package_id=med-autopractice`）、`contracts/capabilities.json` 与 `scripts/project_status.py`。原生插件携带这些代码和合同，不依赖开发者机器上的固定源码路径。若宿主只安装了独立 Skill，先定位用户提供的 MAP 源码目录并核对 Git remote 为 `gaofeng21cn/med-autopractice-reference`，再继续。用 `python3 scripts/project_status.py --json` 回读产品 ID、版本、内容完整性及能力资格；安装完成只表示软件可管理，不表示临床执行已开放。源码合同与默认 profile 不包含任何医院特定配置；所有临床能力在公开目录中默认 `planned`、`callable=false`。本地存在部署 profile、连通 KVM 或看见业务页面都不等于通过系统验收；开发验证以合成数据为主。

接入后的任务顺序为：锁定患者和本次就诊，按业务工作流要求读取带来源与时间的事实，调用独立医学知识组件形成带生成者版本的提案，执行获准的系统操作，回读院内对象及一手截图，交医生接受或提出修订。医学模型可按能力分别配置，但不接收医院界面写入权限；工作流不得擅自改写医学提案。医嘱、组套发布等临床关键变更在提交前核对医生对确切提案的批准；提交后仍需医生最终验收。不得编造临床事件或把点击成功当作系统结果。

执行通道由部署配置选择的设备后端和医院系统适配器组成；不假设某医院开放 API/CLI，也不在目标 Windows 安装辅助程序，除非某部署经授权并单独完成资格验证。MAP 客户端、MCP 与设备控制应用须遵守后端的单一控制者约束。部署产品、版本、UI 路径和资格证据存放于仓库外，通过 `MAP_SITE_PROFILE` 加载。`python3 scripts/site_plan.py` 默认检查通用未配置模板，也可用 `--profile` 指向外部部署配置；它不是临床执行入口。Skill 不管理设备凭据、患者数据存储或正式签署。任务步骤和底层操作证据要能相互关联；不得把真实患者资料、截图、秘密或私有医院资料写入仓库。

首批院内贯通当前住院患者的病程记录和根据结果、护理记录调整医嘱。医嘱批次在录入前需医生确认确切提案，录入后仍需系统回读与医生验收。纯 KVM 中文长文尚未通过资格测试，病历可生成带来源草稿，但不得声称已自动回填。`python3 scripts/kvm_status.py` 只读检查本机共享服务，不连接设备或医院系统。

规划或开发时，从已核对的源码仓库按需读取 `docs/architecture.md`、`docs/workflows.md`、`docs/agent-contract.md` 和 `docs/roadmap.md`，不要一次性加载全部资料。

任务状态和推进由 `src/med_autopractice/runtime.py` 与 `scripts/map_runtime.py` 承担，返回 `status`、`next_action`、`missing_information`、`proposal`、`snapshot` 与 `execution.readback`。该入口当前只组合合成系统与合成存储，只接受 `demo-`／`synthetic-` 身份；不得改造成默认指向院内 profile 的通道。真实院内执行仍须先完成对应系统的界面适配与逐项资格。

可复用 Markdown 知识由 OPL Framework 统一管理。需要列出来源、检索或读取获授权正文时，使用 `scripts/map_knowledge.py --workspace <workspace> [--project <project>] sources|search|read-source`；需要维护已有正文时使用同一入口的 `register`、`propose`、`review`、`apply` 或 `review-source`，把精确的 reviewer、review ref、时间和摘要交给 Framework。MAP 不读取或复制患者资料、院内 profile/playbook 到个人知识库；这些仍由医院系统和部署 owner 管理。知识引用须在启动或修订提案时显式放入 `Proposal.knowledge_refs`，旧引用在未换版本前会保留。重新使用依据前，以 `verify-dependencies --dependencies <JSON 文件>` 核对知识 ID 与 SHA-256；响应的 `knowledge_dependencies.valid` 必须为 `true`，进程退出码为 0 本身不表示依赖有效。

需要盘点实际医院系统或建立页面操作资格时，按 [院内系统调研与操作档案](../../docs/site-survey.md) 执行。调研只使用 `glkvm-native` 的观察、按文字定位、等待和人工接管能力，默认只取 OCR 文字与包围盒，需要证据画面时用 `kvm_shot` 存私有文件取路径；通过 `scripts/site_survey.py` 读取仓库外 `map-site-survey.v1` 档案，不能把厂家、版本、页面路径、截图或患者内容写回仓库。先完成只读探查，再分别验证草稿写入、医嘱写入和回读；调研状态 `qualified` 不等于能力目录 `callable=true`。

高度特定的现场操作步骤（例如门户登录、角色与科室选择）存放在仓库外的私有 playbook 中，用 `scripts/site_playbook.py` 读取与展开；格式见 `contracts/site-playbook.schema.json`，说明见 [院内系统调研与操作档案](../../docs/site-survey.md#私有-playbook把高度特定的操作固化下来)。playbook 只描述“在这套界面里怎么走”，不承载医学判断，也不得写入密码、令牌或患者内容；含 `reviewed_write` 步骤的 playbook 必须先取得对应用户授权再执行。

整理既往病历并需要跨病例统一管理时，按 [病历收集与结构化](../../docs/clinical-record.md) 与 [研究数据集](../../docs/research-dataset.md) 执行：`scripts/structure_record.py` 先把采集包清洗成结构化病历（不补造、不覆盖、逐节可回溯），再抽成固定列的研究长表并补上与原文并列的归一化编码；缺失一律留空，诊断只按文字归并，需要人工确认的条目写入仓库外的私有注记，由 `--review-notes` 就地标成 `※需核实 N`。阅读版顺序固定为结构化病历、问题清单、病历原文。归一化结果不是诊断结论，正式出具或发表前仍须按院方政策完成核对与审批。
