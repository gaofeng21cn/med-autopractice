# 架构与领域模型

本文定义模块边界与数据归属。MAP 公开发行任务内核、医学提案路由、部署计划和系统适配组件。各项医院工作流的开放状态见[能力目录](../contracts/capabilities.json)。

## 一个临床工作智能体，多种系统接入方式

Med Auto Practice 在同一个项目内维护医学提案、业务工作流、临床任务和系统适配。宿主通过 Codex 主 Skill 调用。主 Skill 直接处理已有资料或恢复原任务。首次启动和需要院内数据时，它调用随包的医院适配 Skill，读取本次部署，按当前工作验证缺少的操作，再带着结果继续原任务。部署按单项操作选择原生协议、院内中间件、Computer Use 或 IP KVM。[主 Skill](../skills/med-autopractice/SKILL.md) · [医院适配 Skill](../skills/map-site-adaptation/SKILL.md) · [医院接入示例](hospital-adapters.md)

不同通道共用患者、就诊、文档、医嘱和任务状态。正式记录归医院系统。连接授权、会话与控制权归相应的 API 提供者、桌面宿主或设备服务。

| 层 | 所属项目 | 负责什么 | 不负责什么 |
|---|---|---|---|
| 接入通道 | 医院原生接口、集成平台、Computer Use 宿主、可选 glkvm-native | 提供获准的查询、操作、输入与一手结果 | 诊疗判断和医生审批 |
| 系统适配 | med-autopractice / adapters | 把“打开这次就诊的检验结果”变成可验证的 HIS/EMR/LIS/PACS 操作 | 决定患者应该接受什么诊疗 |
| 医学判断 | med-autopractice / medicine | 使用有来源的患者事实和版本化知识产生候选内容与依据；可按能力选用不同模型 | 操作医院系统、批准提案、决定任务完成 |
| 业务工作流 | med-autopractice / workflows、agent、domain | 定义资料范围和复核门槛，按任务收集事实、提交获准操作、回读、留痕和组织验收 | 自行决定药物、剂量或虚构临床事实 |

患者事实、任务状态、规则版本和审阅记录属于 Med Auto Practice 的共享底座，不再拆成第四个独立项目。医院系统仍是正式病历、正式医嘱及执行状态的权威来源；MAP 存放必要证据引用、工作副本和任务进度。

### Agent 共用基座

MAP 可独立安装和由 Codex 调用，同时遵循 [OPL Agent Apps 标准](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/policies/agent-native-app-policy.md)。共用基座适用于所有自建 Agent，不以 OPL 托管、标准智能体封装或独立 UI 为前提。MAP 只实现临床领域职责，需要通用能力时复用现有公开入口。

| 能力 | 当前入口与 owner | MAP 的职责与后续接缝 |
| --- | --- | --- |
| 安装与调用 | `opl-package.json`、Codex 原生插件/Skill；`scripts/project_status.py`、`scripts/map_runtime.py` | 以稳定 ID `med-autopractice` 管理软件；Framework 投影原生安装与更新状态，不另建安装 registry。临床能力资格继续由 MAP 能力目录决定 |
| 可复用知识与通用上下文 | Framework `opl knowledge`；`knowledge.py` / `scripts/map_knowledge.py` 薄客户端 | 显式绑定工作区、项目、域和来源版本；医学提案通过现有 `ClinicalKnowledge` 接口消费获准内容与引用，详见下一节 |
| 医院连接与凭证 | 部署获准的原生接口、中间件、Computer Use 宿主或设备后端 | 复用对应提供者的授权与会话，适配器绑定站点、患者、就诊和具体操作 |
| 后续外部服务连接 | [Framework Connect 公开合同](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/specs/connection-authorization.md)；实际 provider 保管和续期凭证 | 当前没有外部服务 provider 的生产实现。出现实际需求时绑定已有 capability、连接引用及 grant；缺少入口时与 owner 补齐，不解析宿主凭据文件或新建秘密库 |
| 任务与人机复核 | MAP `TaskRuntime`、提案摘要、系统回读与医生审阅；Codex 展示 | 保留同一临床任务 owner；当前无独立业务 UI。需要 UI 时先复用宿主/共享视图，携带同一 `task_id` 与提案摘要，回读同一任务结果 |

医院正式病历、MAP 临床任务与通用个人知识各有作用范围。共用基座不改变患者存储、临床提案、医院系统写入与医生签署的 owner，也不将临床状态复制到通用工作账本。

### 软件管理与临床资格

`opl-package.json` 持有软件包身份、版本、展示文案和内容锁。OPL 软件中心与 Codex 使用相同的 `med-autopractice` 安装 ID。公开 GitHub `main` 是开发主线，带注释的版本标签锁定发布源码。

OPL Framework 从标签对应的提交生成安装包清单，将公开 OCI 制品发布到 `ghcr.io/gaofeng21cn/one-person-lab-packages/med-autopractice`。软件中心按版本及摘要获取，Codex 也可以直接从仓库安装。用 `python3 scripts/project_status.py --json` 查看当前安装的身份和内容完整性。

`visibility=public` 表示软件公开分发。各院保管自己的连接和凭证，并按具体工作流记录验收结果。

插件包含 MAP 主 Skill、医院适配 Skill、Python 代码、脚本、通用合同与相关文档。两个 Skill 都从自身插件根定位资源。显式 `MAP_SOURCE_ROOT` 可用于开发源码。院内 profile、患者资料和任务库位于包外，卸载插件只移除软件载体，原生管理器不负责删除这些业务资料。管理界面分别显示软件状态与临床能力状态。

代码或合同发生变化后，维护者审阅 `content_lock.paths` 并执行 `python3 scripts/package_metadata.py --write-lock`（内部复用 Framework `opl_framework.package_content_lock.content_digest`，独立运行回退标准 SHA-256）更新内容摘要，再运行项目检查及相关测试。消费方以 Package ID、版本、精确源内容和 native readback 判断同一安装对象。

### 与 OPL 统一知识来源的边界

MAP 的可复用方法、制度说明和医学知识引用可以通过 `src/med_autopractice/knowledge.py` 的薄客户端读取 OPL Framework 公共来源接口（优先导入 `opl_framework.knowledge_client.KnowledgeClient`，未导入时降级为 `opl` CLI 子进程调用）：`sources`、`search`、`read-source`、`register` 以及 `propose`、`review`、`apply`、`review-source` 写回命令。Framework 持有来源 registry、grant、接受版本、快照和写回回执；MAP 不创建第二套 registry、索引、缓存或正文副本。使用 `scripts/map_knowledge.py` 时，调用方必须明确提供工作区、可选项目和 OPL CLI，域身份固定为 `med-autopractice`。

医院系统的正式患者事实、采集包、院内制度与页面操作 JSON 仍由医院/部署 owner 管理。`Snapshot` 是带患者/就诊版本的本次任务事实，不能当作长期知识；site profile、survey 和 playbook 也继续走 MAP 的外部部署合同，不自动注册为个人知识。需要把可复用正文交给医学提案组件时，调用方先从 Framework 获取获授权来源和精确哈希，再把引用作为 `Proposal.knowledge_refs` 传入；真实临床动作仍须独立完成能力资格、医生复核和系统回读。

`KnowledgeSources` 是通用来源客户端，`ClinicalKnowledge.propose(request, snapshot)` 是医学提案接口，两者职责不同。通过 `build_runtime` 可以组合医学实现和获准来源。本地站点入口分开使用批准的规则、事实与医生交代的输入。独立模型及其来源选择需按站点验收。通用个人上下文仅按任务需要读取；来源缺失、撤权、版本失效或 `knowledge_dependencies.valid=false` 时按公共接口的结果修复，不转为本地正文副本。参数与复核流程见[部署档案](site-integration.md#可复用知识的公共来源入口)。

医生在 Codex 中发起任务，MAP 内核调用医学知识组件产生候选提案，再由站点适配器执行获准操作。医学组件持有建议及依据，内核负责身份、版本、权限、审阅和恢复，适配器负责读取来源、提交和独立回读。

### 模块与合同归属

以下列出当前源码主线已实现的模块 owner（后续 Phase 4 离线合规存储与运维增强见[路线图](roadmap.md)）：

| 唯一 owner | 当前源码或合同 | 稳定边界 |
|---|---|---|
| 患者事实、提案、任务和审阅对象 | `domain/task.py` | `Snapshot` 绑定患者、就诊、来源与采集范围；`Proposal` 绑定能力、事实引用、生成者、知识引用和 `map-proposal.v1` 版本。领域对象不决定诊疗内容或 UI 坐标。 |
| 跨平台锁与安全边界 | `platform/locking.py`、`platform/security.py` | 统一 POSIX 与 Windows 独占任务锁及私有目录路径安全校验；保持核心包零外部依赖。 |
| 医学提案端口与双轨起草 | `medicine/ports.py`、`medicine/router.py`、`medicine/proposals.py`、`medicine/llm_proposer.py`、`medicine/phi_guard.py` | `ClinicalKnowledge.propose(request, snapshot)` 返回候选 `Proposal`，支持 `local_trusted` 与 `pseudonymize_outbound` 双轨路由及 `PHIGuard` 脱敏，经 `medicine/proposals.py` 确定性校验。 |
| 业务工作流与宿主入口 | `workflows/catalog.py`、`workflows/results.py`、`workflows/site_*.py`、`agent/task_engine.py`、`agent/task_store.py`、`runtime.py`、`deployment.py`、`mcp_server.py`、`review_ui/` | 一期及扩展工作流共用任务状态、站点录制与压测资格流水线（`site_recorder.py`、`site_qualify.py`）、SQLite 跨进程恢复、标准 MCP Server 与 `review_ui/` 临床复核视图。 |
| 系统语义与多通道驱动 | `adapters/ports.py`、`adapters/composite.py`、`adapters/playbook_runner.py`、`adapters/declarative_ui.py`、`adapters/drivers/`、`adapters/fhir.py`、`adapters/middleware_rest.py`、`adapters/hl7v2.py`、`adapters/kvm.py`、`adapters/ui.py`、`adapters/synthetic.py` | `ClinicalSystem` 规定查询、提交、回读及恢复。`CompositeClinicalSystem` 按依赖选定通道混合路由；`PlaybookRunner` 与 `DeclarativeHospitalUI` 驱动本机 `computer_use`（`desktop_win.py` / `browser_cdp.py`）与通用 `ip_kvm`（`kvm_driver.py`、`ocr.py`、`text_entry.py`），原生支持 FHIR R4、REST 中间件与 HL7 v2，并提供 UI 模式模板 `docs/site-patterns/README.md` 与合成靶机 `examples/sandbox_his/`。 |
| 患者事实清洗与结构化病历 | `records/clinical_record.py`、`records/research_dataset.py`、`contracts/structured-clinical-record.schema.json` | 把仓库外采集包清洗为带每节来源、显式缺口和显式冲突的结构化病历，并衍生固定列研究长表。规则见[病历收集与结构化](clinical-record.md)。它只整理已采集内容，不做临床判断，也不写回院内系统。 |

## 目标架构决策（ADR-1 至 ADR-4）

为兼顾医院隔离终端与医生电脑直接部署两类核心现场需求，MAP 的设计遵循以下四项架构决策（各阶段落地状态与院内验收边界见[路线图](roadmap.md)）：

- **ADR-1（双通道并行演进：`computer_use` 理想形态 ∥ `ip_kvm` 物理隔离兜底）**：本机直连（Windows UIA 桌面控件树 + 浏览器 CDP 附着 + 人工即时抢占）作为理想形态，硬件无关 `KVMTransport`（兼容 `glkvm-native`、PiKVM/JetKVM/NanoKVM、VNC/HID 等）作为物理隔离兜底，两者并行建设并共享同一声明式 `PlaybookRunner`、`CompositeClinicalSystem` 混合通道路由与 `TaskEngine` 受控状态机。
- **ADR-2（IP KVM 中文长文输入三层策略）**：优先完善**策略 B（纯 HID 键码/输入法分段注入 + 局部 OCR 逐段校对）**作为第一兜底，确保零院方软件侵入；同时提供**策略 A（KVM 协议剪贴板 / 受控局域网中转页）**的可选插件能力按医院策略开启；未通过中文输入资格校验的站点自动执行**策略 C（安全降级 `draft_only`）**，绝不静默写入错字病历。
- **ADR-3（复核交互对齐 OPL App MiniApp / MCP App 标准形态）**：不建设重型独立 HIS/Web 管理系统，而通过标准 MCP Server（`mcp_server.py`、`scripts/map_mcp.py`）接入宿主，并围绕 **OPL App MiniApp / MCP App** 标准嵌入式形态构建 `review_ui/` 临床复核卡片，在非 OPL 宿主下自动回退至仅绑定 `127.0.0.1` 的一次性 Token 本地视图，严格操作同一 `TaskRuntime` 对象。
- **ADR-4（临床起草模型双轨并存）**：`medicine/` 层通过 `LLMClinicalProposer` 与 `PHIGuard` 同时支持**院内本地模型直连（`local_trusted`，零 PHI 出网）**与**本地可逆假名化脱敏后调用云端强模型（`pseudonymize_outbound`，按医院策略开启）**，所有输出通过 `medicine/proposals.py` 确定性规则与目录校验拦截幻觉。

工作流不调用医学模型内部方法，也不修改医学提案内容；医学组件不接收键鼠客户端或医院写入权限。一个医嘱任务中，医学组件提出候选变更及依据，业务工作流确认必需资料和医生对确切版本的批准，适配器负责院内录入与可见对象回读。`TaskEngine` 是跨能力的状态编排，不等于各项临床业务流程已经完成。

`Proposal.generator_ref` 标识生成实现及版本，可以是模型配置、规则或医生输入的受控引用，不包含密钥或推理原文。提案摘要覆盖该引用、知识来源、事实引用、患者/就诊、能力和正文；更换生成实现会产生新的待审版本。`payload` 当前仍是能力专属字典，具体医嘱字段和文书结构需在获得本院字典与界面后形成版本化 Schema，未完成前不得宣称可录入真实医嘱。

文档职责也各有 owner（完整导航见[文档索引](README.md)）：本页定义模块及状态归属；[调用契约](agent-contract.md)定义任务输入、输出和审批/回读语义；[工作流](workflows.md)定义各临床任务的输入与产物；[能力目录](../contracts/capabilities.json)唯一记录是否可调用；[路线图](roadmap.md)记录实施顺序与验收条件。其他页面只引用，不重新定义这些事实。

[部署档案规范](site-integration.md)说明通用合同与本地私有 profile 的边界；`contracts/site-dependencies.json` 仅含不绑定任何医院的默认依赖，实际部署通过 `MAP_SITE_PROFILE` 从仓库外加载。`unmapped`、`observed`、`qualified` 的状态只描述适配证据，`observed` 不满足任务执行资格；即使某 profile 的依赖变为 `qualified`，仍须单独按能力目录完成权限、存储、医生验收和临床开放。依赖计划只读，不调用设备或医学模型。

### 稳定任务契约与可替换实现

其他 Agent 通过 MAP Codex 主 Skill 发起任务。主 Skill 把医生意图和获准作用范围交给 MAP，返回任务状态、待审提案、来源和系统证据引用；Codex 展示并传递医生的复核决定。`runtime.py` 是主 Skill 调用的本地边界，内部仍是 `agent/task_engine.py` 的开发内核；显式站点运行时装配本院适配、规则和存储，合成运行时用于独立开发验证。主 Skill 另可整理已提供的采集包，并调用医院适配 Skill 核对实际部署需要的操作。

医学知识、制度规则与系统操作分开维护。医学知识组件可以结合专业知识库和患者事实生成提案，但不能自己点击系统或批准诊疗决定；院内制度包决定文书要求、模板和时限；业务工作流执行收集、核对、提交、回读、复核和修订。更换知识源或医学模型不改工作流与系统适配器；更换 HIS 或 IPKVM 不改临床任务的完成定义。

适配接口包括 `read_context`、`can_apply`、`apply`、`read_back` 和 `find_submission`。获准的原生接口或中间件优先使用系统对象与结构化字段。Computer Use 使用宿主的浏览器或桌面会话。IP KVM 由设备服务负责。各通道共用患者绑定、版本核对、提交审批、回读和医生验收。

每步记录动作、结果、时间与 trace。提交后从目标系统独立读取患者、就诊、对象、状态与完整拟变更字段。界面路径提供一手截图，结构化路径提供带观察时间的源资源版本或私有快照引用，两者都随任务交给医生验收。

在界面路径上，原始画面放在院方获准的证据存储。模型按需读取局部观察与关键帧。等待和页面稳定性检查在适配器本地完成。原生协议路径可直接保留结构化来源。

## 两个仓库怎样组织

设备控制继续由独立的 `glkvm-native` 项目负责；`scripts/kvm_status.py` 优先通过 `opl packages locate glkvm-native --json` 定位已安装设备包，再回退 `$CODEX_HOME/glkvm-native/installation.json`。Med Auto Practice（MAP）维护临床任务语义、通用适配合同和合成验证，不把任何部署工作区路径写入源码。

项目入口与运行示例见 [README](../README.md)。Codex 提供对话与复核界面。

不在版本库保存真实患者资料、账号、截图或令牌。医院专属界面配置和内部制度按院方要求私有管理；真实病例存储位置、保留期、备份及访问权限单独配置。开发用合成数据即可，真实病例存储采用院方认可的加密与访问控制方案，不能把个人目录或普通日志作为默认临床资料库。

Codex 主 Skill 调用 Python 任务内核。接口适配器复用授权连接，Computer Use 复用宿主会话，KVM 适配器复用外部设备服务。代码负责状态、字段、校验与恢复，Skill 负责理解工作目标并选择合适的操作。

只有出现独立团队/发布周期、多套临床产品共享同一连接器，或院内部署必须隔离进程和存储时，再评估拆出第三个项目。专病队列目前作为业务模块，不因为未来可能科研而预先分仓。

## 系统适配层要真正理解什么

HIS 常承担挂号、就诊、医嘱、药品与费用等功能；电子病历可能由独立 EMR 承担。PACS/RIS 对应影像与检查流程，LIS 对应检验；护理记录可能位于独立护理系统。最终必须盘点本院实际产品，不能假定“HIS/PACS 两个软件”涵盖全部入口。

为每个实际系统建立可维护的界面知识：

- 产品、版本、用户角色、环境和入口；测试环境与正式环境明确区分。
- 患者及本次就诊身份如何显示、如何切换、如何核对；不能只用姓名或床号。
- 页面结构、稳定文字或图标锚点、输入法、弹窗、等待条件、分页/虚拟列表和时间范围。
- 每个动作的前置条件、输入、预期后置状态、失败恢复；界面改版或身份不符即停止写入。
- “保存草稿”“签名”“提交医嘱”“打印”“撤销”等按钮的真实效果及结果回读位置。

上层调用应接近 `read_encounter_results`、`read_nursing_records`、`save_note_draft` 等临床语义，而不是反复决定屏幕坐标。接口返回身份、时间范围、数据完整性、来源引用、观察时间和错误状态，不仅返回 OCR 文本。

任何写入尝试都绑定患者、就诊、目标对象和当前数据版本。保存超时先查询目标系统是否已产生对象；点击成功不等于保存成功。相同草稿或医嘱不能因重试而重复创建。没有查询依据时标为结果不确定，交还人工核对。

## 所有工作流共用的临床事实底座

先明确以下对象，避免每个功能重新从截图拼患者：

| 对象 | 最低需要的信息 |
|---|---|
| 患者/就诊 | 医院标识、患者唯一标识、门诊/住院就诊标识、跨系统映射依据 |
| 临床事实 | 内容、单位、发生时间、记录时间、来源系统/文档、提取时间、核对状态 |
| 来源文档 | 原系统对象标识、版本、签署/草稿状态、获取范围、引用定位 |
| 资料快照 | 本次任务使用的事实集合、时间截点、缺失项和冲突项 |
| 任务 | 目标、就诊范围、授权动作、状态、负责人、下一步和截止依据 |
| 提案与审阅 | 原内容、拟变更、理由/来源、待补信息、审阅者、批准的确切版本 |
| 执行记录 | 目标系统对象、尝试与回读结果、是否重复、错误和人工接管点 |

保留“未记录”“未查询到”“明确否认”“不适用”“无法识别”的差异。既往病史不自动变成当前诊断，历史用药不自动变成当前医嘱；日期、单位、药品规格、阴阳性和左右侧等关键字段必须核对。

只为当前任务检索必要事实，增量获取新结果；变化较快的资料在正式操作前刷新。生成内容必须能追溯到事实或明确标注的医生判断；不得将模型推测写作已经发生的查体、沟通、诊疗、签署或护理执行。

## 医生怎样做到一句话发起、少量关键复核

例如：“整理患者 X 本次住院以来的资料，生成今天的病程记录草稿，并列出待我确认的事项。”

系统先锁定患者及本次住院，展示简洁确认上下文；自动收集授权范围内的资料，形成带来源的摘要，生成草稿和少量必要问题。医生查看原文/拟稿差异，补充判断并审阅；具备合格写入通道后回填草稿，回读医院系统中的目标文档。正式签署保留在医生工作环节。

常用的读资料、导航、归类、模板填充和草拟可在明确任务范围内连续执行，不要求每次点击确认。医嘱新开/调整、组套发布、正式签署等临床关键步骤提供具体、可审阅的变更清单，并由有权限的医生决定。复核绑定患者、就诊、事实版本和拟变更；版本改变或发生冲突后重新核对，不能用一次概括同意长期覆盖变化中的患者。

建议状态：待获取 → 资料核对 → 草稿 → 医生审阅 → 待录入 → 已录入 → 已回读。正式签署/医嘱生效使用医院系统独立状态；不要将“医嘱已开立”写成“治疗已执行”。中断恢复从持久化任务状态和目标系统实际状态继续，不靠聊天记忆猜测。
