# 院内信息系统调研与操作档案

本页给出界面通道的站点调研方法，其中设备操作步骤适用于 IP KVM。部署可先评估医院原生接口与中间件，直接桌面或浏览器会话使用宿主 Computer Use。[接入选型与原生示例](hospital-adapters.md)

本页是 MAP 调研院内 HIS、EMR、LIS、RIS/PACS、护理、输血和统一门户的实施合同。调研的目标不是把某一家产品写进 MAP，而是把“医生要完成的临床任务”映射为可核验的系统语义操作，并为每个部署保存可撤销的资格证据。

## 三层组织

| 层 | 存放位置 | 内容 | 是否可进通用 Git |
|---|---|---|---|
| 通用合同与工具 | MAP 仓库 | 系统角色、依赖 ID、语义操作字段、状态机、调研检查规则、只读计划器 | 可以，不含现场资料 |
| 部署调研档案 | `~/.config/med-auto-practice/sites/<deployment-id>/` 或院方指定私有目录 | 厂家/版本引用、角色、页面入口、语义操作、等待与恢复规则、证据引用 | 不可以 |
| 证据与运行数据 | 院方批准的私有证据/任务存储 | 截图、操作 trace、患者/就诊正文、原始 OCR、医生验收记录 | 不可以 |

主 Skill 把需要院内接入的任务交给医院适配 Skill。后者按本次操作选择直接 Computer Use 或隔离终端的 IP KVM，组织观察、验证并记录结果。使用 IP KVM 时由 `glkvm-native` 管理连接、画面、OCR、键鼠、等待和控制权。站点档案解释当前部署页面，医学模块负责基于患者事实形成候选提案。

## 调研工具合同

`contracts/site-survey.schema.json` 定义仓库外的 `map-site-survey.v1` 档案。`scripts/site_survey.py` 是只读计划器，不连接医院、不执行点击，也不把证据正文读入 Git。它检查：

- 系统 ID 唯一且引用通用角色；操作按依赖 ID 唯一；每个操作有 `semantic_action`、读写模式和状态。
- `observed` 必须有私有证据引用；`qualified` 必须同时有证据引用和资格引用；`revoked` 不能被计划视为可用。
- 档案不得含患者、就诊正文、截图字节、密码、Cookie、token 或秘密字段。
- 档案路径必须在源码仓库之外。

示例命令（使用真实部署时替换为院方私有路径）：

```sh
PYTHONPATH=src python3 scripts/site_survey.py \
  --profile "$HOME/.config/med-auto-practice/sites/<deployment-id>/site-survey.json" \
  --dependency bind_inpatient \
  --dependency read_current_orders
```

输出是调研状态和阻断项，不是临床执行许可。`qualified` 只说明某个语义操作完成了调研资格；能力目录中的 `callable` 仍需绑定患者/就诊、事实回读、写后核验、医生验收和数据处理授权。

## 私有 playbook：把高度特定的操作固化下来

页面入口、身份验证方式、角色选择与导航顺序由部署方记录在私有 playbook 中。公开示例使用合成界面和合成角色。

因此把它做成仓库外的 playbook：

| 位置 | 内容 |
|---|---|
| `contracts/site-playbook.schema.json` | 通用格式：步骤 ID、意图、KVM 原语、目标、区域提示、期望可见文本、副作用等级、失败策略、证据要求 |
| `~/.config/med-auto-practice/sites/<deployment-id>/playbooks/*.json` | 该部署的真实步骤；权限 600，永不进 Git |
| `scripts/site_playbook.py` | 只读列出或展开 playbook，校验系统引用必须存在于同一部署的 survey pack |

字段约束的用意是让 Agent 能执行、但仍保留判断：

- `primitive` 限定为既有 KVM 原语（观察、等待、点击、双击、ASCII 输入、组合键、滚轮、拖拽）加上 `browser_autofill_select`、`human_action`、`human_handoff`，不发明新的设备调用。
- `expect_text` 是每一步的可见校验点；没有通过就按 `on_failure` 处理，不能盲目继续。
- `effect` 区分 `read_only`、`draft_write`、`reviewed_write`；含写入的 playbook 会被标记为 `contains_write`，不得当作纯浏览流程执行。
- `account_ref` 使用部署方管理的凭据来源引用；校验器会拒绝出现 `password`、`token`、`patient`、`screenshot_data` 等字段的 playbook。
- playbook 只能放在源码仓库之外；路径检查与 survey pack 相同。

```sh
PYTHONPATH=src python3 scripts/site_playbook.py \
  --playbooks "$HOME/.config/med-auto-practice/sites/<deployment-id>/playbooks" \
  --survey    "$HOME/.config/med-auto-practice/sites/<deployment-id>/site-survey.json" \
  --playbook  <playbook-id>
```

playbook 记录“在这套界面里怎么走”，医学判断和业务流程仍由 `medicine/` 与 `workflows/` 负责；playbook 不定义临床决策，也不因为它存在就自动开放任何能力。界面改版、角色变化或输入法变化时，对应步骤应改版或作废，并在现场档案里记录原因。

### 步骤要标验证状态

每个步骤有 `state`，默认 `unverified`：

- `verified`：在**本部署**的当前客户端版本上重复走通过。
- `unverified`：来自公开手册、厂商文档或他人经验，尚未在本部署复现；执行时按 `on_failure` 处理，不能当作已知可用。
- `revoked`：曾经可用，改版或权限变化后失效。

`scripts/site_playbook.py` 会输出 `verified_steps` / `steps_total`，不要把含 `unverified` 步骤的 playbook 描述为已验证流程。来自公开手册的路径尤其要点名来源并标注未复现状态——这正是“手册说右键，本机却点不出来”这类差异需要被记录而不是被抹平的原因。

## 经验往哪里沉淀

在多个通道或部署中可复用的取数和核验方法，由 MAP 医院适配 Skill 按需读取。用合成情境描述判断、证据和失败处理，实际现场步骤随私有部署档案维护。[院内系统适配与只读取数经验](../agent/knowledge/site-adaptation.md)

| 经验类型 | 例子 | 落位 |
|---|---|---|
| 设备/中间件的通用行为与故障恢复 | 控制租约续期、画面有效期、坐标换算与连接恢复 | `glkvm-native` 的 skill 与 `references/`（设备仓库，不含院内信息） |
| 某部署的界面路径与操作步骤 | 门户登录、角色科室选择、患者列表筛选、右键菜单入口 | 私有 playbook（仓库外） |
| 某部署的事实、版本、权限范围 | 使用的产品与版本、账号可见科室、菜单实际内容 | 私有现场档案 `site-systems.md`、`site-survey.json` |
| 通用合同、校验规则、工具 | playbook/survey 的 schema、只读计划器、状态机 | MAP 源码仓库 |
| 可复用的临床操作经验 | 核对就诊、确认查询覆盖、检查回读对象 | [随包产品知识目录](../contracts/durable_knowledge_catalog.json) |
| 临床能力是否开放 | 某能力是否通过资格 | `contracts/capabilities.json`，逐项、附证据 |

沉淀的触发点：一次操作成功后、一次故障定位后、一次“手册与实际不符”发现后。写 playbook 步骤时同步写 `expect_text`——它是下次执行时的自检点，也是判断界面是否改版的依据。权限、版本或分辨率变化时，把受影响步骤标 `revoked` 而不是删除，保留“为什么改”的依据。

## 现场调研顺序

### 1. 建立边界

记录部署 ID、测试/正式环境、允许的账号角色、数据处理位置、证据保留期限和人工接管方式。先确认统一门户是否能进入各系统，以及当前账号能否只读访问。不得在调研档案中记录密码或复制会话令牌。

### 2. 盘点系统角色

从入口和实际页面确认哪些系统承担：患者/就诊绑定、住院/门诊病历、医嘱、检验、影像报告与原图、护理、输血、感染监测、文书质控和统一门户。记录产品/版本只能写在私有档案；MAP 源码只使用 `inpatient_ehr`、`laboratory`、`imaging` 等通用 role。

### 3. 只读探查

使用原生 KVM 按“观察 → 按文字定位 → 等待 → 再观察”的短步骤探查，不先点击保存或提交。观察默认只取局部 OCR 文字与包围盒，优先用 `kvm_click_text`/`kvm_locate` 按可见文字取框，不靠反复截图估坐标；确需画面证据时用 `kvm_shot` 存成私有文件并引用路径，不把图片逐帧留在上下文。每项操作记录：前置页面、唯一患者/就诊标识、语义目标、分页/时间范围、成功后的可见状态、异常弹窗、退出方式和私有证据引用。姓名或床号不足以消歧；同名、分页和跨系统切换必须形成失败样例。

优先顺序是：统一门户 → 患者/就诊选择 → 当前医嘱 → 检验结果 → 影像报告定位 → 护理记录 → 文书列表/状态。PACS 原图只验证“能定位和调阅”，不把 KVM 压缩画面当诊断影像。

### 4. 形成语义操作

每项操作用稳定语义命名，例如 `bind_inpatient`、`read_current_orders`、`read_lab_results`、`save_inpatient_note_draft`、`readback_inpatient_note`。页面坐标、窗口标题、按钮文字和等待区域留在私有操作说明；工作流代码只引用依赖 ID。操作返回必须包含患者/就诊绑定、采集范围、来源对象、时间、状态、KVM trace 和证据引用。

### 5. 分级资格

- `unmapped`：尚未找到稳定入口或字段。
- `observed`：看到了页面并记录了证据，但未证明操作可重复或结果正确。
- `qualified`：在指定产品版本、角色、分辨率和环境中重复完成，目标对象能回读，证据和资格记录完整。
- `revoked`：改版、权限、设备、输入法或回读失败后撤销资格，必须重验。

读取资格和写入资格分开。病历长文输入另有资格；医嘱关键字段逐项核对；保存草稿、录入医嘱、签署、执行和打印不得互相推断。

## 形成 MAP 专业 Skill

选择 IP KVM 后，主 Skill 按以下步骤调用设备提供者。

1. `survey_status`：读取外部 survey pack，显示指定能力的依赖和阻断项。
2. `survey_connect`：确认 App/网页已退出控制页，调用 `kvm_connect`，回读设备身份、分辨率、键鼠和运行版本。
3. `survey_observe`：先确认页面身份与唯一锚点文字，用局部 OCR 和 `kvm_locate`/`kvm_click_text` 完成一个语义操作；只有需要长期证据时才 `kvm_shot` 存私有文件。
4. `survey_probe_read`：执行只读导航，按条件等待，记录分页、时间范围、来源和回读状态；不执行写入。
5. `survey_record`：更新私有档案中的状态和证据引用；仓库只接收去现场化的合同变更。
6. `survey_qualify`：在重复测试、目标回读和人工复核完成后，才把操作标为 `qualified`；任意身份、版本、权限或画面不一致立即停止。
7. `survey_release`：释放 KVM 控制权并回读断开状态，保留私有证据引用。

调研期间模型只看到完成下一步所需的局部画面；原始截图、OCR 和患者正文留在获准私有证据面。调研结果不是诊疗建议，也不会自动开放医嘱或病历能力。

## 当前落地顺序

第一批只调研当前住院双主线依赖：患者/住院绑定、当前医嘱、检验、影像报告、护理记录、病程草稿入口和医嘱录入/回读入口。先完成只读资格，再做非医疗 Windows 中文输入资格，最后在医生监督下验证草稿保存和医嘱录入。既往资料批量、门诊、组套、出院和时效质控复用同一合同，按能力逐项扩展。

调研完成的判据是“语义操作可重复、目标对象可回读、证据可追溯、失败会停、医生能验收”，不是“能点到页面”或“厂商 App 显示在线”。
