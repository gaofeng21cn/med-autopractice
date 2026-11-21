<p align="center"><img src="assets/branding/medautopractice-logo.png" width="112" alt="Med Auto Practice 标志"></p>
<p align="center"><strong>中文</strong> · <a href="README.en.md">English</a></p>

# Med Auto Practice（MAP）

MAP 是面向医生的开源临床工作智能体。它把门诊与住院资料整理成带来源的结构化病历，把医嘱拟变更和病程草稿交给医生审阅，并跟踪医院系统的回读结果。医生可以直接在 Codex 中发起任务，部署团队可以按本院条件接入现有信息系统。

## 先跑一个病例

在装有 Git 和 Python 3.10+ 的电脑上运行。示例使用合成数据和 Python 标准库。

```sh
git clone https://github.com/gaofeng21cn/med-autopractice.git
cd med-autopractice
python3 scripts/map_demo.py
```

你会看到一笔合成医嘱从待审阅到模拟医生批准、从系统回读到最终验收。输出中的 `readback.object_ref` 是重新打开的目标对象，`statuses` 记录了任务如何走到 `accepted`。

## 安装到宿主

在 OPL 软件中心搜索 **Med Auto Practice**，或使用同一套软件包命令。

```sh
opl packages install med-autopractice --json
```

也可以直接安装到 Codex。

```sh
codex plugin marketplace add https://github.com/gaofeng21cn/med-autopractice.git --ref main
codex plugin add med-autopractice@med-autopractice
```

在新会话输入「用 MAP 跑一次合成医嘱，给我看医生批准的版本和系统回读」。要亲自执行审批、修订和中断恢复，请按分步示例操作。[快速上手](docs/getting-started.md)

## 用自己的医院环境

先选一项工作，例如为住院病程草稿读取已知检验结果。信息部门可以用医院开放的原生 API 或集成中间件读取结构化数据。只有业务界面可用时，可通过 Computer Use 操作浏览器或桌面。需要硬件接入隔离终端时，可选 IP KVM。同一任务可以组合多种通道。

| 想做的事 | 从哪里开始 |
| --- | --- |
| 看懂一次原生取数 | 运行 `python3 examples/synthetic/fhir_read.py`。本机 FHIR R4 测试端点返回一条与患者、就诊和资源版本绑定的定量结果。[接入示例](docs/hospital-adapters.md) |
| 规划本院试点 | 用全合成部署样例生成仓库外的配置。逐项指定读写通道，查看事实来源、依赖缺口和医生复核点。[部署档案演练](docs/site-integration.md#运行合成部署示例) |
| 从采集包生成结构化病历 | 导出带来源的病历和九张固定列研究表。阅读版先给结构化资料，再给问题清单和采集原文。[样本与命令](tests/fixtures/README.md) |
| 接入自有模型、医院中间件或桌面宿主 | 复用 `ClinicalKnowledge` 和 `ClinicalSystem`，在同一任务内核验证资料采集、提案、审批和回读。[二次开发](docs/adapter-development.md) |

## 临床工作如何推进

一期覆盖既往资料、门诊与住院医嘱组套、新收与调整医嘱、出院带药、门诊与住院病历、出院质控和时效提醒。首批现场验证场景是住院病程草稿和医嘱调整。专病队列与动态随访排在后续阶段。[产品范围](docs/product.md) · [工作流与依赖](docs/workflows.md)

MAP 绑定患者和就诊，把事实、时间、来源和待核实项交给医学组件草拟。医生按具体版本批准医嘱，适配器提交后从目标系统重新读取对象、内容和状态，医生在同一任务上验收或提出修订。中断后可用任务 ID 恢复；提交结果不确定时先查原系统。[任务契约](docs/agent-contract.md)

## 版本与参与开发

公开版提供 Codex 主 Skill、可恢复的合成任务、结构化病历导出、FHIR 定量结果读取组件和站点适配接口。真实工作流由部署方按本院授权逐项验证，站点配置、患者资料和证据放在受控存储中。查看当前版本和能力状态可运行 `python3 scripts/project_status.py --json`。[能力目录](contracts/capabilities.json) · [架构](docs/architecture.md) · [路线图](docs/roadmap.md)

源码与文档采用 Apache-2.0 许可证。欢迎贡献可用合成数据复现的工作流、适配器与测试。[贡献指南](CONTRIBUTING.md) · [公开资料范围](docs/public-scope.md) · [安全反馈](SECURITY.md) · [许可证](LICENSE)
