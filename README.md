# Worldmind · 界行

**0.4.0** 是可运行、可观察、可回放的 NPC 行动规划演示。新增双角色协作：医生提出药材需求，快递员自主取材和交接，医生收到物资后制药送药。可切换角色认知，干预道路与资源，观察 Full 重规划与 Local 局部修复。

默认使用本地符号规划，无需 API Key，不调用收费模型。交付为源码和本地演示；未部署公开服务，未验证真实供应商联通。当前开发分支为 `codex/cooperation-v0.4`，范围与验证见 [0.4 协作协议与发布说明](docs/RELEASE_V04.md) 和 [版本记录](CHANGELOG.md)。

## 快速启动

需要 Python 3.11+、Node.js 22.12+。在仓库根目录执行：

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install --no-deps -e .
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m worldmind.cli serve
```

Windows PowerShell（无需激活虚拟环境）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm --prefix web ci
npm --prefix web run build
.\.venv\Scripts\python.exe -m worldmind.cli serve
```

打开 [本地演示](http://127.0.0.1:8000)。同一 FastAPI 进程提供 API 和构建后的页面；[健康检查](http://127.0.0.1:8000/api/health) 返回应用与录制协议版本，[接口文档](http://127.0.0.1:8000/docs) 列出 HTTP API。修改前端后重新构建；开发时可用 `npm --prefix web run dev`，Vite 将 `/api` 代理给后端。

升级已有安装时，停止服务、备份 `var/` 内数据库，更新源码后重新执行上述依赖安装、editable 安装和前端构建。保留模型额度账本；源码版本变更后也需刷新 Python 包元数据。

## 体验与存档

选择「双角色协作」，单步查看工作单从提议到接受、交接的变化。切换「关注角色」查看医生与快递员各自收到的消息、计划、库存与等待原因。可按条款取消订单，或在取材、交接、制作期间中断动作。「新协作场景配置」可调整订单期限、消息延迟和商人取材时点；点击按钮建立新场景。

药材交接完成前不会进入医生库存；制作开始即消耗材料，中断不退款。订单期限包含截止 tick 的实际交付。协作场景使用离线规划，模型入口暂不开放。单角色原演示仍可按以下步骤体验：

1. 选择 Local，单步一次，再阻断“东桥 ↔ 患者住处”。切换世界/角色视图，观察真实道路与角色已知信息的差别。
2. 运行并查看替代路线、观察来源、动作和预算；Full / Local 对照从相同初态接受同一次断桥干预。
3. 导出 JSON，或在“回放存档”区保存；恢复后先进入校验回放，选择游标并点击“从此处继续”，再单步或运行。源存档不变，已消耗材料和预算不会重置。

切换策略、重置或刷新页面会建立新的模拟；活动会话不跨服务重启保存。显式存档默认位于 `var/replay-archives.sqlite3`，保存 7 天，最多 64 份和 64 MiB 有效载荷。可用 `WORLDMIND_ARCHIVES` 指定持久磁盘路径。

浏览器把存档凭证保存在当前站点的 `localStorage`；清除站点数据后无法自动找回。凭证用于恢复和删除，需妥善保存。跨设备优先导出 JSON；服务器重启后的恢复取决于存档文件和凭证仍在。

**应用版本与录制协议独立。** 协作场景使用 `worldmind-0.4.0/cooperation-1`，单角色场景继续使用 `worldmind-0.2.1/river-1`。支持原 0.1、0.2、0.2.1 记录按原动作、执行和规划规则校验及续跑，不改写版本标签或哈希。详细容量、并发与兼容边界见 [实现协议](docs/IMPLEMENTATION.md)。

## CLI 与 Python SDK

只运行离线核心或使用 SDK 时不需要 Node.js 和浏览器：

```bash
.venv/bin/python -m worldmind.cli run --policy Local --output var/demo.json
.venv/bin/python -m worldmind.cli replay var/demo.json
.venv/bin/python -m worldmind.cli eval --output var/comparison.json
.venv/bin/python -m worldmind.cli run --scenario cooperation --output var/cooperation.json
.venv/bin/python -m worldmind.cli eval --scenario cooperation --output var/cooperation-pair.json
```

`run` 和 `eval` 默认使用第 1 tick 断桥场景。同步 SDK 可控制已启动的服务：

```python
from worldmind.sdk import WorldmindClient

with WorldmindClient("http://127.0.0.1:8000") as client:
    session = client.create_session({"policy": "Local"})
    print(session.step()["tick"])
    session.delete()
```

协作会话可通过 `client.create_session({"scenario": "cooperation", "policy": "Local"})` 创建，`session.actor("doctor")` 获取医生投影。原存档接口同样支持协作记录、游标续跑和独立分支；详情见 SDK 指南。

会话、存档、续跑、冲突与显式重试见 [SDK 指南](docs/SDK.md)。SDK 不自动重试或发起模型调用。

## 可选模型

支持 19 个服务配置、5 类 HTTP 协议；输出仅作为经过语法、语义校验的动作提议。配置和已验证范围见 [供应商矩阵](docs/PROVIDERS.md) 与 [.env.example](.env.example)。没有预填模型 ID，默认禁用、不自动重试或跨供应商回退；缺失用量与费用保留为未知。

启用时先初始化持久额度账本：`worldmind init-ledger`，再设置服务端模型、密钥、至少 24 字符的 `WORLDMIND_MODEL_ACCESS`，显式开启 `WORLDMIND_ENABLE_MODELS=1`。模型开启而账本缺失时拒绝启动。失败与取消不退请求额度。浏览器只输入演示访问口令，不输入供应商密钥。

`.env` 不自动加载，可导出环境变量，或运行：

```bash
.venv/bin/python -m uvicorn worldmind.api:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

使用单进程，不加 `--workers`；当前不支持多进程共享活动会话和模型并发控制。

## 开发与验证

```bash
.venv/bin/python -m pytest -q --cov=worldmind --cov-report=term-missing
npm --prefix web run format:check
npm --prefix web run build
cd web
npx playwright install chromium
npm run test:e2e
```

`build` 包含 TypeScript 检查。Playwright 启动真实后端并使用生产构建；已有 Chromium 可设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`。Windows 使用对应的 Python 可执行路径。[CI](.github/workflows/ci.yml) 检查 Windows/Linux 后端及 Linux 浏览器，实际状态见 PR Checks。

`.venv/`、`web/node_modules/` 是本地安装依赖，构建与测试缓存也不进入 Git；`var/` 保存运行数据，不应当作临时缓存删除。`docs/evidence/` 是历史验证证据，保留原文件。复现实验请输出到工作目录：

```bash
.venv/bin/python scripts/benchmark_v021.py --output var/benchmarks/v021
.venv/bin/python scripts/benchmark_review.py
```

`src/worldmind/` 包含动作、规划、引擎、会话、存档、模型网关和 SDK；`web/` 是交互界面，`tests/` 与 `web/e2e/` 保存回归测试。文档按用途集中在 [文档索引](docs/README.md)：当前接口以实现与 SDK 指南为准，历史报告和原始规划按各自日期阅读。
