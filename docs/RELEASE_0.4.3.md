# V1 原地换芯 0.4.3 本机发布记录

日期：2026-07-12

## 发布范围

- 保留 V1 Tk UI、用户操作入口、公开方法签名和状态投影结构；
- 保留稳定投稿与智能投稿双路线，未切换任何投稿生产执行权；
- 写作 10、画图 6、选刊作者 4，共 20 份用户调优内部 Prompt 保持逐字节不变；
- 新增 V1 兼容合同、后端动作路由和首个纯 V2 读模型；
- 正式运行中 `_plan_for` 仍默认使用 legacy，实现切换只在自动测试中验证。

## 回滚基线

- 冻结目录：`D:\Documents\大一统_backups\V1_FROZEN_BASELINE_0.4.2_20260712T002900`
- 冻结 EXE 版本：`0.4.2`
- 冻结 EXE SHA256：`24A7DE28F9895939455B264F32CA3760412C549851E36C9EB5BEFC6D44EDA607`
- 备份 manifest：3,215 个文件，1,155,811,216 字节，逐文件校验 0 失败；
- 已在备份副本内完成测试、Ruff、EXE CLI/doctor 和 20/20 Prompt 恢复演练；
- 私密账号资料随备份保留，ACL 仅允许当前 Windows 用户和 SYSTEM。

## 0.4.3 发布物

- 路径：`D:\Documents\大一统\dist\UnifiedPaperOrchestrator.exe`
- 文件大小：11,855,512 字节
- SHA256：`0269048D1A52CD3C393830CC3623713AC9CC83CDC9517D9D8F587AACF1D7ABED`
- Windows FileVersion / ProductVersion：`0.4.3`
- Authenticode：`NotSigned`

## 自动验证

- 全量测试：397 passed，54 subtests passed；
- V1 兼容、路由、纯读模型专项：39 passed；
- Ruff：通过；
- 全部 Python 源码编译：通过；
- 隔离构建环境探针：通过；
- 候选 EXE `--help`、`--version` 和无效参数退出码：通过；
- 正式 EXE `doctor`：PASS；
- 正式 EXE `scheduler-status`：写作、画图、选刊作者、稳定投稿均为 PAUSED；
- Smart Submission：manual / PAUSED，local queue 0，online queue 0，running 0；
- 20 份内部 Prompt：20/20 SHA256 匹配；
- 发布完成后 EXE 运行进程：0；构建临时目录已删除。

## 尚未纳入本次发布门

- 未执行从写论文到在线最终投稿的完整长链路实稿测试；用户已决定停止该低效率测试；
- 未做 Authenticode 代码签名；
- 冻结备份位于同一 D 盘，只能防误改和误删，不能替代另一物理磁盘或离线加密副本；
- UI 合同当前以源码结构和既有测试为主，后续仍需补充隔离 widget 树与关键点击冒烟。
