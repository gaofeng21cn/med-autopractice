# 快速上手

MAP 的本地演示只需要 Python 3.10+。在 macOS、Linux 或 WSL 的终端执行以下命令，即可检查版本并走完一次合成医嘱任务。演示使用 Python 标准库和临时目录。

```sh
git clone https://github.com/gaofeng21cn/med-autopractice.git
cd med-autopractice
python3 scripts/project_status.py --json
python3 scripts/map_demo.py
```

`map_demo.py` 调用合成 CLI，依次生成拟变更、提交模拟医生对确切提案和事实版本的批准、回读模拟系统对象，再记录模拟医生验收。输出中的 `mode` 为 `synthetic`，状态依次为 `needs_pre_submit_review`、`needs_clinician_acceptance`、`accepted`。命令退出时清理临时任务库。

## 在 Codex 中加载 MAP

使用支持 `codex plugin marketplace add` 和 `codex plugin add` 的 Codex CLI。以下命令以 Codex CLI 0.170.0 验证。

```sh
codex plugin marketplace add https://github.com/gaofeng21cn/med-autopractice.git --ref main
codex plugin add med-autopractice@med-autopractice
codex plugin list --marketplace med-autopractice --json
```

在新会话里输入「使用 Med Auto Practice 查看能力状态，并运行合成医嘱演示。报告待复核状态、系统回读和最终状态。」Codex 对话使用宿主自身的账号。合成 CLI 可以直接运行。

开发自己的 fork 时，从已审阅的 checkout 根目录用 `codex plugin marketplace add .` 登记源码。已配置同名 marketplace 时，先用 `codex plugin marketplace list` 确认当前来源再切换。Git 来源可以用 `--ref` 指定已审阅的提交。

## 分步体验与恢复

在源码根目录执行以下命令。任务库和审计文件保存在同一临时目录，终端退出后仍可通过该路径查看。

```sh
map_run_dir=$(mktemp -d)
python3 scripts/map_runtime.py start --store "$map_run_dir/tasks.sqlite" \
  --request examples/synthetic/revise-orders.json
```

从输出记录 `task_id`、`proposal.digest` 和 `snapshot.revision`。查看提案内容后，把这三个值分别填入下面的变量。

```sh
map_task_id='<task_id>'
map_proposal_digest='<proposal.digest>'
map_facts_revision='<snapshot.revision>'

python3 scripts/map_runtime.py submit --store "$map_run_dir/tasks.sqlite" \
  --task-id "$map_task_id" --clinician demo-doctor \
  --digest "$map_proposal_digest" --revision "$map_facts_revision"
```

检查 `execution.readback` 中的对象、状态和提案摘要。确认后执行模拟验收。用同一任务库与任务 ID 查询状态，或用同一份请求重复执行 `start` 以恢复任务。

```sh
python3 scripts/map_runtime.py review --store "$map_run_dir/tasks.sqlite" \
  --task-id "$map_task_id" --clinician demo-doctor --accept
python3 scripts/map_runtime.py show --store "$map_run_dir/tasks.sqlite" \
  --task-id "$map_task_id"
```

每份新任务使用独立的 `request_id`。`reconcile` 用于回查提交结果不确定的任务。各状态及证据要求见 [Agent 调用契约](agent-contract.md)。

## 按任务继续

| 目标 | 入口 |
| --- | --- |
| 用合成采集包生成结构化病历和研究长表 | [病历结构化示例](../tests/fixtures/README.md) |
| 学习如何配置部署依赖、站点调查和操作档案 | [部署档案演练](site-integration.md#运行合成部署示例) |
| 替换提案生成器或实现系统适配器 | [二次开发](adapter-development.md) |

公开版提供合成运行时和只读部署计划。真实临床执行须由各站点完成适配、授权和逐项资格验证。
