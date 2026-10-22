import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from worldmind.api import computation, create_app
from .test_api import new


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancel_holds_writer_until_thread_finishes(worker_fails):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    lock = asyncio.Lock()

    def work():
        entered.set()
        assert release.wait(5), "worker was not released"
        completed.set()
        if worker_fails:
            raise ValueError("worker failure after cancellation")

    async def mutate():
        async with lock:
            await computation(work)

    task = asyncio.create_task(mutate())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert lock.locked()
            assert not completed.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert completed.is_set() and not lock.locked()


def test_non_ascii_capability_headers_are_rejections_not_server_errors(tmp_path):
    app = create_app(ledger_path=tmp_path / "ledger.db", archive_path=tmp_path / "archive.db",
                     model_access="test-model-access")
    with TestClient(app) as client:
        path, _, _ = new(client)
        assert client.get(path, headers=[(b"x-session-token", b"\xff")]).status_code == 404
        assert client.get("/api/model/audit", headers=[(b"x-model-access", b"\xff")]).status_code == 403
