# GitHub 首发与传播方案

## 目标定位

项目名：**Unified Paper Workbench / 大一统论文智能工作台**。
仓库：**TYSJY/unified-paper-workbench**。

一句话：**从题目与资料，到写作、图表和 Word / PDF 交付的本地可视化工作台。**

英文 About：A local desktop workspace for AI-assisted academic writing, figures, Word/PDF delivery, and submission workflows. Built with Python and Qt.

建议 Topics：academic-writing, research-assistant, desktop-app, codex, python, qt, latex, docx, workflow-automation, ai-agent。

## 参考对象与采用的结构

2026-10-28 核对 GitHub 项目页面，约数会变化：

| 项目 | 页面所示 Star | 参考的呈现方式 |
|---|---:|---|
| [GPT Researcher](https://github.com/assafelovic/gpt-researcher) | 30.3k | 先说明使用场景与产物，再给文档和上手入口 |
| [AutoResearchClaw](https://github.com/aiming-lab/AutoResearchClaw) | 14.8k | 从明确结果切入，展示交付物结构，再说明流程差异 |
| [Cherry Studio](https://github.com/CherryHQ/cherry-studio) | 53k | 产品化首屏、语言入口、界面演示与社区贡献路径 |

这里只参考结构，文案和视觉均围绕大一统自己的能力编写。Star 数不能证明某种 README 写法必然带来增长。

## 首屏安排

1. 产品名称、简洁横幅与一句话用途。
2. 真正的软件截图，并清楚标明虚构演示数据。
3. 期刊、本科、图表、Word/PDF 的具体使用场景。
4. 快速开始入口、依赖和真实限制。
5. 工作流、路线图、贡献与许可证。

不放虚假 Star、未经验证的录用承诺、捏造的使用人数或尚不存在的下载链接。

## 首发前最值得完成的产品工作

**首先让一个从未接触过项目的人，在干净 Windows 环境完成一次示例。** 当前工作区仍依赖历史模块模板，构建脚本仍绑定维护者工具链，这是首次体验的主要缺口。主页已经明确披露，后续应该优先解决，而不是只追加卖点。

还需：公开二进制依赖许可材料、验证干净安装、提供首个安全 Release，以及一段真实操作演示。

## 演示视频脚本（计划，尚未录制）

- 0–5 秒：打开工作台，说明把分散的写作、图表和交付统一起来。
- 5–15 秒：选择本科或期刊方案，填写题目和导入资料。
- 15–25 秒：展示任务、阶段进度与待处理提示。
- 25–35 秒：点击打开论文 PDF，展示 Word/PDF 的交付入口。
- 35–45 秒：展示提示词配置与项目开源地址。

视频只能使用已验证的实际操作；演示数据和回放应标明，不把静态界面当成已经完成的模型执行。

## 发布文案草案（未对外发布）

### 中文

我把分散在聊天窗口、脚本和文件夹里的论文工作，做成了一个本地桌面工作台：大一统 / Unified Paper Workbench。写作、图表、Word/PDF 和投稿流程可以在同一界面查看与管理，支持期刊及本科人文社科、计算机软件方案。项目以 AGPL-3.0 开源，目前是早期预览，正在优先完善干净安装和工作区模板。欢迎试用、提问题，或一起把它做成更可靠的工具。

### English

Introducing Unified Paper Workbench: a local desktop workspace for AI-assisted academic writing, figures, Word/PDF deliverables, and submission workflows. Built with Python and Qt, with journal and undergraduate thesis prompt schemes. This AGPL-3.0 early preview is focused on making research workflows visible and resumable. Clean installation and portable workspace templates are the next priorities. Feedback and focused contributions are welcome.

## 首发后的节奏

- 首日：完成 About、Topics、语言入口、真实演示和安装问题反馈渠道。
- 前三天：优先解决前几位新用户的安装问题，记录可复现的问题和修复。
- 第一周：发布一次有实际改进的更新，补齐示例和短演示。
- 之后：按用户真实卡点迭代，发布变更记录，邀请独立贡献。

推广内容先作为草案保存。没有在社交平台发布，也没有购买或制造 Star。
