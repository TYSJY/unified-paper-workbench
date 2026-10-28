# Windows 预览版下载与使用

下载入口：[GitHub Releases](https://github.com/TYSJY/unified-paper-workbench/releases)。
选择 `v0.12.0b11-preview` 的 `UnifiedPaperWorkbench-Setup-0.12.0b11-preview.exe`。
这是可安装的 Windows x64 预览版，内置 Python、Qt、Codex SDK/运行时和 Pandoc。
不需要先安装 Python，也不需要通过命令行启动。

1. 下载并运行安装程序，按提示安装到当前 Windows 用户。
2. 从桌面或开始菜单打开 **Unified Paper Workbench**。
3. 首次安装自动创建空白论文工作区；已有用户保留自己的工作区选择。
4. 设置 → AI 账号，完成自己的官方授权。
5. 设置 → 阶段与 Step，检测模型，选择期刊或本科提示词方案。
6. 本科方案填写题目并导入学校要求；随后按任务状态检查产物。

程序默认安装在 `%LOCALAPPDATA%\Programs\UnifiedPaperWorkbench`。
新建工作区位于 `%LOCALAPPDATA%\UnifiedPaperWorkbench\Workspaces\Main`。
论文、账号与授权均由用户在本地创建，安装包不包含维护者的数据。
卸载程序与论文工作区分开存放；升级不覆盖已有工作区注册表。

## 已验证范围

公开包会校验核心 EXE、安装载荷、空工作区初始化及保留已有选择的行为，
并使用空用户配置做 GUI 启动检查。这不等同于在另一台实体电脑上完成整篇
付费 AI 论文流程，也不代表真实账号授权、实验或期刊投稿已验证。

AI 服务需要用户自己的账号并消耗用量。Word 正式稿的原生分页与 PDF 渲染
仍需要本机 Microsoft Word；期刊 TeX 编译需要 LaTeX 环境。投稿能力依赖
具体站点适配和用户授权。

安装包当前没有商业代码签名，Release 附带 `SHA256SUMS.txt`。库的许可文本、
完整依赖清单和源码获取说明随安装提供；Qt/PySide6、Pandoc 对应源码包在
同一 Release 的 `THIRD_PARTY_SOURCES.zip` 中。项目源码见该 Release 的标签。
