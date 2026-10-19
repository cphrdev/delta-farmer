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

    def set(self, name, value, *, domain, path, secure=False):
        self.values[(domain, path, name)] = value

    def get(self, name, default=None, **kwargs):
        for (_domain, _path, key), value in self.values.items():
            if key == name:
                return value
        return default


def _http():
    cookies = _Cookies()
    headers = {}
    return SimpleNamespace(session=SimpleNamespace(cookies=cookies, headers=headers)), cookies, headers


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

    http, cookies, _headers = _http()
    await unwaf_cf.solve_managed_cf_clearance(
        http,
        "https://omni.variational.io/api/auth/login",
        proxy="http://user:pass@proxy.example:8080",
    )

    assert sleeps == [unwaf_cf._TASK_CREATE_DELAY, 3]
    assert len(_Session.calls) == 3
    assert _Session.calls[1][1]["task"]["websiteURL"] == "https://omni.variational.io"
    assert cookies.values[("omni.variational.io", "/", "cf_clearance")] == "clearance-token"


@pytest.mark.asyncio
async def test_managed_clearance_applies_solver_cookies_and_user_agent(monkeypatch):
    _Session.calls = []
    _Session.responses = [
        {"errorId": 0, "taskId": "task-2"},
        {
            "status": "closed",
            "solution": {
                "cookies": {"cf_clearance": "cf_clearance=from-cookies", "cf_bm": "bm"},
                "userAgent": "Mozilla/5.0 solver",
            },
        },
    ]

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(unwaf_cf, "AsyncSession", _Session)
    monkeypatch.setattr(unwaf_cf.asyncio, "sleep", no_sleep)

    http, cookies, headers = _http()
    await unwaf_cf.solve_managed_cf_clearance(
        http,
        "https://omni.variational.io/api/auth/login",
        proxy="http://user:pass@proxy.example:8080",
    )

    assert cookies.values[("omni.variational.io", "/", "cf_clearance")] == "from-cookies"
    assert cookies.values[("omni.variational.io", "/", "cf_bm")] == "bm"
    assert headers["User-Agent"] == "Mozilla/5.0 solver"
