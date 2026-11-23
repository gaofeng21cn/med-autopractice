# 部署档案配置

MAP 源码面向多个医院和系统版本。源码仓库维护通用数据模型、工作流、适配器接口、字段角色和验证机制；实际医院名称、供应商、系统版本、院内地址、账号角色、UI 路径、截图和资格证据属于部署数据，必须放在仓库之外，不进入任何公开或私有 Git 历史。

## 两种资料的边界

| 内容 | 归属 | 示例 |
| --- | --- | --- |
| 通用系统角色 | MAP 源码 | `inpatient_ehr`、`laboratory`、`imaging`、`transfusion` |
| 通用任务依赖 | MAP 源码 | `read_current_orders`、`read_lab_results`、`save_inpatient_note_draft` |
| 实际产品与供应商 | 私有 site profile | 某部署使用的 HIS/EHR 厂家与产品版本 |
| 操作方式 | 私有部署资料 | 门户路径、页面锚点、等待条件、查询字段、回读步骤 |
| 资格证据 | 私有部署资料或获准的证据库 | 经核实的操作 trace、适用角色、复核人和失效条件 |
| 真实患者任务数据 | 独立的受控运行存储 | 不得放进源码 profile、fixture、日志或版本库 |

字段名称、任务依赖和校验算法应保持通用；产品条件差异通过 adapter/profile 配置，不在工作流代码中写医院名称分支。不同医院可选不同设备后端，也可在获准时使用业务系统原生 API、CLI 或 Agent 接口；顶层任务完成语义与医生复核要求保持一致。

## 本机私有 profile

默认目录建议为：

```text
~/.config/med-auto-practice/sites/<deployment-id>/site-profile.json
```

将 profile 明确放在仓库之外。源码中的 `.gitignore` 目录只作为误操作防线，不构成存储边界；加密、访问控制、备份和保留期限由部署政策决定。不要把文件复制到仓库再依赖 ignore 规则。

设置环境变量或使用命令参数：

```sh
MAP_SITE_PROFILE="$HOME/.config/med-auto-practice/sites/<deployment-id>/site-profile.json" \
  python3 scripts/site_plan.py revise_orders

python3 scripts/site_plan.py --profile \
  "$HOME/.config/med-auto-practice/sites/<deployment-id>/site-profile.json" revise_orders
```

MAP 启动时用 `python3 scripts/site_plan.py --inspect` 确认当前选择。参数 `--profile` 优先于 `MAP_SITE_PROFILE`，两者都未指定时只读通用模板。指定的档案失效时，检查会报告错误。加上 `--dependency read_lab_results` 只看这一项操作的通道、状态和证据引用，加上 `write_inpatient_notes` 则查看写病程所需的各项依赖。

新部署可在院方指定的 Git 仓库外私有目录中运行 `python3 scripts/site_plan.py --init-profile <新文件路径>`。命令从[通用依赖合同](../contracts/site-dependencies.json)创建一份全为 `unmapped` 的档案，以私有权限写入新文件，已有文件会得到提示。部署方可以为这份档案填写自己的 `profile_id`，随后按实际操作保存系统信息和证据引用。宿主通过 `MAP_SITE_PROFILE` 选择本部署，后续任务沿用同一档案。

详细的页面路径、语义操作、读取字段、恢复方式和证据保留信息目前应放在同一仓库外部署目录中的独立私有资料；`site_plan.py` 尚不读取这些 UI 说明。后续适配器必须增加经过版本化的操作 profile schema 和验证后，才可用机器读取这些内容。`observed` 仅代表观察到界面；`qualified` 需要明确资格证据，但仍不等于 `contracts/capabilities.json` 中的能力已开放。

## 运行合成部署示例

在源码根目录执行以下命令。示例根据通用模板在仓库外生成配置，为检验、医嘱、护理和影像分别选择四种通道。输出中的 `transport` 表示该操作的接入方式，`observed` 表示已有合成观察记录。起草自己的正式配置时，从 `contracts/site-dependencies.json` 的全空模板开始。[接入选型与 FHIR 示例](hospital-adapters.md)

```sh
map_site_dir=$(mktemp -d)
python3 examples/synthetic/configure_site.py "$map_site_dir/site-profile.json"
cp examples/synthetic/survey.json "$map_site_dir/survey.json"
cp -R examples/synthetic/playbooks "$map_site_dir/playbooks"

python3 scripts/site_plan.py --profile "$map_site_dir/site-profile.json" revise_orders
python3 scripts/site_survey.py --profile "$map_site_dir/survey.json" --dependency bind_inpatient
python3 scripts/site_playbook.py --survey "$map_site_dir/survey.json" \
  --playbooks "$map_site_dir/playbooks" --playbook demo-open-overview
```

计划会列出未配置的依赖，调研档案中的 `bind_inpatient` 处于 `observed`，操作清单中的合成步骤处于 `unverified`。三个命令都只读取本地文件。自己的部署从同一份通用依赖模板起步，保留工作流依赖 ID，并在真实资格档案里记录经验证的来源与证据。[站点调研流程](site-survey.md)

## Git 防误提交措施

- 使用 `.gitignore` 拦截本地 profile、现场档案和现场截图的常见路径。
- 使用合成部署 profile 运行测试；不可把现场 profile 改写成“匿名真实医院”后作为示例。
- 测试、fixture、文档与代码注释里只用合成标识（`SYNTH-*`、`示例*`）；不要图省事
  把真实住院号、真实诊断名或医生姓名抄进样例，哪怕只是作为“看起来真实”的演示。
- 提交前检查 staged diff、完整变更历史和待跟踪文件，确认没有机构名称、产品版本、供应商清单、内网地址、角色或现场操作细节。
- 如果已误提交，先将文件移出源码并修复当前树；若敏感站点资料已在 Git 历史中，须改写相关提交并更新远端，再核验所有分支、标签与 GitHub 读取结果。仅删除文件或增加 `.gitignore` 无法清除 Git 历史。公开发布还须审查远端保留的 PR 引用、附件、历史运行日志和发布制品。需要隔离旧记录时，从审核后的文件新建公开参考仓库。

### 已误提交后的清理 SOP

适用于真实患者标识、机构名称、内网地址等已进入 Git 历史（含已推送）的情况。
先止损、再改写、最后独立核验；每一步都留证据，顺序不要颠倒。

1. **确认范围。** 用 `git log --all -S '<泄露文本>' -- .` 找出命中的提交，再用
   `git rev-list --all` 加 `git grep` 扫全历史与所有分支，确认是单个词还是整批
   资料、是否还有相邻字段（同一行里的其他标识）。
2. **先备份。** `git bundle create <仓库外私有目录>/<repo>-prehistory-<时间戳>.bundle --all`
   并设权限 600。改写不可逆，备份是唯一的回头路。
3. **写替换规则。** 用 `git filter-repo --replace-text <规则文件>` 改写，规则文件
   一行一条 `原文==>替换文`；替换文必须是合成值，不要留空导致语义损坏。
   规则文件本身含真实文本，用完即删，不要提交。
4. **改写后自检。** 重跑测试并确认工作区干净；再对若干真实标识词跑一遍全历史
   `git grep`，确认命中为 0。
5. **带前置条件推送。** 远端已有旧历史时必须改写远端。先 `git ls-remote origin
   refs/heads/main` 记录远端当前提交，再用
   `git push --force-with-lease=refs/heads/main:<旧提交> origin main`，避免覆盖他人
   期间的新提交；不能用无条件 `--force`。
6. **从远端独立核验。** 另建一个临时克隆，在克隆里重跑全历史扫描确认泄露为 0、
   HEAD 正确、测试通过，然后删除临时克隆。只在本机检查不算完成。
7. **记录。** 在任务或私有台账里记下：泄露内容、影响提交、备份 bundle 路径、
   改写后的提交、核验方式。备份保留到确认无需回滚为止。

改写历史会改变提交哈希，其他分支、标签、worktree 与协作者都需要相应同步；改写
前先确认没有未吸收的分支或待评审 PR，改写后逐一核验。

不要自行另造尚未实现的 profile schema。当前部署依赖文件从 `contracts/site-dependencies.json` 完整复制；系统 ID 必须继续引用通用 role ID，敏感产品名和版本只改在仓库外副本。站点详情如页面路径和 UI 资格应与依赖文件同目录保存，并在未来为其单独定义可校验的版本化合同。

秘密、密码、Cookie、令牌及真实患者信息不能存入 profile；profile 只保留受控凭据/证据引用。配置校验只能验证依赖结构与引用，不得仅凭 profile 自报状态开放临床能力。

## 可复用知识的公共来源入口

跨站点的操作和复核方法由 MAP 维护在随包的[产品知识目录](../contracts/durable_knowledge_catalog.json)。Codex 的两个 Skill 按任务直接读取正文。OPL 环境可用 `opl knowledge list --repo <MAP 包根>` 查看目录，也可按 `site-adaptation` 或 `record-review` 召回本次所需的方法。更新正文时同步更新目录记录的摘要和软件包内容锁。

部署操作 JSON 由站点维护。工作区或项目的制度说明、医学资料和其他私有知识通过 Framework 现有 registry 绑定。`scripts/map_knowledge.py` 是无本地存储的 CLI 薄入口，固定消费者域为 `med-autopractice`。读取范围随来源授权及工作区、项目绑定确定，写回须有独立写入 grant。可用 `OPL_BIN` 指定 Framework 的确切可执行文件。

```sh
python3 scripts/map_knowledge.py --workspace /private/workspace sources
python3 scripts/map_knowledge.py --workspace /private/workspace search --query "workflow" --limit 10
python3 scripts/map_knowledge.py --workspace /private/workspace read-source --id owner.method
```

未登记的正文先经 `discover` 取得候选，再由有权维护者按精确摘要和审阅证据使用 `register` 登记原文；已有正文修订使用 `propose` → `review` → `apply`。用户原处修改由 `inspect-source` 与 `review-source` 接受。各命令保持 Framework 原 flags，`--workspace` 和可选 `--project` 放在操作名前，reviewer、review ref、reviewed at、next review at 等放在其后。

重新使用已有知识依据前，用 `verify-dependencies --dependencies <JSON 文件>` 核对 `[{"knowledge_id":"…","sha256":"sha256:…"}]` 中的版本、有效性和访问范围。该命令的调用成功与依赖有效是两个结果：只有响应中 `knowledge_dependencies.valid` 为 `true` 才能继续依赖这些来源；`false` 时按 `issues` 重新读取和审阅，不能只检查进程退出码。此入口本身不赋予临床执行资格。

来源与记忆的唯一政策见 [Framework 知识政策](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/policies/knowledge-source-and-memory-policy.md)，参数和 schema 见[知识来源接口](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/specs/knowledge-sources.md)。Framework 无可用接口、来源未授权或版本已失效时入口返回错误，不转为直接读文件，也不自动重试未知写入。读取结果可包含私有正文，只交给获授权任务上下文；患者病历、院内截图和 JSON 操作档案继续走原有受控存储。
