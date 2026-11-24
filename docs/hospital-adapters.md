# 从一个医院操作开始接入

先选一个可在测试环境核对的目标，例如「取回这次就诊的一条检验结果」。让医院信息部门确认数据由哪个系统负责、可用接口、授权角色，以及患者和就诊编号如何映射。一个工作流可用多种通道。部署团队为每个操作选择通道，MAP 任务沿用同一患者、就诊、资料版本与审阅记录。

| 部署条件 | profile 中的 `transport` | 实现与首个验证点 |
| --- | --- | --- |
| 系统开放受支持的原生 API 或 SDK | `native_api` | 用正式查询接口取得有来源的对象。下方 FHIR R4 示例可直接运行。 |
| 已有院内集成平台、消息服务或厂商中间件 | `middleware` | 将获准的操作封装在站点的 `ClinicalSystem`。先核对中间件标识到原系统对象的映射、版本与时延。 |
| 医院允许宿主操作浏览器或桌面 | `computer_use` | 由宿主提供已授权会话，用可访问性树、浏览器控件或局部观察实现 `HospitalUI`，交给 `UIClinicalSystem`。先验证选中的患者和窗口，最终回读原对象。 |
| 隔离终端仅开放视频与键鼠 | `ip_kvm` | 选用设备后端和同一套界面检查。验证显示尺寸、输入、中文长文与人工接管。[KVM 界面资格](site-survey.md#kvm-界面资格与失败恢复) |

## 用 FHIR R4 读取一条定量结果

```sh
python3 examples/synthetic/fhir_read.py
```

示例在回环地址启动临时测试服务，实际发出两次 HTTP GET。它先打开 `Encounter` 核对患者，再读取指定 `Observation` 并核对患者和就诊。输出包含结果、单位、发生时间、报告时间和来源引用。示例的 `source_ref` 以 `Observation/synthetic-result/_history/3` 结尾。

在本院适配器内，可用同一个读取组件对接经过授权的 FHIR R4 端点。由部署环境传入地址、就诊映射和可续期的访问令牌回调。这里的 `patient_id`、`encounter_id`、`observation_id` 都是该端点的 FHIR 资源 ID。历史关联号、住院号等业务标识由站点先做映射。

```python
from med_autopractice.adapters.fhir import FHIRObservationSource

source = FHIRObservationSource(fhir_base_url, site_id, access_token=provider.get_access_token)
fact = source.read_quantity(subject, observation_id)
```

读取器处理已知 ID 的单项定量结果，保留比较符、单位及初步结果状态。查询结果清单、组合检验、报告正文、跨系统编号映射与访问授权由站点实现。将结果并入 `Snapshot` 时，按实际取得的资料填写 `coverage` 和缺项。单项结果只证明这一项的取数。[读取组件](../src/med_autopractice/adapters/fhir.py) · [FHIR R4 Observation](https://hl7.org/fhir/R4/observation.html)

## 把中间件和业务工作流连接起来

1. 在外部部署档案中给一项依赖填入 `transport`，再运行 `scripts/site_plan.py` 查看该工作流需要的其他来源。已有 profile 可继续使用，未指定通道的步骤会显示 `null`。[合成部署演练](site-integration.md#运行合成部署示例)
2. 通过 `ClinicalSystem.read_context` 将获准查询映射成 `Snapshot`，保留来源、就诊、版本、覆盖范围和缺项。医学提案组件消费这份快照。使用 `build_runtime` 注入站点适配器、存储、审计与医学组件。
3. 开放写入前，按具体操作实现并验证 `can_apply`、`apply`、`read_back`、`find_submission`。医嘱按患者、就诊、事实版本与提案摘要批准。提交得到对象标识后，用独立查询从正式目标重新读取对象、状态与完整的拟变更字段。
4. 原生或中间件回读将重新取得的具体资源版本或私有证据快照引用放入 `resource_version_ref`，记录观察时间与 trace。界面通道用同一对象的一手截图 `screenshot_ref`。两种方式都要逐项核对内容，并将回读交给医生验收。
5. 在测试系统验证患者不符、资料过期、返回超时和同一提案可能对应多个对象的情况。结果不确定时，根据原任务查找唯一对象再回读。待站点证据、规则、持久存储、必要角色和医生验收都就绪后，再由部署团队对相应能力开放临床使用。

端点、角色、页面、令牌和回读证据由各院保管。上游贡献可提供同结构的合成请求、模拟响应与失败用例，使其他部署团队能复用业务逻辑。[临床系统接口](../src/med_autopractice/adapters/ports.py) · [公开资料范围](public-scope.md)

## 二次开发

先运行既有合成任务，确认预期状态、证据和医生复核点，再替换与需求直接相关的模块，沿用同一个任务内核：

| 想改的内容 | 实现入口 | 验证结果 |
| --- | --- | --- |
| 草稿或候选提案 | 实现 [`ClinicalKnowledge.propose`](../src/med_autopractice/medicine/ports.py)，由 [`CapabilityKnowledgeRouter`](../src/med_autopractice/medicine/router.py) 按能力选择 | 提案保留事实引用、生成器版本和适用的知识版本 |
| 工作流所需资料与审阅点 | 修改[工作流定义](../src/med_autopractice/workflows/catalog.py)及对应的[工作流说明](workflows.md) | 缺失资料、事实变化和医生核对分别进入约定状态 |
| 医院系统连接与回读 | 实现 [`ClinicalSystem`](../src/med_autopractice/adapters/ports.py)；使用 Computer Use 或 KVM 时复用 [`HospitalUI` 与 `UIClinicalSystem`](../src/med_autopractice/adapters/ui.py) | 快照绑定对象与来源范围，提交后从目标系统重新读取对象和内容 |

通过 `python3 examples/synthetic/custom_proposer.py` 可演示 `build_synthetic_runtime(..., knowledge=router)` 如何注入自定义提案生成器并输出 `generator_ref`、`cited_fact_ids` 与按来源排列的草稿。

对接自己的测试系统时：
1. 通过[部署档案配置](site-integration.md#接入本院环境)确定依赖 ID 和证据字段，在受控目录记录系统角色与读写资格；
2. 从模拟器或获准测试环境验证 `read_context`，使 `Snapshot` 保留患者/就诊绑定、采集范围和来源证据；
3. 在自有宿主通过 `build_runtime(knowledge, system, audit, store, expected_states)` 组装（`expected_states` 需覆盖工作流中具有写入依赖的能力，适配器通过 `can_apply` 逐项检查写入资格，见[`runtime.py`](../src/med_autopractice/runtime.py)）；
4. 验证提交前审批、提交后独立回读、超时唯一对象回查以及医生验收。

修改纳入插件的文件后，维护者须更新 `opl-package.json` 中的 `content_lock` 并运行 `python3 scripts/package_metadata.py --write-lock` 与全套测试，见[贡献指南](../CONTRIBUTING.md)。
