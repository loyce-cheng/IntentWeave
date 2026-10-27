# Worldmind v0.1 重建进度

本文保留 0.1 历史交付。当前工作见 [0.2 升级进度](UPGRADE_V02.md) 和 [0.2 验证报告](TEST_REPORT_V02.md)。

本轮从 `fa1638a589d1d1e505bfa52f88c0ad63a2c64570` 的规划仓库重新实现。
没有恢复旧实现、旧测试或旧提交。开发分支：`codex/rebuild-v0.1`。

## 已完成

- 仓库、分支、远程、工作区、AGENTS.md 检查：初始工作区干净，无 AGENTS.md。
- 已实际创建并推送开发分支，远程哈希与初始本地 HEAD 一致。
- 阅读 README、汇编及 01–07 计划；核对汇编八部分与分篇正文一致。
- 确定以 07 的 v0.1 范围为基础，以本轮用户要求扩充多供应商协议、额度与测试。
- 阶段 1：实现离线动作模型、受限观察、共享 BFS、连续窗口修复、总预算、
  制作取消、固定商人资源竞争、导出与逐步重执行校验。
  本轮 `.venv/bin/pytest -q`：24 passed（仅内核阶段，不代表最终完整测试）。
- 阶段 2：18 个供应商/服务配置，5 类协议的文本提议适配；
  持久请求额度、缓存、数值用量审计和错误脱敏。官方来源及未确认项见 PROVIDERS。
  本轮累计 pytest：117 passed；全部供应商验证为协议模拟，没有真实付费请求。

## 实施与验收顺序

1. **离线内核**：七地点、快递员/医生/商人、六动作、三目标；
   权威执行、受限观察、Full/Local 同搜索与总预算；输入轨迹与逐步哈希回放。
   验收正常/断路/缺材/未知/预算/取消/重放损坏及信息隔离。
2. **可选模型**：核查官方文档；按协议复用，供应商差异显式配置。
   严格提议校验、并发/时限/大小限制、SQLite 请求额度/缓存/审计；
   协议模拟验证，不启用真实付费调用。
3. **API 与界面**：会话隔离、暂停/单步/运行/重置、干预、对照、导入导出；
   pytest、TypeScript、构建、Playwright 实跑。
4. **交付**：启动/配置/供应商/测试/限制文档，阶段提交推送，最终 PR。

## 本轮边界与调整

- 保留 Python/FastAPI + TypeScript，Vite 只承担前端开发和构建。
- 单进程、每会话单写者；CPU 工作有界，API 不持锁等待网络。
- 世界事件保留在有界会话中，导出本地；SQLite 专用于持久模型额度、缓存和审计。
- 一个焦点角色规划，医生接收交付，商人执行固定规则；不实现多自主 Agent、承诺协商、
  Godot、概率信念、训练或论文级统计结论。
- 六动作采用明确离散 tick 语义；制作跨 tick 消耗与产出，取消不退款。
- 不部署付费服务，不调用真实模型；开发/本地交付与公开托管验收分开记录。
- 供应商“文档覆盖”“协议模拟通过”“真实联通”分开，不以自定义 URL 声称全兼容。

## 阶段 3 与交付状态

阶段 3 已实现会话 API、控制台界面和回放操作，检查点当时后端 128 passed、Chromium 端到端 6 passed。最终补充验证结果见下方。已创建 [PR #1](https://github.com/loyce-cheng/worldmind/pull/1)，未合并 main。

内核阶段首次测试发现测试轨迹在
第二步已到达药圃，不能继续要求缺材世界观察相同；现将桥梁隐藏测试与药圃接触
释放测试分开，并明确断言首次合法观察后的差异。未删除信息隔离检查。

## 恢复工作

```bash
git clone https://github.com/loyce-cheng/worldmind.git
cd worldmind
git switch codex/rebuild-v0.1
git pull --ff-only
git status --short
```

阅读本文件、README 和 `docs/plan/07-两周上线计划.md`。
每阶段执行相关验证，检查 diff，commit 后 push，并比较
`git rev-parse HEAD` 与 `git ls-remote origin refs/heads/codex/rebuild-v0.1`。
本地提交不等于远程保存；推送失败时必须明确报告并先恢复保存通路。

阶段 3 验证环境：Python 3.12 / Node 24；系统 Chromium 151。Playwright 管理浏览器下载域被网络策略拒绝，使用已安装的系统 Chromium 完成实际浏览器测试。测试所需本机网络权限已启用。

## 最终交付检查

- 后端累计 151 passed；语句覆盖率 86.47%；前端类型、格式、构建与 6 项 Chromium E2E 通过。
- 官方文档进一步复核后新增方舟配置，并更新 MiMo 的鉴权与输出上限；最终 19 个配置、5 类协议。
- 7 类 / 14 份 Full/Local 对照记录可重放，保留 Local 开销高于 Full 的负面结果。
- README、配置示例、供应商矩阵、实现边界、测试报告、CI 及恢复步骤已补齐。
- 已推送检查点：`5b13f72`（范围）、`514027e`（内核）、`de0aa43`（模型）、`c7e63ed`（API/界面）。
- 完整源码/文档/记录提交 [`6379753`](https://github.com/loyce-cheng/worldmind/commit/6379753a37e007c4055d81b72e4e00c112d076e4) 已推送，实际核对 `ls-remote` 与本地 HEAD 一致。此后文档更新仍沿用同一远程分支。
- 交付 PR：[#1](https://github.com/loyce-cheng/worldmind/pull/1)；[远程分支](https://github.com/loyce-cheng/worldmind/tree/codex/rebuild-v0.1)。GitHub Linux/Windows 后端已通过；初次浏览器暂停断言发现响应等待问题，已补充确认状态及延迟响应验证，失败证据见 TEST_REPORT。修复后的远程结果见 PR Checks。使用远程分支 HEAD 恢复，不依赖本地临时目录。
- 未启用付费调用、未部署服务、未合并 main；真实联通、公开托管、Windows 本机和研究结论仍待后续验证。

### 从源码恢复运行

按 README 安装 Python 锁定依赖和 editable 包，`npm --prefix web ci`，
`npm --prefix web run build`，运行 `python -m worldmind.cli serve`。
`docs/evidence/recordings` 可直接导入演示；`scripts/evaluate.py` 能重新生成所有例子。
SQLite 账本和 API 密钥不在 Git 中。若未来启用真实模型，必须单独备份持久账本；
账本丢失时不得以新空账本冒充已恢复额度。
