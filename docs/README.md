# Med Auto Practice 文档索引

本仓库遵循 [OPL 文档生命周期政策](https://github.com/gaofeng21cn/one-person-lab/blob/main/docs/policies/docs-lifecycle-policy.md)（该政策为家族级规则 owner）。

## 核心文档分工

| 文档 | 唯一职责 |
| --- | --- |
| [product.md](product.md) | 产品定位、一期 8 项范围、产品分工与复核边界 |
| [architecture.md](architecture.md) | 当前源码模块归属、OPL Agent 共用基座、统一知识来源边界与架构决策（ADR-1 至 ADR-4） |
| [agent-contract.md](agent-contract.md) | 任务调用输入/输出契约、审批、独立回读与幂等恢复语义 |
| [workflows.md](workflows.md) | 一期及扩展临床工作流输入/产物/复核门槛与规则维护 |
| [clinical-record.md](clinical-record.md) | 采集包到结构化病历清洗规则、命令与禁用边界 |
| [research-dataset.md](research-dataset.md) | 结构化病历到固定列研究长表与归一化编码规则 |
| [getting-started.md](getting-started.md) | 安装、Codex 加载、合成病例及合成站点演练入口 |
| [hospital-adapters.md](hospital-adapters.md) | 接入通道选型、FHIR R4 示例与适配器二次开发指南 |
| [site-patterns/README.md](site-patterns/README.md) | 4 类典型临床终端 UI 架构的合成脱敏 Playbook v2 骨架模板索引 |
| [site-survey.md](site-survey.md) | 院内系统调研、私有 Playbook 合同、KVM 界面资格与失败恢复 |
| [site-integration.md](site-integration.md) | 仓库外私有部署档案、`deployment.json`/`provider.py` 接入、防误提交 SOP 与知识入口 |
| [roadmap.md](roadmap.md) | 当前已实现范围、分阶段演进路线与真实院内验收条件 |
| [public-scope.md](public-scope.md) | 公开仓库 vs 私有部署边界、披露政策与提交前检查 |

## 验证入口

```bash
python3 scripts/package_metadata.py --write-lock
python3 scripts/public_release_check.py
python3 scripts/project_status.py --json
python3 scripts/site_plan.py revise_orders
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```
