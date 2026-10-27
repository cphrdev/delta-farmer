from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from clients.jumper import (
    JumperClient,
    JumperOrderUnknown,
    derive_agent,
    market_ref,
    order_params,
    read_action_result,
    sign_typed_data,
)
from lib.http import ApiError
from strategy import OrderStatus


def test_agent_is_stable_and_separate_from_the_wallet():
    client = JumperClient("test", "0x" + "11" * 32)
    again = derive_agent(client.account)

    assert client.agent.address == again.address
    assert client.agent.address != client.address


def test_market_ref_uses_native_hyperliquid_and_prefixed_categories():
    assert market_ref("BTC") == {"marketId": "BTC", "categoryId": "hyperliquid"}
    assert market_ref("xyz:TSLA") == {"marketId": "xyz:TSLA", "categoryId": "xyz"}


def test_market_order_omits_time_in_force_so_lifi_sends_a_frontend_market():
    params = order_params("BTC", "bid", Decimal("0.01"), Decimal("100"), "FrontendMarket", False)

    assert params["side"] == "BUY"
    assert params["type"] == "MARKET"
    assert "timeInForce" not in params
    assert params["size"] == "0.01"
    assert params["price"] == "100"


def test_limit_order_is_gtc_sell():
    params = order_params("ETH", "ask", Decimal("1"), Decimal("2000.5"), "Gtc", True)

    assert params["side"] == "SELL"
    assert params["type"] == "LIMIT"
    assert params["timeInForce"] == "GTC"
    assert params["reduceOnly"] is True


def test_signature_is_hex_of_the_eip712_digest():
    client = JumperClient("test", "0x" + "11" * 32)
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "AcceptTerms": [
                {"name": "action", "type": "string"},
                {"name": "acceptor", "type": "address"},
            ],
        },
        "domain": {
            "name": "LI.FI",
            "version": "1",
            "chainId": "42161",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "primaryType": "AcceptTerms",
        "message": {"action": "Accept", "acceptor": client.address},
    }

    first = sign_typed_data(client.account, typed)
    second = sign_typed_data(client.account, typed)

    assert first == second
    assert first.startswith("0x")
    assert len(bytes.fromhex(first.removeprefix("0x"))) == 65
    assert typed["domain"]["chainId"] == "42161"


def test_result_reader_accepts_acknowledgement_and_stops_on_unknown_outcome():
    assert (
        read_action_result(
            [{"action": "placeOrder", "status": "submitted", "orderId": "7"}], "placeOrder"
        )["orderId"]
        == "7"
    )

    with pytest.raises(ApiError, match="margin"):
        read_action_result(
            [{"action": "placeOrder", "status": "failed", "error": "margin"}],
            "placeOrder",
        )

    with pytest.raises(JumperOrderUnknown, match="not confirmed"):
        read_action_result(
            [{"action": "placeOrder", "status": "indeterminate", "error": "not confirmed"}],
            "placeOrder",
        )


def _response(status: int, body: dict) -> Mock:
    return Mock(ok=200 <= status < 300, status_code=status, json=Mock(return_value=body), text="")


def _client() -> JumperClient:
    client = JumperClient("test", "0x" + "22" * 32)
    client._api = Mock(request=AsyncMock())
    client.registered = AsyncMock(return_value=True)
    client._info = AsyncMock(return_value=[])
    return client


async def test_order_is_built_by_lifi_and_signed_by_the_agent(monkeypatch):
    client = _client()
    client._api.request.side_effect = [
        _response(200, {"accepted": True, "termsVersion": "3"}),
        _response(201, {"actions": []}),
        _response(201, {"actions": []}),
        _response(201, {"actions": []}),
        _response(
            201,
            {
                "actions": [
                    {
                        "action": "placeOrder",
                        "typedData": {
                            "types": {"Agent": [{"name": "source", "type": "string"}]},
                            "domain": {
                                "name": "Exchange",
                                "version": "1",
                                "chainId": 1337,
                                "verifyingContract": "0x0000000000000000000000000000000000000000",
                            },
                            "primaryType": "Agent",
                            "message": {"source": "a"},
                        },
                    }
                ]
            },
        ),
        _response(
            202, {"results": [{"action": "placeOrder", "status": "submitted", "orderId": "99"}]}
        ),
    ]
    client.get_order = AsyncMock(
        return_value=Mock(status=OrderStatus.FILLED, filled=Decimal("0.01"), size=Decimal("0.01"))
    )
    monkeypatch.setattr("clients.jumper.asyncio.sleep", AsyncMock())

    order = await client._place_order(
        "BTC", "bid", Decimal("0.01"), Decimal("100"), "FrontendMarket"
    )

    assert order.status == OrderStatus.FILLED
    create = client._api.request.await_args_list[4]
    execute = client._api.request.await_args_list[5]
    assert create.args == ("POST", "/createAction")
    assert create.kwargs["json"]["signerAddress"] == client.agent.address
    assert "x-lifi-api-key" not in client._api.request.await_args_list[0].kwargs
    assert execute.args == ("POST", "/executeAction")
    assert execute.kwargs["retry"] is False
    assert execute.kwargs["json"]["actions"][0]["signature"].startswith("0x")
    assert execute.kwargs["json"]["actions"][0]["typedData"]["message"] == {"source": "a"}


async def test_missing_terms_are_accepted_before_the_order_is_built():
    client = _client()
    client._api.request.side_effect = [
        _response(401, {"code": 2012, "message": "terms"}),
        _response(200, {"accepted": False, "termsVersion": "4"}),
        _response(
            201,
            {
                "actions": [
                    {
                        "action": "metaAcceptTerms",
                        "typedData": {
                            "types": {"AcceptTerms": [{"name": "action", "type": "string"}]},
                            "domain": {
                                "name": "LI.FI",
                                "version": "1",
                                "chainId": 42161,
                                "verifyingContract": "0x0000000000000000000000000000000000000000",
                            },
                            "primaryType": "AcceptTerms",
                            "message": {"action": "Accept"},
                        },
                    }
                ]
            },
        ),
        _response(202, {"results": [{"action": "metaAcceptTerms", "status": "accepted"}]}),
        _response(201, {"actions": []}),
    ]

    assert await client._create("hyperliquid", "approveBuilderFee", {}, user=True)


async def test_cancel_all_uses_lifi_order_ids():
    client = _client()
    client._dex_names = AsyncMock(return_value=[""])
    client._info = AsyncMock(return_value=[{"oid": 15, "coin": "BTC"}])
    client._run = AsyncMock(return_value={"status": "submitted"})
    client._exchange = AsyncMock(side_effect=AssertionError("direct Hyperliquid order"))

    assert await client.cancel_all_orders() == 1
    client._run.assert_awaited_once_with("cancelOrder", {"ids": ["15"]})


async def test_unconfirmed_market_fill_is_not_sent_again(monkeypatch):
    client = _client()
    client._ready = True
    client._api.request.side_effect = [
        _response(
            201,
            {
                "actions": [
                    {
                        "action": "placeOrder",
                        "typedData": {
                            "types": {"Agent": [{"name": "source", "type": "string"}]},
                            "domain": {
                                "name": "Exchange",
                                "version": "1",
                                "chainId": 1337,
                                "verifyingContract": "0x0000000000000000000000000000000000000000",
                            },
                            "primaryType": "Agent",
                            "message": {"source": "a"},
                        },
                    }
                ]
            },
        ),
        _response(
            202,
            {"results": [{"action": "placeOrder", "status": "indeterminate", "error": "timeout"}]},
        ),
    ]
    monkeypatch.setattr("clients.jumper.asyncio.sleep", AsyncMock())

    with pytest.raises(JumperOrderUnknown):
        await client._place_order("BTC", "ask", Decimal("0.02"), Decimal("100"), "FrontendMarket")

    assert client._api.request.await_count == 2
