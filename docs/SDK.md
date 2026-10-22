# Python SDK（0.2）

`worldmind.sdk` 是本地 Worldmind HTTP 服务的同步客户端，不负责启动服务或调用模型。先按 README 安装包、构建页面并启动 FastAPI；SDK 使用同一个 Python 环境，无需增加第三方依赖。

```bash
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m worldmind.cli serve
```

Windows 将 Python 路径替换为 `.\.venv\Scripts\python.exe`。从 0.1 更新源码后重新执行 editable 安装，以刷新包版本元数据。

## 完成任务并保存回放

在另一个 Python 进程执行：

```python
import json
from pathlib import Path

from worldmind.domain import Config
from worldmind.sdk import WorldmindClient

with WorldmindClient("http://127.0.0.1:8000", timeout=10) as client:
    session = client.create_session(Config(policy="Local", goal="medicine_home"))
    session.step()
    session.command("block", "bridge|home")
    for _ in range(120):
        view = session.step()
        if view["status"] not in ("ready", "running"):
            break
    print(view["status"], view["tick"], view["metrics"]["budget_used"])

    recording = session.export()
    destination = Path("var/sdk-recording.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(recording, ensure_ascii=False), encoding="utf-8")

    receipt = session.save_archive("SDK 断桥送药")
    another = client.create_session()
    restored = another.restore_archive(receipt)
    assert restored["mode"] == "verified_replay"
    another.seek(0)
    another.seek(1)
    another.delete()
    session.delete()
```

`receipt` 是 `ArchiveReceipt`，包含 ID、访问凭证、名称、协议、创建/到期时间、字节数和输入步数。凭证不显示在 repr，但它仍是秘密：持有者可恢复或删除该存档。SDK 不自动保存回执；跨进程恢复须由调用者把完整回执保存在私有存储中。服务器不提供凭证找回。上例导出的普通回放 JSON 不包含凭证，适合手动导入与备份。

删除活动会话不删除其存档。确定不再需要存档时，可调用 `client.delete_archive(receipt)`。存档默认 7 天到期；服务进程重启不影响尚在持久磁盘上的有效存档。恢复是只读校验回放，不是从中间步骤续跑；调用 `control("reset")` 才会开始当前版本的新模拟。

## 接口

| 对象 / 方法 | 行为 |
|---|---|
| `WorldmindClient(base_url, timeout=10)` | 建立客户端；服务 URL 不可含用户名、密码、查询或 fragment |
| `client.health()` | 读取应用版本、录制版本和模型开关 |
| `client.create_session(config=None)` | 建立新的内存会话，接受 `Config` 或配置字典 |
| `client.attach_session(id, token)` | 绑定已有凭证；不保证会话仍然存在 |
| `session.view()` / `session.actor()` | 调试全景 / 角色受限投影；规划应使用后者 |
| `session.command(kind, target=None, ...)` | 提交一步命令，例如 `block`、`remove`、`goal`、`cancel` |
| `session.step()` | 提交一次单步，不自动循环 |
| `session.control(operation)` | `run`、`pause` 或 `reset` |
| `session.export()` | 返回完整回放字典；不写文件 |
| `session.import_recording(recording)` | 完整校验成功后进入只读回放 |
| `session.seek(until)` | 重新校验到指定输入位置，0 表示初态 |
| `session.save_archive(name)` | 验证并持久保存，返回私密回执 |
| `session.restore_archive(receipt)` | 使用独立存档凭证恢复只读回放 |
| `client.delete_archive(receipt)` | 删除对应服务端存档 |
| `session.delete()` | 删除活动会话，不删除存档 |

句柄使用能力凭证，不是账号。服务最多保留 16 个活动会话、每分钟最多创建 30 个，1 小时不活动到期。完成独立脚本后应删除不用的会话。客户端 context manager 只关闭自己的 HTTP 客户端，不自动删除会话或存档。

## 冲突与显式重试

`command()` 未提供 `expected_revision` 时，会先读取当前 revision，再提交一次。并发操作仍可能发生在这两步之间；HTTP 409 通过 `SDKError` 返回，SDK 不会自动用新 revision 重试。

当网络异常导致一次写入结果不确定时，只有保留原请求的 `command_id` 和 `expected_revision` 才能显式重试，利用服务端幂等回执：

```python
from uuid import uuid4
from worldmind.sdk import SDKError

revision = session.view()["revision"]
command_id = uuid4().hex
try:
    result = session.command("step", command_id=command_id, expected_revision=revision)
except SDKError as error:
    print(error.status, error.detail)  # 不打印凭证或请求头
    # 由调用者判断是否重试。若重试，原样保留两个参数：
    # result = session.command("step", command_id=command_id,
    #                          expected_revision=revision)
```

同 ID 同内容返回原回执；同 ID 异内容拒绝。409 可能表示 revision 冲突、只读回放或其他状态限制，不能一概当作网络失败。控制、导入和存档操作没有命令幂等 ID，不能照搬上述重试方式。

## 超时、错误与测试客户端

`timeout` 必须为 `(0, 60]` 内的有限数值，默认 10 秒，采用 httpx 的连接/读写超时语义；不是整个远程任务的绝对执行期限。SDK 没有自动重试、重定向或模型调用入口。默认使用 httpx 的代理与 CA 环境配置，不替换注入凭据或关闭 TLS 校验。

`SDKError.status` 为 HTTP 状态码；请求尚未获得响应时为 `None`。`detail` 是有界错误说明，服务端校验错误不回显输入，SDK 也不把原始网络异常附在错误文本中。网络中断不代表服务端一定没有执行操作。

测试或嵌入场景可注入专用于该 Worldmind 服务的 `httpx.Client` 或 FastAPI `TestClient`：

```python
from fastapi.testclient import TestClient
from worldmind.api import create_app
from worldmind.sdk import WorldmindClient

app = create_app(ledger_path="var/test-ledger.sqlite3",
                 archive_path="var/test-archives.sqlite3")
with TestClient(app) as transport:
    with WorldmindClient(client=transport) as client:
        session = client.create_session()
        assert session.step()["tick"] == 1
        session.delete()
```

注入的客户端由调用者关闭。它必须连接预期的服务来源，不应共享含其他账号认证的客户端。真实服务默认关闭模型调用；本轮 SDK 测试不调用供应商 API。当前测试中 Starlette 对 TestClient 的 timeout 参数有弃用提示，详见本轮测试报告。
