# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | No AI was harmed making this
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from clients.omni import (
    IndicativeQuote,
    OmniClient,
    OmniSupportedAsset,
    OmniTradingSchedule,
    OmniTradingSession,
    TransientGatewayError,
    _is_cloudflare_challenge,
)
from lib.http import ApiError, AsyncHttp
from strategy import Position


def _signing_data(message="omni.variational.io wants you to sign in", nonce="nonce-1"):
    return Mock(ok=True, json=Mock(return_value={"message": message, "nonce": nonce}))


@pytest.fixture
def client():
    value = object.__new__(OmniClient)
    value.name = "test"
    value.address = "0x123"
    value.account = Mock()
    value.account.sign_message.return_value.signature = b"sig"
    value.http = Mock()
    value.http.session.cookies = {}
    value._login_error = None
    value._login_error_until = 0.0
    value._prepare_login = AsyncMock()
    value._request = AsyncMock()
    return value


async def test_login_reuses_existing_session(client):
    client.http.session.cookies["vr-token"] = "saved"
    client._request.return_value = Mock(ok=True, status_code=200)

    await client.login()

    client._prepare_login.assert_not_awaited()
    client._request.assert_awaited_once_with("GET", "/portfolio?compute_margin=true")
    client.http.clear_cookies.assert_not_called()


async def test_login_reuses_persisted_session(client, tmp_path):
    cookies_file = str(tmp_path / "cookies.pkl")
    saved_http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    saved_http.session.cookies.set("vr-token", "saved", domain="example.com")
    await saved_http.close()

    client.http = AsyncHttp(baseurl="https://example.com", headers={}, cookies_file=cookies_file)
    client.http.clear_cookies = Mock(wraps=client.http.clear_cookies)
    client._request.return_value = Mock(ok=True, status_code=200)

    try:
        assert not client.http._cookies_loaded
        await client.login()

        assert client.http.session.cookies.get("vr-token") == "saved"
        client.http.clear_cookies.assert_not_called()
        client._prepare_login.assert_not_awaited()
        client._request.assert_awaited_once_with("GET", "/portfolio?compute_margin=true")
    finally:
        await client.http.close()


@pytest.mark.parametrize("force", [False, True])
async def test_login_authenticates_missing_or_forced_session(client, force):
    client.http.session.cookies["vr-token"] = "old"
    client.http.session.cookies["cf_clearance"] = "clearance"
    client.http.clear_cookies.side_effect = client.http.session.cookies.clear

    async def request(_method, path, **_kwargs):
        if path == "/auth/v2/generate_signing_data":
            return _signing_data()
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True)

    client._request.side_effect = request
    if not force:
        client.http.session.cookies.pop("vr-token")

    await client.login(force=force)

    assert client.http.session.cookies["vr-token"] == "new"
    assert client.http.clear_cookies.call_count == int(force)
    assert ("cf_clearance" in client.http.session.cookies) is not force


async def test_login_reauthenticates_rejected_session(client):
    client.http.session.cookies["vr-token"] = "old"
    client.http.session.cookies["cf_clearance"] = "clearance"
    client.http.clear_cookies.side_effect = client.http.session.cookies.clear

    async def request(_method, path, **_kwargs):
        if path == "/portfolio?compute_margin=true":
            return Mock(ok=False, status_code=401)
        if path == "/auth/v2/generate_signing_data":
            return _signing_data()
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True)

    client._request.side_effect = request

    await client.login()

    assert client.http.session.cookies["vr-token"] == "new"
    assert client.http.session.cookies["cf_clearance"] == "clearance"
    client.http.clear_cookies.assert_not_called()
    client._prepare_login.assert_awaited_once_with()


async def test_login_does_not_clear_session_on_server_error(client):
    client.http.session.cookies["vr-token"] = "saved"
    client._request.return_value = Mock(ok=False, status_code=500, text="Unavailable")

    with pytest.raises(ApiError, match="Auth check failed"):
        await client.login()

    assert client.http.session.cookies["vr-token"] == "saved"
    client.http.clear_cookies.assert_not_called()
    client._prepare_login.assert_not_awaited()


async def test_login_preparation_does_not_retry_solver_error(client):
    client._request.side_effect = RuntimeError("ACTIVE_TASKS_LIMIT_EXCEEDED")

    with pytest.raises(RuntimeError, match="ACTIVE_TASKS_LIMIT_EXCEEDED"):
        await OmniClient._prepare_login(client)

    client._request.assert_awaited_once()


async def test_login_reports_cloudflare_rate_limit(client):
    async def request(_method, path, **_kwargs):
        if path == "/auth/v2/generate_signing_data":
            return _signing_data()
        return Mock(ok=False, status_code=429, text="<html>rate limited</html>")

    client._request.side_effect = request

    with pytest.raises(ApiError, match="Login blocked by Cloudflare \\(429\\)"):
        await client.login()


async def test_login_does_not_rerun_after_failure(client):
    client._request.side_effect = ApiError("Login failed")

    with pytest.raises(ApiError, match="Login failed"):
        await client.login()
    with pytest.raises(ApiError, match="Login failed"):
        await client.login()

    assert client._prepare_login.await_count == 1
    assert [call.args[1] for call in client._request.await_args_list] == [
        "/auth/v2/generate_signing_data"
    ]


def _gateway_page(status: int = 502):
    return Mock(ok=False, status_code=status, text="<!DOCTYPE html><html>bad gateway</html>")


async def test_login_retries_gateway_error_and_does_not_cache_it(client, monkeypatch):
    monkeypatch.setattr("lib.decorators.asyncio.sleep", AsyncMock())
    calls = {"n": 0}

    async def request(_method, path, **_kwargs):
        if path == "/auth/v2/generate_signing_data":
            calls["n"] += 1
            if calls["n"] == 1:
                return _gateway_page()
            return _signing_data()
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True, status_code=200, text="ok")

    client._request.side_effect = request

    await client.login()

    assert calls["n"] == 2
    assert client._login_error is None


async def test_gateway_login_failure_is_retried_on_the_next_call(client, monkeypatch):
    monkeypatch.setattr("lib.decorators.asyncio.sleep", AsyncMock())
    client._request.return_value = _gateway_page()

    with pytest.raises(TransientGatewayError, match=r"Signing data request failed: 502 \[html\]"):
        await client.login()
    with pytest.raises(TransientGatewayError, match=r"Signing data request failed: 502 \[html\]"):
        await client.login()

    assert client._prepare_login.await_count == 6
    assert client._login_error is None


async def test_login_preparation_reports_gateway_status(client):
    client._prepare_login = OmniClient._prepare_login.__get__(client, OmniClient)
    client._request.return_value = _gateway_page()

    with pytest.raises(TransientGatewayError, match=r"Login preparation failed: 502 \[html\]"):
        await client._prepare_login()


async def test_sticky_login_error_expires(client, monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr("clients.omni.time.monotonic", lambda: now["t"])
    client._request.side_effect = ApiError("Login failed")

    with pytest.raises(ApiError, match="Login failed"):
        await client.login()
    client._prepare_login.reset_mock()

    with pytest.raises(ApiError, match="Login failed"):
        await client.login()
    client._prepare_login.assert_not_awaited()

    now["t"] += 61

    async def request(_method, path, **_kwargs):
        if path == "/auth/v2/generate_signing_data":
            return _signing_data()
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True, status_code=200, text="ok")

    client._request.side_effect = request
    await client.login()

    client._prepare_login.assert_awaited_once()


async def test_login_restarts_with_fresh_signing_data_after_rejected_login(client):
    paths = []
    signing_data = iter(
        [
            "omni.variational.io wants you to sign in: first",
            "omni.variational.io wants you to sign in: second",
        ]
    )

    async def request(_method, path, **_kwargs):
        paths.append(path)
        if path == "/auth/v2/generate_signing_data":
            return _signing_data(next(signing_data), nonce=f"nonce-{paths.count(path)}")
        if paths.count("/auth/login") == 1:
            return Mock(ok=False, status_code=400, text="signature expired")
        client.http.session.cookies["vr-token"] = "new"
        return Mock(ok=True, status_code=200)

    client._request.side_effect = request

    await client.login()

    assert paths == [
        "/auth/v2/generate_signing_data",
        "/auth/login",
        "/auth/v2/generate_signing_data",
        "/auth/login",
    ]
    assert client.account.sign_message.call_count == 2
    login = client._request.await_args_list[1]
    assert login.kwargs["json"]["nonce"] == "nonce-1"
    assert login.kwargs["json"]["signed_message"] == "736967"


def test_400_cloudflare_html_is_detected_as_challenge():
    rep = Mock(ok=False, status_code=400, text=">Just a moment...< /cdn-cgi/challenge-platform/")
    assert _is_cloudflare_challenge(rep)


async def test_login_post_uses_managed_solver_for_jsd_html(monkeypatch):
    client = object.__new__(OmniClient)
    client.name = "test"
    client.proxy = "http://user:pass@proxy.example:8080"
    client.http = Mock()
    jsd = Mock(
        ok=False,
        status_code=403,
        text=">Just a moment...< /cdn-cgi/challenge-platform/scripts/jsd/",
    )
    success = Mock(ok=True, status_code=200, text="ok")
    client.http.request = AsyncMock(side_effect=[jsd, success])
    solve = AsyncMock()
    jsd_solve = AsyncMock()
    monkeypatch.setattr("clients.omni.solve_managed_cf_clearance", solve)
    monkeypatch.setattr("clients.omni.ensure_cf_clearance", jsd_solve)

    rep = await OmniClient._request(client, "POST", "/auth/login")

    assert rep is success
    solve.assert_awaited_once()
    jsd_solve.assert_not_awaited()


async def test_request_does_not_overwrite_managed_clearance_with_jsd(monkeypatch):
    client = object.__new__(OmniClient)
    client.name = "test"
    client.proxy = "http://user:pass@proxy.example:8080"
    client.http = Mock()
    managed = Mock(
        ok=False,
        status_code=403,
        text=">Just a moment...< cType: 'managed' /cdn-cgi/challenge-platform/",
    )
    jsd = Mock(
        ok=False,
        status_code=403,
        text=">Just a moment...< /cdn-cgi/challenge-platform/scripts/jsd/",
    )
    success = Mock(ok=True, status_code=200, text="ok")
    client.http.request = AsyncMock(side_effect=[managed, jsd, success])
    solve = AsyncMock()
    jsd_solve = AsyncMock()
    monkeypatch.setattr("clients.omni.solve_managed_cf_clearance", solve)
    monkeypatch.setattr("clients.omni.ensure_cf_clearance", jsd_solve)

    rep = await OmniClient._request(client, "POST", "/auth/login")

    assert rep is success
    assert solve.await_count == 2
    jsd_solve.assert_not_awaited()


async def test_request_replays_login_after_managed_challenge(monkeypatch):
    client = object.__new__(OmniClient)
    client.name = "test"
    client.proxy = "http://user:pass@proxy.example:8080"
    client.http = Mock()
    challenge = Mock(
        ok=False,
        status_code=400,
        text=">Just a moment...< cType: 'managed' /cdn-cgi/challenge-platform/",
    )
    success = Mock(ok=True, status_code=200, text="ok")
    client.http.request = AsyncMock(side_effect=[challenge, success])
    solve = AsyncMock()
    monkeypatch.setattr("clients.omni.solve_managed_cf_clearance", solve)

    rep = await OmniClient._request(client, "POST", "/auth/login")

    assert rep is success
    solve.assert_awaited_once()
    assert client.http.request.await_count == 2


async def test_call_relogs_only_once(client):
    client.http.session.cookies["vr-token"] = "old"
    unauthorized = Mock(ok=False, status_code=401, text="Unauthorized")
    client._request.side_effect = [unauthorized, unauthorized]

    async def relogin():
        assert "vr-token" not in client.http.session.cookies
        client.http.session.cookies["vr-token"] = "new"

    client.login = AsyncMock(side_effect=relogin)

    with pytest.raises(ApiError, match="API error: 401 Unauthorized"):
        await client._call("GET", "/portfolio")

    client.login.assert_awaited_once_with()
    assert client._request.await_count == 2


async def test_close_position_skips_closed_market(client):
    position = Position(
        id="XAUS",
        symbol="XAUS",
        side="bid",
        size=Decimal("1"),
        entry_price=Decimal("4000"),
    )
    client._quote = AsyncMock(
        side_effect=ApiError(
            'API error: 422 {"error_code":"market_status_refused",'
            '"error_params":{"status":"Closed","symbol":"XAUS"}}'
        )
    )

    assert await client.close_position(position) is False


async def test_close_position_does_not_hide_other_api_errors(client):
    position = Position(
        id="BTC",
        symbol="BTC",
        side="bid",
        size=Decimal("0.01"),
        entry_price=Decimal("100000"),
    )
    client._quote = AsyncMock(side_effect=ApiError("API error: 500 unavailable"))

    with pytest.raises(ApiError, match="500 unavailable"):
        await client.close_position(position)


@pytest.mark.parametrize(
    ("asset", "expected"),
    [
        (
            OmniSupportedAsset(
                asset="BTC",
                has_perp=True,
                instrument_type="perpetual_future",
                funding_interval_s=28800,
            ),
            {
                "underlying": "BTC",
                "settlement_asset": "USDC",
                "instrument_type": "perpetual_future",
                "funding_interval_s": 3600,
            },
        ),
        (
            OmniSupportedAsset(
                asset="ANTHROPIC",
                has_perp=True,
                instrument_type="perpetual_rwa_future",
                asset_class="equity",
            ),
            {
                "underlying": "ANTHROPIC",
                "settlement_asset": "USDC",
                "instrument_type": "perpetual_rwa_future",
                "kind": "equity",
            },
        ),
        (
            OmniSupportedAsset(
                asset="XAUS", has_perp=True, instrument_type="swap", asset_class="commodity"
            ),
            {
                "underlying": "XAUS",
                "settlement_asset": "USDC",
                "instrument_type": "swap",
                "funding_interval_s": 0,
                "kind": "commodity",
            },
        ),
    ],
)
async def test_instrument_uses_supported_asset_metadata(client, asset, expected):
    client.supported_assets = AsyncMock(return_value={asset.asset: asset})

    assert await client._instrument(asset.asset) == expected


def test_indicative_quote_reads_qty_tick_from_ask_limits():
    quote = IndicativeQuote.model_validate(
        {
            "quote_id": "q1",
            "mark_price": "100",
            "index_price": "100",
            "bid": "99",
            "ask": "101",
            "qty": "1",
            "qty_limits": {"ask": {"min_qty_tick": "0.01"}},
        }
    )
    assert quote.qty_tick == Decimal("0.01")


async def test_supported_assets_skips_invalid_variants(client):
    client._call = AsyncMock(
        return_value={
            "BAD": [{"asset": "BAD", "trading_sessions": [{"open": "nope", "close": "nope"}]}],
            "XAUS": [{"asset": "XAUS", "has_perp": True, "instrument_type": "swap"}],
        }
    )

    assets = await client.supported_assets()

    assert list(assets) == ["XAUS"]
    assert assets["XAUS"].has_perp


async def test_swap_symbols_are_listed_without_quoting(client):
    client.supported_assets = AsyncMock(
        return_value={
            "XAUS": OmniSupportedAsset(asset="XAUS", has_perp=True, instrument_type="swap"),
            "US100S": OmniSupportedAsset(asset="US100S", has_perp=True, instrument_type="swap"),
        }
    )

    assert await client.get_symbols() == ["XAUS", "US100S"]


async def test_is_symbol_tradeable_uses_swap_sessions(client):
    session = OmniTradingSession(
        open=datetime(2026, 10, 18, 22, tzinfo=UTC),
        close=datetime(2026, 10, 19, 21, tzinfo=UTC),
    )
    client.supported_assets = AsyncMock(
        return_value={
            "XAUS": OmniSupportedAsset(
                asset="XAUS",
                has_perp=True,
                instrument_type="swap",
                market_status="open",
                trading_sessions=[session],
                trading_schedule=OmniTradingSchedule(
                    next_open_at=datetime(2026, 10, 19, 22, tzinfo=UTC),
                    next_close_at=datetime(2026, 10, 19, 21, tzinfo=UTC),
                ),
            )
        }
    )

    assert await client.is_symbol_tradeable("XAUS", datetime(2026, 10, 18, 22, 42, tzinfo=UTC))
    assert not await client.is_symbol_tradeable("XAUS", datetime(2026, 10, 18, 21, tzinfo=UTC))


async def test_instrument_defaults_unknown_symbol_to_crypto_perp(client):
    client.supported_assets = AsyncMock(return_value={})

    assert await client._instrument("BTC") == {
        "underlying": "BTC",
        "settlement_asset": "USDC",
        "instrument_type": "perpetual_future",
        "funding_interval_s": 3600,
    }


async def test_concurrent_unauthorized_calls_share_one_authentication(client):
    client.http.session.cookies["vr-token"] = "old"
    unauthorized = Mock(ok=False, status_code=401, text="Unauthorized")
    success = Mock(ok=True, status_code=200)
    success.json.return_value = {"balance": "1"}
    both_requests_started = asyncio.Event()
    portfolio_requests = 0
    login_requests = 0

    async def request(_method, path, **_kwargs):
        nonlocal portfolio_requests, login_requests
        if path == "/portfolio":
            portfolio_requests += 1
            if portfolio_requests <= 2:
                if portfolio_requests == 2:
                    both_requests_started.set()
                await both_requests_started.wait()
                return unauthorized
            return success
        if path == "/auth/v2/generate_signing_data":
            login_requests += 1
            return _signing_data()
        if path == "/auth/login":
            login_requests += 1
            client.http.session.cookies["vr-token"] = "new"
            return Mock(ok=True)
        raise AssertionError(path)

    client._request.side_effect = request
    client._check_auth = AsyncMock(side_effect=lambda: "vr-token" in client.http.session.cookies)
    client.http.clear_cookies.side_effect = client.http.session.cookies.clear

    results = await asyncio.gather(
        client._call("GET", "/portfolio"),
        client._call("GET", "/portfolio"),
    )

    assert results == [{"balance": "1"}, {"balance": "1"}]
    assert login_requests == 2
    assert portfolio_requests == 4
    assert client._check_auth.await_count == 2


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"own_volume": None, "trade_volume": None, "referred_by": None},
    ],
)
async def test_total_volume_handles_missing_referral_summary(client, response):
    client._call = AsyncMock(return_value=response)

    assert await client.total_volume() == (Decimal(0), None)
    client._call.assert_awaited_once_with("GET", "/referrals/summary")


async def test_total_volume_parses_referral_summary(client):
    client._call = AsyncMock(
        return_value={"own_volume": {"total": "123.45"}, "referred_by": {"code": "REF"}}
    )

    assert await client.total_volume() == (Decimal("123.45"), "REF")
