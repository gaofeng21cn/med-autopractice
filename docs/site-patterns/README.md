# 医院 UI 适配模式库（合成脱敏模板）

本目录与 `examples/patterns/` 提供 4 类典型临床终端 UI 架构的 **声明式 Playbook v2 骨架模板**。所有模板均不含任何真实医院名称、厂商名称、真实页面 URL、真实患者数据或登录凭据。

## 模式选型表

| 架构类型 | 适用通道 | 关键要点 | 模板文件 |
| --- | --- | --- | --- |
| 经典 Win32 C/S 工作站 | `computer_use` (`WindowsUIADriver`) 或 `ip_kvm` 兜底 | 依赖控件树名称/ClassName 与剪贴板备份还原原子粘贴；弹窗需显式 `handle_dialog` 处理 | [win32_cs_playbook.json](../../examples/patterns/win32_cs_playbook.json) |
| WPF / .NET 住院医生站 | `computer_use` (`WindowsUIADriver`) | 使用稳定 `AutomationId` 作为 `selector`；虚拟化列表需 `scroll_collect` 去重采集 | [wpf_workstation_playbook.json](../../examples/patterns/wpf_workstation_playbook.json) |
| 老旧多框架 Web 门户（`iframe`） | `computer_use` (`BrowserCDPDriver`) | 跨 `iframe` 查找编辑器与检验表；注意异步表格翻页等待 `wait_state` | [legacy_web_portal_playbook.json](../../examples/patterns/legacy_web_portal_playbook.json) |
| 现代 SPA 前后端分离 HIS | `computer_use` (`BrowserCDPDriver`) + XHR 被动监听 | DOM 写后直接利用只读 XHR 网络监听与 `read_text` 双重印证 | [modern_spa_his_playbook.json](../../examples/patterns/modern_spa_his_playbook.json) |

## 现场复用流程

1. 将模板复制到**仓库外**的本院部署目录 `playbooks/` 中，根据实际控件树/DOM 选择器或 KVM 区域锚点调整 `selector`、`target` 与 `region_hint`。
2. 保持所有步骤初始 `state` 为 `"unverified"`，运行 `scripts/site_sync.py` 检查结构合规，并运行 `scripts/site_qualify.py` 完成 5 项压测后再在受控范围内晋级为 `"qualified"`。
