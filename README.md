# Worldmind · 界行

可运行、可观察、可验证的 NPC 行动规划演示。角色在有限认知下送药；你可以阻断道路、移除药材、改变目标，查看 Full 重规划与 Local 局部修复的真实结果，并逐步校验回放。

**这是从仓库规划重新构建的 v0.1 开发版本，不是旧云端实现的恢复。** 默认不调用模型、不需要 API Key、不产生模型费用。当前交付为源码及本地演示，未部署公开 HTTPS 服务、未验证真实供应商联通。实际验证和负面结果见 [测试报告](docs/TEST_REPORT.md)。

![本轮浏览器测试中的断桥修复与对照界面](docs/evidence/desktop.png)

## 快速启动

需要 Python 3.11+、Node.js 22.12+（本轮使用 Python 3.12 / Node 24）。从仓库根目录执行。

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install --no-deps -e .
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m worldmind.cli serve
```

Windows PowerShell（无需激活脚本）：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
npm --prefix web ci
npm --prefix web run build
.\.venv\Scripts\python.exe -m worldmind.cli serve
```

打开 [本地演示](http://127.0.0.1:8000)。[健康检查](http://127.0.0.1:8000/api/health) 和 [OpenAPI 协议](http://127.0.0.1:8000/openapi.json) 可用于检查服务。先构建前端，再启动后端；单个 FastAPI 服务托管构建产物。修改前端后重新 build。开发时可另运行 `npm --prefix web run dev`，Vite 代理 `/api` 至后端。

核心离线命令不依赖 Node 或浏览器：

```bash
.venv/bin/python -m worldmind.cli run --policy Local --output var/demo.json
.venv/bin/python -m worldmind.cli replay var/demo.json
.venv/bin/python -m worldmind.cli eval --output var/comparison.json
.venv/bin/python scripts/evaluate.py
```

最后一条会重建 `docs/evidence` 的七类例子和指标。结果包含失败/未知，不能视作论文级实验。

## 五分钟体验

1. 选择 Local 和“向患者送药”，点“单步”。
2. 阻断“东桥 ↔ 患者住处”。世界视图立刻显示阻断；切换“角色已知信息”，远处道路仍显示上次知道的状态。
3. 点运行。快递员到桥边获得新观察后，真实搜索替代路线；制作已经消耗的药材不会回滚。
4. 查看计划恢复、认知来源、动作时间线、展开/校验预算。暂停后可继续单步；制作时取消不会退材料。
5. 运行 Full / Local 对照。两者从相同初态开始，tick 1 阻断同一道路，共用动作模型、搜索器和总预算口径。
6. 导出 JSON；导入时完整重执行校验，随后可从起点逐步验证。损坏文件被拒绝，当前会话不被覆盖。

缺材例子：重置后移除药圃和仓库材料，再运行，角色在观察确认后报告当前认知下未找到计划。不要把 unknown 解读为真实世界永久无解。

切换规划策略会创建新会话；重置会丢弃当前未导出的轨迹。所有会话在服务重启后丢失。回放为只读，重置后才可继续新模拟。单会话最长 500 tick、1500 条输入；服务最多 16 会话，1 小时不活动到期。默认界面仅展示最近 150 条事件，导出保留完整有效输入。

## 可选模型

已实现 19 个配置、5 类协议：Chat Completions、Responses、Messages、Gemini generateContent、Ollama 原生。覆盖 OpenAI、DeepSeek、MiMo、Gemini、Anthropic、方舟、Qwen、Kimi、智谱、聚合平台和本地服务。每个配置的来源、鉴权、参数及限制见 [供应商能力矩阵](docs/PROVIDERS.md)。

没有预填或猜测模型 ID；没有真实凭据时只做协议模拟。JSON/思考模式不通用；模型输出只能成为经过严格校验的动作提议。默认不重试、不跨供应商回退，缺失用量/费用为未知。SQLite 持久账本在请求前占用额度，失败和取消不退款，重启不重置。

配置示例见 [.env.example](.env.example)。**本轮未启用真实调用；后续真实调用需要另行授权。** 获得授权后在持久磁盘执行 `worldmind init-ledger`，配置服务端模型与密钥、至少 24 字符的演示访问口令，再显式设 `WORLDMIND_ENABLE_MODELS=1`。模型开启而账本缺失时服务拒绝启动。`.env` 不自动加载；可导出变量，或使用：

```bash
.venv/bin/python -m uvicorn worldmind.api:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

浏览器只输入项目演示口令，不输入供应商密钥。服务严格使用单进程，不加 `--workers`；多实例额度恢复与并发服务不在 v0.1 支持范围。

## 验证

```bash
.venv/bin/python -m pytest -q --cov=worldmind --cov-report=term-missing
npm --prefix web run typecheck
npm --prefix web run build
cd web
npx playwright install chromium
npm run test:e2e
```

Playwright 启动实际后端并使用生产构建，不替换业务 API。已有 Chromium 时可设置 `PLAYWRIGHT_CHROMIUM_EXECUTABLE`；云环境本轮使用 `/usr/bin/chromium`。受限沙箱可能需要开放本机线程事件循环/HTTP 所需的网络权限。Windows 对应替换 Python 可执行路径。

[CI](.github/workflows/ci.yml) 配置 Windows/Linux 后端和 Linux 浏览器检查；远程运行状态以 GitHub Checks 为准，配置文件存在不等于运行通过。

## 代码与设计

- `src/worldmind/domain.py`：有限动作模型和输入类型。
- `planner.py`：共享 BFS、连续窗口替换、完整剩余计划校验和预算。
- `engine.py`：权威世界、角色观察、动作执行、事件、导出与回放。
- `providers.py` / `models.py`：协议差异、严格提议校验、持久额度/缓存/审计。
- `api.py`：会话能力令牌、单写者、幂等命令、后台运行、异步模型代次。
- `web`：TypeScript/Vite 交互工作台与 Playwright 测试。

[当前实现与限制](docs/IMPLEMENTATION.md) · [测试报告](docs/TEST_REPORT.md) · [进度与恢复步骤](docs/PROGRESS.md)

原始构思完整保留：[汇编](Worldmind-完整项目构思与规划.md)、[首版计划](docs/plan/07-两周上线计划.md)、[长期计划](docs/plan/01-项目总规划.md)。旧文档中的未来能力和研究假设不是当前已实现清单。
