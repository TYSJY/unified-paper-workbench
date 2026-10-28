# 快速开始 / Getting started

## 当前发行状态

这是源码开源预览。维护者本机已经验证 Windows GUI `0.12.0b11`，这不等于已经在全新电脑完成完整研究流程。请先确认本页的依赖与限制。

目前没有在 GitHub 上传经过公开发布检查的 Windows 安装包。不要从 Issue、评论或第三方网盘取得声称为官方的安装包。

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

当前 `workspace.py` 保留了兼容旧工作区的路径配置。新建受管工作区会从已有模板复制配置，并不自动下载完整的四阶段模板。部分期刊写作、配图和选刊提示词与工具目前位于维护者的历史模块中，没有随这份源码完整分发。

因此：干净克隆后可以研究源码、贡献功能和运行隔离界面演示，但本页不承诺克隆后即可完整生成论文。公开首发的优先工作是导出无私人数据的工作区模板、去除默认绝对路径依赖，并完成干净环境演练。

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
