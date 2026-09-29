from types import SimpleNamespace

import pytest

from lib import unwaf_cf


class _Response:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class _Session:
    responses = []
    calls = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json):
        self.calls.append((url, json))
        return _Response(self.responses.pop(0))


class _Cookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, *, domain, path):
        self.values[(domain, path, name)] = value


@pytest.mark.asyncio
async def test_managed_clearance_retries_active_task_limit(monkeypatch):
    _Session.calls = []
    _Session.responses = [
        {
            "errorId": 1,
            "errorCode": "ACTIVE_TASKS_LIMIT_EXCEEDED",
            "errorDescription": "busy",
        },
        {"errorId": 0, "taskId": "task-1"},
        {"status": "closed", "solution": {"token": "clearance-token"}},
    ]
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(unwaf_cf, "AsyncSession", _Session)
    monkeypatch.setattr(unwaf_cf.asyncio, "sleep", no_sleep)

    cookies = _Cookies()
    http = SimpleNamespace(session=SimpleNamespace(cookies=cookies))
    await unwaf_cf.solve_managed_cf_clearance(
        http,
        "https://omni.variational.io/api/auth/login",
        proxy="http://user:pass@proxy.example:8080",
    )

    assert sleeps == [unwaf_cf._TASK_CREATE_DELAY, 3]
    assert len(_Session.calls) == 3
    assert cookies.values[
        ("omni.variational.io", "/", "cf_clearance")
    ] == "clearance-token"
