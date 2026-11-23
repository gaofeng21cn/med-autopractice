# 本地临床工作流部署

MAP 的本地入口可装配院方的授权、事实读取、条件写入与回查。医院保管正式病历、医嘱、接口凭据及合规证据。公开包提供通用工作流和合成站点演练，实际临床开放以[现场验收计划](roadmap.md)为准。

## 用合成站点演练

在 MAP 内容根运行。演练在你指定的新目录建立站点、请求、任务库和一个独立的模拟目标系统。

```sh
map_demo_root=$(mktemp -d)
python3 examples/synthetic/local_deployment.py "$map_demo_root/site"
python3 scripts/map_runtime.py start --site "$map_demo_root/site" \
  --request "$map_demo_root/site/write_inpatient_notes.json"
python3 scripts/map_runtime.py batch --site "$map_demo_root/site" \
  --requests "$map_demo_root/site/batch.json"
```

单项请求返回任务 ID、资料版本、提案摘要与下一动作。只读结果进入 `review_result`。拟写入的内容先由医生按返回的摘要和资料版本批准，再从独立目标对象回读。用输出替换下面三个变量后，可在合成站点演练批准和验收。

```sh
python3 scripts/map_runtime.py submit --site "$map_demo_root/site" \
  --task-id "$map_task_id" --clinician demo-clinician \
  --digest "$map_proposal_digest" --revision "$map_facts_revision"
python3 scripts/map_runtime.py review --site "$map_demo_root/site" \
  --task-id "$map_task_id" --clinician demo-clinician \
  --digest "$map_proposal_digest" --accept
```

再次使用相同 `request_id` 会打开原任务。用 `refresh` 更新未提交任务的原始资料，再请医生核对新版本。返回 `needs_input` 时，先展示已有事实和 `required_input`，医生补充后用 `refresh --inputs <JSON 文件>` 在原任务提交完整的新输入。`reconcile` 用于回查结果不确定的写入。批量输入是院方给定的只读请求清单，增量保存每个任务。重复同一清单会返回已建立的任务。新一轮随访可使用新的截止日期和请求 ID。

## 接入本院环境

1. 在仓库外选择一个仅授权账号可访问、置于院方认可的加密卷上的目录。文件使用院方的留存、备份和销毁策略。`tasks.sqlite` 在该目录保存任务、草稿、授权引用和审计事件。本地部署使用 macOS 或 Linux 上支持进程锁的文件系统，每个任务在写入及回查期间由一个进程推进。操作输出进入获准工作区或宿主。
2. 用站点依赖模板创建 `dependencies.json`。为准备开放的能力记录适用角色、通道、证据及资格引用。用 `site_plan.py --profile <档案> <能力 ID>` 检查缺口。撤销某条依赖后会停止相关操作。部署档案的 `profile_id` 与部署的 `site_id` 一致。
3. 由部署方审阅并安装私有 `provider.py`。它在获准的本地账号下运行，实现 `build_provider(directory)` 并返回下表中的接口。配置由可信维护者管理，连接凭据沿用医院或设备服务的授权入口。
4. 建立 `deployment.json`，写入 `schema_version: map-local-deployment.v1`、`site_id`、`capabilities` 与 `rules`。能力映射的值为目标读回状态。只读工作流使用 JSON `null`，病历使用 `saved_draft`。依赖规则的能力需要含 `approved_ref` 的对应规则对象。示例生成器给出每项结构。站点必须按实际字段和用途逐项验收。
5. 在受控宿主登记部署目录和本院的授权方式。先验证一项只读操作，再验证病程草稿保存与回读，最后才开放确切医嘱批准和写入。输入和系统返回可能含患者数据，部署方负责控制宿主的数据流向及可见范围。

| 适配接口 | 部署方需要实现的行为 |
| --- | --- |
| `authorize(action, request, decision)` | 核对会话及任务用途、患者、就诊和授权。读取使用受控授权引用。提交和验收还要解析医生的批准或验收记录，并与任务 ID、内容摘要和资料版本相符。CLI 中的人名或引用只是待核对输入。 |
| `read_context(subject, capability_id)` | 返回与传入对象严格绑定的 `Snapshot`，包含操作 trace、完整度、来源和稳定资料版本。仅将本工作确实读全的范围标记为完整。历史资料可用多页 collection facts，保留原就诊 ID 与分页／覆盖证据。 |
| `can_apply(proposal)` | 核对目标、实际字段能力及当前角色是否可以提交。写入和回读两项资格都齐全时，MAP 才呈现可提交状态。 |
| `apply_bound(request, proposal, revision, idempotency_key, existing_object_ref, approval)` | 在写入处检查有效批准和目标事实版本，使用目标系统的条件更新或等效锁。幂等键用于同一次提交的查重和回查。病历以草稿状态保存。医嘱由医生对确切变更授权。 |
| `read_back(receipt, task)` 与 `find_submission(task)` | 从原系统独立读取对象和完整保存内容，并提供一手截图或观察时间与资源版本。回查只返回唯一可核对的对象。找不到或存在多个候选时保留待核对状态。 |

## 业务输入和结果

运行时使用 `TaskRequest.inputs` 传入本次任务资料。部署方将每项读取映射到带来源的 `Fact`，结构化字段放在 `details`。规则引用随提案保存。

| 工作 | 运行时读取的结构与输出 |
| --- | --- |
| 住院、门诊病历 | 规则声明 `sections`，事实提供 `details.section`，医师本轮补充放在 `inputs.sections`。形成带出处的可审阅分节草稿。 |
| 新收、调整医嘱、出院带药、组套 | 事实提供本地目录和现行医嘱或组套条目。医生给出 `inputs.changes`，含动作、目录代码、依据和所需用法字段。更改和停用绑定原对象，组套携名称、版本和生效日。 |
| 历史病历 | `history_collection` 事实保留采集包、就诊标识和来源。同一患者的分页内容合并去重，身份或内容冲突需要核对。产出沿用结构化病历格式。 |
| 出院质控与文书提醒 | 使用院方的 `required_documents`、`deadline_days` 和每份文书状态。提醒需要明确的触发日及本轮 `as_of`，输出到期项与缺失事实。 |
| 专病队列与随访 | 使用院方确认的 `include`、`exclude` 或 `intervals`。队列输出纳入、排除或待核实及证据。随访输出到期、完成或待补资料。院方负责调度及对外提醒渠道。 |

确定性工作流整理院内事实和医生说明，药物选择、剂量方案和正式签署由医生决定。模型或知识组件可通过既有 `ClinicalKnowledge` 接口接入院方环境，医学验证和临床开放随具体能力进行。[接口与二次开发](adapter-development.md)
