# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Built by humans, blamed on AI
import asyncio
import time
from decimal import Decimal
from typing import Any, Literal

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak

from lib.decorators import bind_log_context, locked, ttl_cache
from lib.errors import AppError
from lib.http import ApiError, AsyncHttp
from lib.logger import logger
from strategy import Order, OrderStatus, Side, TradingClient

from .hyperliquid import HyperLiquidClient, _fmt

PERPS_API = "https://li.quest/v1/perps"
PROVIDER = "hyperliquid"
META = "meta"
TERMS_NOT_ACCEPTED = 2012
SETUP_REQUIRED = 2070
MARKET_FILL_TIMEOUT = 5.0
MARKET_FILL_POLL = 0.25
_AGENT_DOMAIN = b"jumper-hyperliquid-agent-v1"
_OK = frozenset({"submitted", "accepted"})


class JumperOrderUnknown(AppError):
    """LI.FI accepted the action, but the venue outcome is not known yet."""


def derive_agent(account: LocalAccount) -> LocalAccount:
    """Stable Hyperliquid agent for this wallet. The key never leaves the process."""
    digest = keccak(bytes(account.key) + _AGENT_DOMAIN)
    return Account.from_key(digest)


def market_ref(symbol: str) -> dict[str, str]:
    """Map a Hyperliquid coin to the market LI.FI expects."""
    if ":" in symbol:
        category, _name = symbol.split(":", 1)
        return {"marketId": symbol, "categoryId": category}
    return {"marketId": symbol, "categoryId": "hyperliquid"}


def order_params(
    symbol: str,
    side: Side,
    qty: Decimal,
    price: Decimal,
    tif: str,
    reduce_only: bool,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "market": market_ref(symbol),
        "side": "BUY" if side == "bid" else "SELL",
        "type": "MARKET" if tif == "FrontendMarket" else "LIMIT",
        "size": _fmt(qty),
        "price": _fmt(price),
        "reduceOnly": reduce_only,
    }
    if tif != "FrontendMarket":
        params["timeInForce"] = {"Gtc": "GTC", "Ioc": "IOC", "Alo": "POST_ONLY"}.get(tif, "GTC")
    return params


def sign_typed_data(account: LocalAccount, typed_data: dict) -> str:
    """Sign LI.FI EIP-712 typed data. The original object is returned to the API unchanged."""
    types = typed_data.get("types")
    if not isinstance(types, dict) or "primaryType" not in typed_data:
        raise ApiError("Jumper typed data is missing EIP-712 fields")
    payload = {
        "types": {key: value for key, value in types.items() if key != "EIP712Domain"},
        "domain": dict(typed_data.get("domain") or {}),
        "primaryType": typed_data["primaryType"],
        "message": typed_data.get("message") or {},
    }
    chain_id = payload["domain"].get("chainId")
    if isinstance(chain_id, str) and chain_id.isdigit():
        payload["domain"]["chainId"] = int(chain_id)
    signature = account.sign_message(encode_typed_data(full_message=payload)).signature
    return "0x" + bytes(signature).hex()


def action_error_code(body: Any) -> int | None:
    if not isinstance(body, dict) or "code" not in body:
        return None
    try:
        return int(body["code"])
    except (TypeError, ValueError):
        return None


def read_action_result(results: Any, action: str) -> dict:
    """Return the venue acknowledgement, or raise when the outcome is a refusal or unknown."""
    if not isinstance(results, list):
        raise JumperOrderUnknown(f"Jumper {action} returned no result")
    matches = [item for item in results if isinstance(item, dict) and item.get("action") == action]
    if not matches:
        raise JumperOrderUnknown(f"Jumper {action} was not confirmed")
    result = matches[-1]
    status = result.get("status")
    if status in _OK:
        return result
    detail = result.get("error") or f"Jumper {action} {status}"
    if status == "indeterminate":
        raise JumperOrderUnknown(str(detail))
    raise ApiError(str(detail))


@bind_log_context
class JumperClient(HyperLiquidClient):
    """Hyperliquid orders routed through LI.FI so Jumper records the activity.

    Market data, balances, positions, and order status stay on Hyperliquid.
    Every order, cancel, and leverage change is built by https://li.quest/v1/perps
    with no API key, which selects LI.FI's default builder fee.
    """

    exchange = "jumper"
    dex_prefix = ""

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return JumperClient

    def __init__(self, name: str, privkey: str, proxy: str | None = None):
        super().__init__(name, privkey, proxy)
        self.agent = derive_agent(self.account)
        self._api = AsyncHttp(
            baseurl=PERPS_API,
            headers={"Content-Type": "application/json"},
            proxy=proxy,
        )
        self._ready = False

    async def close(self) -> None:
        await asyncio.gather(self.http.close(), self._api.close())

    async def login(self, *, force: bool = False) -> str:
        if not await self.registered():
            raise AppError("Account is not registered; deposit on Hyperliquid first")
        if force:
            self._ready = False
        return await self._ensure_ready()

    @locked
    async def _ensure_ready(self) -> str:
        if self._ready:
            return "ready"
        terms = await self._accept_terms()
        agent = await self._approve_agent()
        referrer = await self._set_referrer()
        builder = await self._setup("approveBuilderFee", {}, user=True)
        self._ready = True
        return ", ".join((terms, agent, referrer, builder))

    async def _accept_terms(self) -> str:
        status = await self._send("GET", "/meta/terms", params={"address": self.address})
        if status.get("accepted") is True:
            return "terms ok"
        version = status.get("termsVersion", "")
        await self._setup("metaAcceptTerms", {}, user=True, provider=META)
        logger.info(f"Accepted Jumper terms {version}".rstrip())
        return "terms accepted"

    async def _approve_agent(self) -> str:
        params = {"agentAddress": self.agent.address}
        try:
            return await self._setup("approveAgent", params, user=True)
        except ApiError as error:
            text = str(error).lower()
            if "extra agent" not in text and "agent slot" not in text:
                raise
            await self._setup("revokeAgent", {}, user=True)
            return await self._setup("approveAgent", params, user=True)

    async def _set_referrer(self) -> str:
        try:
            return await self._setup("setReferrer", {}, user=False)
        except ApiError as error:
            if "referr" not in str(error).lower():
                raise
            logger.warning(f"Jumper referral was not applied: {error}")
            return "referrer skipped"

    @ttl_cache(3600)
    async def _routable_symbols(self) -> list[str]:
        """Perp markets LI.FI will build orders for, HIP-3 dexes (`xyz:`, `io:`) included.

        LI.FI is the authority here, not Hyperliquid's `meta`: the native universe omits
        HIP-3 coins entirely, and a HIP-3 market LI.FI has not onboarded is rejected at
        build time even though Hyperliquid lists it. Spot entries (`@1`, `PURR/USDC`) are
        dropped because they are not perps, and delisted markets still appear in the feed.
        """
        rep = await self._api.request("GET", "/markets", params={"provider": PROVIDER})
        if not rep.ok:
            raise ApiError("Jumper markets error", rep)
        return [
            market["id"]
            for market in rep.json().get("markets", [])
            if market.get("categoryId") != "spot" and not market.get("isDelisted")
        ]

    async def get_symbols(self) -> list[str]:
        return await self._routable_symbols()

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._run(
            "updateLeverage",
            {
                "market": market_ref(self._resolve(symbol)[1]),
                "leverage": leverage,
                "marginMode": "ISOLATED",
            },
        )

    async def _place_order(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        tif: str,
        reduce_only=False,
    ) -> Order:
        params = order_params(symbol, side, qty, price, tif, reduce_only)
        result = await self._run("placeOrder", params)
        order_id = str(result.get("orderId") or "")
        if not order_id:
            raise JumperOrderUnknown("Jumper placeOrder was submitted without an order id")
        if params["type"] == "MARKET":
            return await self._wait_market_fill(order_id)
        return Order(
            id=order_id,
            symbol=symbol,
            side=side,
            size=qty,
            filled=Decimal(0),
            price=price,
            status=OrderStatus.OPEN,
            reduce_only=reduce_only,
        )

    async def _wait_market_fill(self, order_id: str) -> Order:
        deadline = time.monotonic() + MARKET_FILL_TIMEOUT
        while True:
            order = await self.get_order(order_id)
            if order is not None and order.status == OrderStatus.FILLED:
                return order
            if order is not None and order.status == OrderStatus.CANCELED:
                raise ApiError(
                    f"Jumper market order {order_id} canceled: filled {order.filled}/{order.size}"
                )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(MARKET_FILL_POLL)
        raise JumperOrderUnknown(
            f"Jumper market order {order_id} was submitted but its fill is not confirmed"
        )

    async def cancel_order(self, order: Order) -> bool:
        try:
            await self._run(
                "cancelOrder",
                {"ids": [order.id], "assetId": market_ref(order.symbol)["marketId"]},
            )
            return True
        except JumperOrderUnknown:
            raise
        except ApiError as error:
            logger.warning(f"cancel_order failed: {error}")
            return False

    async def cancel_all_orders(self) -> int:
        orders: list[dict] = []
        for dex in await self._dex_names():
            rep = await self._info(
                type="openOrders", user=self.address, **({} if not dex else {"dex": dex})
            )
            if isinstance(rep, list):
                orders.extend(rep)
        if not orders:
            return 0
        await self._run("cancelOrder", {"ids": [str(order["oid"]) for order in orders]})
        return len(orders)

    async def _run(self, action: str, params: dict) -> dict:
        await self._ensure_ready()
        try:
            created = await self._create(PROVIDER, action, params, user=False)
        except _SetupRequired:
            self._ready = False
            await self._ensure_ready()
            created = await self._create(PROVIDER, action, params, user=False)
        results = await self._execute(PROVIDER, action, created.get("actions") or [], user=False)
        return read_action_result(results, action)

    async def _setup(
        self,
        action: str,
        params: dict,
        *,
        user: bool,
        provider: str = PROVIDER,
    ) -> str:
        created = await self._create(provider, action, params, user=user)
        actions = created.get("actions") or []
        if not actions:
            return f"{action} ok"
        results = await self._execute(provider, action, actions, user=user)
        read_action_result(results, action)
        return f"{action} done"

    async def _create(self, provider: str, action: str, params: dict, *, user: bool) -> dict:
        try:
            return await self._send(
                "POST",
                "/createAction",
                json=self._body(provider, action, params, user=user),
            )
        except _TermsRequired as error:
            if provider == META and action == "metaAcceptTerms":
                raise error.error from None
            await self._accept_terms()
            return await self._send(
                "POST",
                "/createAction",
                json=self._body(provider, action, params, user=user),
            )

    async def _execute(self, provider: str, action: str, actions: list, *, user: bool) -> list:
        if not actions:
            return []
        signed = [
            {
                "action": step["action"],
                "typedData": step["typedData"],
                "signature": sign_typed_data(
                    self.account if user else self.agent, step["typedData"]
                ),
            }
            for step in actions
        ]
        body = self._body(provider, action, {}, user=user)
        body["actions"] = signed
        body.pop("params", None)
        response = await self._send("POST", "/executeAction", json=body, retry=False)
        results = response.get("results")
        if not isinstance(results, list):
            raise JumperOrderUnknown(f"Jumper {action} execute returned no results")
        return results

    def _body(self, provider: str, action: str, params: dict, *, user: bool) -> dict:
        body: dict[str, Any] = {
            "provider": provider,
            "address": self.address,
            "action": action,
            "params": params,
        }
        if not user:
            body["signerAddress"] = self.agent.address
        return body

    async def _send(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        retry: bool = True,
    ) -> dict:
        rep = await self._api.request(method, path, json=json, params=params, retry=retry)
        try:
            body = rep.json()
        except ValueError:
            body = None
        if rep.ok:
            if isinstance(body, dict):
                return body
            raise ApiError("Jumper API returned an unexpected body", rep)
        code = action_error_code(body)
        if code == TERMS_NOT_ACCEPTED:
            raise _TermsRequired(ApiError("Jumper terms", rep))
        if code == SETUP_REQUIRED:
            raise _SetupRequired(ApiError("Jumper setup", rep))
        raise ApiError("Jumper API", rep)


class _TermsRequired(Exception):
    def __init__(self, error: ApiError):
        self.error = error
        super().__init__(str(error))


class _SetupRequired(Exception):
    def __init__(self, error: ApiError):
        self.error = error
        super().__init__(str(error))
