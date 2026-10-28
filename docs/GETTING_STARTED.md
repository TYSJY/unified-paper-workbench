# 快速开始 / Getting started

## 当前发行状态

提供源码与 Windows 安装预览版 `0.12.0b11`。安装包、空工作区初始化和隔离 GUI 启动已校验，这不等于已在另一台电脑完成完整付费 AI 研究流程。

普通 Windows 用户优先从 [GitHub Release](https://github.com/TYSJY/unified-paper-workbench/releases/tag/v0.12.0b11-preview) 下载安装包。无需先安装 Python；具体步骤见 [Windows 预览版说明](WINDOWS_PREVIEW.md)。以下源码步骤面向开发者。

## 源码环境

当前本机验证环境：Windows 11、Python 3.12、Qt Quick / PySide6。Python 元数据的最低版本不代表已经测试了所有对应平台。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\tools\prepare_codex_runtime.ps1 -Python .\.venv\Scripts\python.exe
```

需要本科 Word 导出时，另外准备 Word 运行时：

```powershell
.\tools\prepare_word_runtime.ps1 -Python .\.venv\Scripts\python.exe
```

本机还需要安装 Microsoft Word，才能执行当前的原生分页、目录更新和 Word PDF 预览流程。期刊稿件的 TeX 编译需要对应 LaTeX 工具和依赖。

## 完整执行前需要工作区模板

仓库的 `workspace_templates/` 提供干净的工作区清单和工作流提示词；不含账号、论文或历史运行记录。Windows 安装程序为首次用户复制模板并登记工作区，已有用户保留选择。源码模式下可以把该目录复制到自己的数据目录，再通过 `--workspace <目录>` 启动。

`workspace.py` 仍保留历史兼容路径。正式使用应选择受管工作区；选刊受保护提示词中的旧路径由 SDK 的工作区绑定解释为当前目录。完整论文执行还需要自己的模型授权、资料及相应 Word/LaTeX 环境，尚未通过独立电脑的全流程验收。

## 查看真实界面演示

仓库提供只包含虚构任务的隔离界面渲染脚本，不需要模型调用：

```powershell
$env:PYTHONPATH = (Get-Location).Path + ';' + (Join-Path (Get-Location) 'runtime/sdk-0.147.0')
.\.venv\Scripts\python.exe tools/render_public_demo.py --output build/public-demo-local
```

该命令生成软件截图，不是完整科研流程的执行记录。

## 已有可用工作区的用户

1. 在软件中切换到自己的论文工作区。
2. 设置 → AI 账号，完成自己的官方授权。
3. 设置 → 阶段与 Step，选择提示词方案并检测可用模型。
4. 本科论文填写题目、导入学校要求；无需用户预先提供大纲。
5. 图表页选中论文，使用“打开论文 PDF”或“打开当前阶段产物目录”。

AI 用量由所连接的服务决定，开源许可证不包含模型额度。外部站点的登录与最终操作需要用户自己的访问权限。

## 维护者构建

`tools/build_exe.ps1 -StageOnly` 会先运行测试，再生成隔离候选。该脚本目前固定校验维护者电脑上的 Python/Git 路径与哈希，移植到其他环境前需要调整受信工具链配置；不能把它当作已经完成的跨机器发布流水线。

See [FEATURES.md](FEATURES.md) for scope and [ROADMAP.md](ROADMAP.md) for remaining work.
