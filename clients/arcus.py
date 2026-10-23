# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Signed, sealed, hopefully delivered
"""Arcus — dYdX Labs perpetual DEX (https://docs.arcus.xyz).

Auth is two-layer: an Ed25519 API key is registered once via EIP-712 signed by the
owning EVM wallet, then every mutating request carries X-API-Key / X-Timestamp /
X-Signature. Two signing schemes coexist:

  * typed    — placeOrder / cancelOrder sign a canonical JSON string directly
  * legacy   — cancelAllOrders / setLeverage sign timestamp + action + canonicalJSON(body)

Reads (/account, /positions, /markets, /bbo, /order) need no auth at all.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal, Self

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account.messages import encode_defunct, encode_typed_data
from pydantic import BaseModel, Field

from lib import utils
from lib.decorators import bind_log_context, locked, retry, ttl_cache
from lib.http import ApiError, AsyncHttp, HttpMethod
from lib.logger import logger
from lib.models import AccountConfig
from lib.utils import round_to_tick_size
from strategy import (
    Order,
    OrderBook,
    OrderStatus,
    Position,
    ProfileInfo,
    Side,
    TradingClient,
)

API_URL = "https://api.arcus.xyz"
APP_URL = "https://arcus.xyz"
CHAIN_ID = 4663  # testnet is 46630

# Points live on a separate, undocumented host behind Privy wallet auth. Values below
# are taken from the web client bundle (app.arcus.xyz), not guessed.
POINTS_API = "https://points-api.arcus.xyz"
PRIVY_API = "https://auth.privy.io/api/v1"
PRIVY_APP_ID = "cmobo450d00ug0cjy8hcx1645"
PRIVY_DOMAIN = "app.arcus.xyz"
PRIVY_URI = f"https://{PRIVY_DOMAIN}"
PRIVY_STATEMENT = (
    "By signing, you are proving you own this wallet and logging in. "
    "This does not initiate a transaction or cost any fees."
)
PRIVY_TOKEN_TTL = timedelta(minutes=45)  # identity tokens are short-lived

# API key labels are visible in the Arcus UI and identical names across accounts would
# link them. Derived from the address so each account gets its own stable, unrevealing
# name rather than a shared literal.
_NAME_HEADS = (
    "atlas", "aster", "basis", "cobalt", "ember", "harbor", "indigo", "juniper",
    "lumen", "meridian", "nimbus", "onyx", "quartz", "ridge", "solace", "tundra",
    "vertex", "willow", "zephyr", "cinder", "fathom", "kestrel", "lattice", "marlin",
)  # fmt: skip
_NAME_TAILS = (
    "alpha", "arc", "beam", "capital", "edge", "flow", "grid", "labs",
    "markets", "node", "quant", "signal", "stack", "systems", "vector", "works",
)  # fmt: skip


def api_wallet_name(address: str) -> str:
    """Stable per-account label, e.g. "meridian-vector-8c1f"."""
    digest = sha256(f"arcus-api-name:{address.lower()}".encode()).digest()
    head = _NAME_HEADS[digest[0] % len(_NAME_HEADS)]
    tail = _NAME_TAILS[digest[1] % len(_NAME_TAILS)]
    return f"{head}-{tail}-{digest[2:4].hex()}"


API_KEY_TTL = timedelta(days=30)  # server allows 1..180 days
API_KEY_RENEW_BEFORE = timedelta(days=1)  # re-register only once the key is nearly stale
GOOD_TIL = timedelta(days=40)  # must be >= 1 month ahead or the order is rejected
MARKET_SLIPPAGE = Decimal("0.05")  # MARKET orders must price within 10% of mark
QUANTUMS = Decimal(10) ** 9  # quote quantums per $1

# Canonical payload enums — values are fixed by the Arcus order-signing scheme.
_OP_PLACE, _OP_CANCEL = 1, 2
_SIDE_CODE: dict[str, int] = {"BUY": 0, "SELL": 1}
_TIF_CODE: dict[str, int] = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}

_OPEN_STATUS = {
    "PENDING", "OPEN", "PARTIALLY_FILLED", "ACK", "UNTRIGGERED",
    "TPSL_PLACED", "TPSL_TRIGGERED", "CANCEL_ACKNOWLEDGED", "CANCEL_PENDING",
}  # fmt: skip
_FILLED_STATUS = {"FILLED"}


def to_domain_status(status: str) -> OrderStatus:
    s = (status or "").upper()
    if s in _FILLED_STATUS:
        return OrderStatus.FILLED
    if s in _OPEN_STATUS:
        return OrderStatus.OPEN
    return OrderStatus.CANCELED  # CANCELED, REJECTED, MARGIN_CANCELED, LIQUIDATED, ADL, ERROR


def to_domain_side(side: str) -> Side:
    return "bid" if side.upper() in ("BUY", "LONG") else "ask"


def to_api_side(side: Side) -> str:
    return "BUY" if side == "bid" else "SELL"


class TradingHours(BaseModel):
    startSecondsOfDay: int
    endSecondsOfDay: int
    timezone: str | None = None
    isOvernight: bool = False


class ArcusMarket(BaseModel):
    marketId: int
    marketDisplayName: str
    baseAsset: str
    status: str
    tickSize: Decimal
    stepSize: Decimal
    minOrderSize: Decimal
    minOrderNotional: Decimal
    initialMarginFraction: Decimal | None = None
    markPrice: Decimal | None = None
    oraclePrice: Decimal | None = None
    lastTradePrice: Decimal | None = None
    volume24hNotional: Decimal = Decimal(0)
    regularTradingHours: TradingHours | None = None

    @property
    def price(self) -> Decimal:
        for value in (self.markPrice, self.oraclePrice, self.lastTradePrice):
            if value:
                return value
        raise ApiError(f"No price available for {self.marketDisplayName}")

    @property
    def max_leverage(self) -> int:
        imf = self.initialMarginFraction
        return int(1 / imf) if imf and imf > 0 else 1


class ArcusPosition(BaseModel):
    marketId: int
    marketDisplayName: str
    side: str
    size: Decimal
    averageEntryPrice: Decimal
    unrealizedPnl: Decimal = Decimal(0)
    leverage: Decimal | None = None


class ArcusOrder(BaseModel):
    orderId: str
    marketDisplayName: str
    side: str
    status: str
    price: Decimal | None = None
    originalSize: Decimal
    remainingSize: Decimal = Decimal(0)
    reduceOnly: bool = False

    @property
    def filled(self) -> Decimal:
        return self.originalSize - self.remainingSize


class ArcusAccount(BaseModel):
    equity: Decimal = Decimal(0)
    freeCollateral: Decimal = Decimal(0)
    netQuoteBalance: Decimal = Decimal(0)
    netDeposits: Decimal = Decimal(0)
    positions: dict[str, ArcusPosition] = Field(default_factory=dict)


class ArcusSeason(BaseModel):
    season: str
    active: bool = False
    points_visible: bool = False
    rank: int | None = None
    tier: str | None = None
    points: Decimal = Decimal(0)


class ArcusPoints(BaseModel):
    season: str | None = None
    season_points: Decimal = Decimal(0)
    total_points: Decimal = Decimal(0)
    season_rank: int | None = None
    tier: str | None = None
    next_drop_at: str | None = None
    seasons: list[ArcusSeason] = Field(default_factory=list)


class ArcusStats(BaseModel):
    """Volume and fees are quoted in quantums: 1e9 quantums == $1."""

    lifetimeVolume: Decimal = Decimal(0)
    lifetimeFeesPaid: Decimal = Decimal(0)


@bind_log_context
class ArcusClient:
    exchange = "arcus"

    @classmethod
    def __type_check(cls) -> type[TradingClient]:
        return ArcusClient

    @classmethod
    def from_config(cls, cfg: AccountConfig) -> Self:
        return cls(name=cfg.name, privkey=cfg.privkey.get_secret_value(), proxy=cfg.proxy)

    def __init__(self, name: str, privkey: str, proxy: str | None = None, account_index: int = 0):
        self.account = utils.parse_eth_key(privkey, name)
        self.address = self.account.address
        self.name = name
        self.account_index = account_index
        self.api_wallet_name = api_wallet_name(self.address)
        self._api_key_valid_until: datetime | None = None
        self._privy_token: str | None = None
        self._privy_token_at: datetime | None = None

        # Derived deterministically from the EVM key so the same API key is reproduced on
        # every run. Arcus explicitly allows re-registering a key you already own.
        seed = sha256(f"arcus-api-key:{self.address.lower()}".encode() + self.account.key).digest()
        self._signer = Ed25519PrivateKey.from_private_bytes(seed)
        self.api_key = self._signer.public_key().public_bytes_raw().hex()

        self.http = AsyncHttp(
            baseurl=API_URL,
            headers={"Origin": APP_URL, "Referer": f"{APP_URL}/"},
            proxy=proxy,
        )

    # MARK: Transport

    async def _call(self, method: HttpMethod, path: str, **kwargs) -> Any:
        rep = await self.http.request(method, path, **kwargs)
        if rep.status_code == 404:
            return None
        if not rep.ok:
            raise ApiError("API error", rep)
        return rep.json()

    def _sign_typed(self, payload: str) -> str:
        """Scheme 1 — the canonical JSON string itself is the signing message."""
        return self._signer.sign(payload.encode()).hex()

    def _sign_legacy(self, ts: str, action: str, body: dict) -> str:
        """Scheme 2 — timestamp + action + canonicalJSON(body), no separators."""
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return self._signer.sign(f"{ts}{action}{canonical}".encode()).hex()

    def _headers(self, ts: str, signature: str) -> dict[str, str]:
        return {"X-API-Key": self.api_key, "X-Timestamp": ts, "X-Signature": signature}

    async def _signed(self, action: str, body: dict, *, signature: str, ts: str) -> Any:
        return await self._call(
            "POST",
            f"/v1/{action}?address={self.address}",
            json=body,
            headers=self._headers(ts, signature),
        )

    # MARK: Lifecycle

    async def _registered_key_expiry(self) -> datetime | None:
        """Expiry of our Ed25519 key as the exchange sees it, or None if not registered."""
        res = await self._call("GET", f"/v1/apiKeys?address={self.address}")
        for key in (res or {}).get("apiKeys", []):
            if key.get("apiKey") != self.api_key or key.get("status") != "ACTIVE":
                continue
            valid_until = key.get("validUntil")
            if not valid_until:
                return datetime.max.replace(tzinfo=UTC)  # registered with no expiry
            return datetime.fromtimestamp(valid_until / 1000, UTC)
        return None

    @locked
    async def login(self, *, force: bool = False) -> None:
        now = datetime.now(UTC)
        if (
            not force
            and self._api_key_valid_until
            and self._api_key_valid_until - now > API_KEY_RENEW_BEFORE
        ):
            return

        # The expiry only lived in memory, so every process start used to re-register a
        # key that was already ACTIVE. Ask the exchange instead: that costs one GET
        # rather than an EIP-712 signature, and avoids the brief window after
        # registration in which a key is not yet usable.
        if not force:
            expiry = await self._registered_key_expiry()
            if expiry and expiry - now > API_KEY_RENEW_BEFORE:
                self._api_key_valid_until = expiry
                logger.debug(f"API key already active until {expiry:%Y-%m-%d}")
                return

        valid_until = int((now + API_KEY_TTL).timestamp() * 1_000)
        nonce = str(time.time_ns())
        message = {
            "apiWalletName": self.api_wallet_name,
            "apiWalletPublicKey": self.api_key,
            "validUntil": valid_until,
            "nonce": nonce,
            "accountIndex": self.account_index,
        }
        signed = self.account.sign_message(
            encode_typed_data(
                full_message={
                    "types": {
                        "CreateApiKey": [
                            {"name": "apiWalletName", "type": "string"},
                            {"name": "apiWalletPublicKey", "type": "string"},
                            {"name": "validUntil", "type": "uint256"},
                            {"name": "nonce", "type": "string"},
                            {"name": "accountIndex", "type": "uint8"},
                        ]
                    },
                    "domain": {"name": "Arcus API Key", "version": "1", "chainId": CHAIN_ID},
                    "primaryType": "CreateApiKey",
                    "message": message,
                }
            )
        )

        await self._call(
            "POST",
            "/v1/createApiKey",
            json={
                "address": self.address,
                "publicKey": self.api_key,
                "apiWalletName": self.api_wallet_name,
                "accountIndex": self.account_index,
                "validUntil": valid_until,
                "nonce": nonce,
                # `v` must be a hex string ("0x1b"/"0x1c"), not an int - an int is
                # rejected with 400 "Invalid request body". Matches the web client's
                # splitEthereumSignature().
                "signature": {
                    "r": f"0x{signed.r:064x}",
                    "s": f"0x{signed.s:064x}",
                    "v": f"0x{(signed.v if signed.v >= 27 else signed.v + 27):02x}",
                },
            },
        )
        self._api_key_valid_until = now + API_KEY_TTL
        logger.debug(f"API key registered until {self._api_key_valid_until:%Y-%m-%d}")

    async def registered(self) -> bool:
        return await self._call("GET", f"/v1/account?address={self.address}") is not None

    # MARK: Markets

    @ttl_cache(60)
    async def _markets(self) -> dict[str, ArcusMarket]:
        res = await self._call("GET", "/v1/markets")
        markets = [ArcusMarket(**m) for m in (res or {}).get("markets", [])]
        return {m.marketDisplayName: m for m in markets}

    async def _market(self, symbol: str) -> ArcusMarket:
        # Only the exact marketDisplayName is accepted. Positions come back labelled with
        # it, and the strategy matches positions to trades by exact string, so an alias
        # like "BTC" would place orders fine and then never find them again to close.
        markets = await self._markets()
        if symbol in markets:
            return markets[symbol]
        hint = next((k for k in markets if k.split("-")[0] == symbol.upper()), None)
        suffix = f"; did you mean {hint}?" if hint else ""
        raise ApiError(f"Unknown Arcus market: {symbol}{suffix}")

    async def get_symbols(self) -> list[str]:
        markets = (await self._markets()).values()
        online = [m for m in markets if m.status == "ONLINE"]
        return [m.marketDisplayName for m in sorted(online, key=lambda m: -m.volume24hNotional)]

    async def is_symbol_tradeable(self, symbol: str, at: datetime, reduce_only=False) -> bool:
        market = await self._market(symbol)
        if market.status != "ONLINE":
            return False

        hours = market.regularTradingHours
        if hours is None:
            return True  # 24/7 market

        at_utc = at.astimezone(UTC) if at.tzinfo else at.replace(tzinfo=UTC)
        second_of_day = at_utc.hour * 3600 + at_utc.minute * 60 + at_utc.second
        start, end = hours.startSecondsOfDay, hours.endSecondsOfDay
        if hours.isOvernight or start > end:
            return second_of_day >= start or second_of_day < end
        return start <= second_of_day < end

    async def get_lot_size(self, symbol: str) -> Decimal:
        return (await self._market(symbol)).stepSize

    async def get_tick_size(self, symbol: str) -> Decimal:
        return (await self._market(symbol)).tickSize

    async def get_min_trade_usd(self, symbol: str) -> Decimal:
        market = await self._market(symbol)
        return max(market.minOrderNotional, market.minOrderSize * market.price)

    # MARK: Prices

    async def get_price(self, symbol: str) -> Decimal:
        return (await self._market(symbol)).price

    async def get_bbo(self, symbol: str) -> tuple[Decimal, Decimal]:
        market = await self._market(symbol)
        res = await self._call("GET", f"/v1/bbo/{market.marketDisplayName}")
        bid, ask = (res or {}).get("bestBid"), (res or {}).get("bestAsk")
        if not bid or not ask:
            raise ApiError(f"Empty orderbook for {market.marketDisplayName}")
        return Decimal(str(bid["price"])), Decimal(str(ask["price"]))

    @ttl_cache(5)
    async def get_order_book(self, symbol: str) -> OrderBook:
        market = await self._market(symbol)
        res = await self._call("GET", f"/v1/l2Orderbook/{market.marketDisplayName}") or {}
        bids, asks = res.get("bids", []), res.get("asks", [])
        if not bids or not asks:
            bid, ask = await self.get_bbo(symbol)
            return OrderBook.build(bids=[(bid, 0)], asks=[(ask, 0)])
        return OrderBook.build(bids=bids, asks=asks)  # levels are ["price", "size"] pairs

    # MARK: Account

    async def _account(self) -> ArcusAccount:
        res = await self._call("GET", f"/v1/account?address={self.address}")
        return ArcusAccount(**res) if res else ArcusAccount()

    async def balance(self) -> Decimal:
        return (await self._account()).equity

    @locked
    async def _identity_token(self) -> str:
        """Mint a Privy identity token by signing Arcus's SIWE message with the wallet key.

        Points are gated behind Privy rather than the Ed25519 API key, so this is a second,
        independent auth path. Tokens are short-lived, hence the TTL refresh.
        """
        now = datetime.now(UTC)
        if (
            self._privy_token
            and self._privy_token_at
            and now - self._privy_token_at < PRIVY_TOKEN_TTL
        ):
            return self._privy_token

        headers = {
            "privy-app-id": PRIVY_APP_ID,
            "privy-client": "react-auth",
            "Origin": PRIVY_URI,
            "Referer": f"{PRIVY_URI}/",
        }
        rep = await self.http.request(
            "POST", f"{PRIVY_API}/siwe/init", headers=headers, json={"address": self.address}
        )
        if not rep.ok:
            raise ApiError("Privy SIWE init failed", rep)
        nonce = rep.json()["nonce"]

        message = self._siwe_message(nonce, now)
        signature = self.account.sign_message(encode_defunct(text=message)).signature.hex()
        rep = await self.http.request(
            "POST",
            f"{PRIVY_API}/siwe/authenticate",
            headers=headers,
            json={
                "message": message,
                "signature": signature if signature.startswith("0x") else f"0x{signature}",
                "chainId": f"eip155:{CHAIN_ID}",
                "walletClientType": "metamask",
                "connectorType": "injected",
                "mode": "login-or-sign-up",
            },
        )
        if not rep.ok:
            raise ApiError("Privy SIWE authenticate failed", rep)
        token = rep.json().get("identity_token")
        if not token:
            raise ApiError("Privy returned no identity_token")

        self._privy_token, self._privy_token_at = token, now
        return token

    def _siwe_message(self, nonce: str, issued_at: datetime) -> str:
        """EIP-4361 message, byte-for-byte as app.arcus.xyz builds it."""
        issued = issued_at.isoformat().replace("+00:00", "Z")
        return (
            f"{PRIVY_DOMAIN} wants you to sign in with your Ethereum account:\n"
            f"{self.address}\n\n{PRIVY_STATEMENT}\n\n"
            f"URI: {PRIVY_URI}\nVersion: 1\nChain ID: {CHAIN_ID}\n"
            f"Nonce: {nonce}\nIssued At: {issued}\nResources:\n- https://privy.io"
        )

    async def points(self) -> ArcusPoints:
        token = await self._identity_token()
        rep = await self.http.request(
            "GET", f"{POINTS_API}/v1/points/{self.address}", headers={"privy-id-token": token}
        )
        if not rep.ok:
            raise ApiError("Points request failed", rep)
        return ArcusPoints(**rep.json())

    async def stats(self) -> ArcusStats:
        res = await self._call("GET", f"/v1/account/stats?address={self.address}&include=volumes")
        return ArcusStats(**res) if res else ArcusStats()

    async def profile(self) -> ProfileInfo:
        account, stats, points = await asyncio.gather(
            self._account(), self.stats(), self.points(), return_exceptions=True
        )
        if isinstance(account, BaseException):
            raise account
        if isinstance(stats, BaseException):
            raise stats
        if isinstance(points, BaseException):
            # Points ride on third-party (Privy) auth; never let them break the report.
            logger.debug(f"points unavailable: {points}")
            points = ArcusPoints()
        # Arcus exposes no realized-PnL field. netQuoteBalance moves only on external cash
        # flows, fills, fees and funding, so subtracting lifetime deposits leaves exactly
        # realized trading P/L net of fees and funding.
        return ProfileInfo(
            addr=utils.short_addr(self.address),
            balance=account.equity,
            volume=stats.lifetimeVolume / QUANTUMS,
            pnl=account.netQuoteBalance - account.netDeposits,
            points=points.season_points,
            rank=points.season_rank,
            mode=points.tier,
        )

    async def positions(self) -> list[Position]:
        res = await self._call(
            "GET", f"/v1/positions?address={self.address}&accountIndex={self.account_index}"
        )
        out: list[Position] = []
        for raw in (res or {}).get("positions", {}).values():
            pos = ArcusPosition(**raw)
            if pos.size == 0:
                continue
            out.append(
                Position(
                    id=str(pos.marketId),
                    symbol=pos.marketDisplayName,
                    side=to_domain_side(pos.side),
                    size=abs(pos.size),
                    entry_price=pos.averageEntryPrice,
                    unrealized_pnl=pos.unrealizedPnl,
                )
            )
        return out

    async def close_position(self, position: Position) -> bool:
        side: Side = "ask" if position.side == "bid" else "bid"
        await self.market_order(position.symbol, side, position.size, reduce_only=True)
        return True

    async def close_all_positions(self) -> int:
        positions = await self.positions()
        for position in positions:
            await self.close_position(position)
        return len(positions)

    # MARK: Orders

    @staticmethod
    def _to_units(value: Decimal, unit: Decimal, what: str) -> int:
        scaled = value / unit
        if scaled != scaled.to_integral_value():
            raise ApiError(f"{what} {value} is not a multiple of {unit}")
        return int(scaled)

    async def _place(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        order_type: Literal["LIMIT", "MARKET"],
        tif: Literal["GTT", "IOC", "FOK", "ALO"],
        reduce_only: bool,
    ) -> Order:
        await self.login()
        market = await self._market(symbol)

        qty = round_to_tick_size(qty, market.stepSize)
        price = round_to_tick_size(price, market.tickSize)
        if qty <= 0:
            raise ApiError(f"Order quantity rounds to zero for {market.marketDisplayName}")

        ts = str(time.time_ns())
        good_til_us = int((time.time() + GOOD_TIL.total_seconds()) * 1_000_000)
        api_side = to_api_side(side)

        # Canonical payload: keys alphabetical, no whitespace, address lowercased,
        # `g` in nanoseconds while the body carries microseconds.
        payload = (
            f'{{"ad":"{self.address.lower()}","ai":{self.account_index},"ct":{ts},'
            f'"g":{good_til_us * 1000},"m":{market.marketId},"op":{_OP_PLACE},'
            f'"p":{self._to_units(price, market.tickSize, "price")},'
            f'"q":{self._to_units(qty, market.stepSize, "quantity")},'
            f'"r":{1 if reduce_only else 0},"s":{_SIDE_CODE[api_side]},'
            f'"t":{_TIF_CODE[tif]},"v":1}}'
        )

        body = {
            "address": self.address,
            "accountIndex": self.account_index,
            "marketId": market.marketId,
            "orderSide": api_side,
            "orderType": order_type,
            "quantity": str(qty),
            "price": str(price),
            "timeInForce": tif,
            "goodTilTime": str(good_til_us),
            "timestamp": int(ts),
            "reduceOnly": reduce_only,
        }

        res = await self._signed("placeOrder", body, signature=self._sign_typed(payload), ts=ts)
        return Order(
            id=str(res["orderId"]),
            symbol=market.marketDisplayName,
            side=side,
            size=qty,
            filled=Decimal(str(res.get("filledSize", 0) or 0)),
            price=price if order_type == "LIMIT" else None,
            status=to_domain_status(res.get("status", "")),
            reduce_only=reduce_only,
        )

    async def market_order(self, symbol: str, side: Side, qty: Decimal, reduce_only=False) -> Order:
        # Arcus has no unpriced market order: MARKET must be IOC and priced within 10%
        # of mark, so cross the book by a bounded margin instead.
        mark = await self.get_price(symbol)
        slip = Decimal(1) + (MARKET_SLIPPAGE if side == "bid" else -MARKET_SLIPPAGE)
        return await self._place(
            symbol, side, qty, mark * slip, order_type="MARKET", tif="IOC", reduce_only=reduce_only
        )

    async def limit_order(
        self, symbol: str, side: Side, qty: Decimal, price: Decimal, reduce_only=False
    ) -> Order:
        # Reduce-only orders may not rest on the book; the engine requires IOC or FOK.
        tif: Literal["GTT", "IOC"] = "IOC" if reduce_only else "GTT"
        return await self._place(
            symbol, side, qty, price, order_type="LIMIT", tif=tif, reduce_only=reduce_only
        )

    @retry(max_attempts=3, delay=1.0)
    async def get_order(self, order_id: str) -> Order | None:
        res = await self._call("GET", f"/v1/order/{order_id}?address={self.address}")
        if res is None:
            return None
        raw = ArcusOrder(**res)
        return Order(
            id=raw.orderId,
            symbol=raw.marketDisplayName,
            side=to_domain_side(raw.side),
            size=raw.originalSize,
            filled=raw.filled,
            price=raw.price,
            status=to_domain_status(raw.status),
            reduce_only=raw.reduceOnly,
        )

    async def cancel_order(self, order: Order) -> bool:
        await self.login()
        market = await self._market(order.symbol)
        ts = str(time.time_ns())
        payload = (
            f'{{"ad":"{self.address.lower()}","ai":{self.account_index},"ct":{ts},'
            f'"id":"{order.id}","m":{market.marketId},"op":{_OP_CANCEL},"v":1}}'
        )
        body = {
            "kind": "orderId",
            "address": self.address,
            "accountIndex": self.account_index,
            "marketId": market.marketId,
            "orderId": order.id,
        }
        await self._signed("cancelOrder", body, signature=self._sign_typed(payload), ts=ts)
        return True

    async def cancel_all_orders(self) -> int:
        await self.login()
        ts = str(time.time_ns())
        body = {"address": self.address, "accountIndex": self.account_index}
        signature = self._sign_legacy(ts, "cancelAllOrders", body)
        await self._signed("cancelAllOrders", body, signature=signature, ts=ts)
        return 0  # engine acks asynchronously; per-order results arrive on the ws channel

    # MARK: Leverage

    async def get_leverage(self, symbol: str) -> int | None:
        # Arcus exposes no leverage read endpoint; it is only visible on an open position.
        market = await self._market(symbol)
        res = await self._call(
            "GET",
            f"/v1/positions?address={self.address}"
            f"&accountIndex={self.account_index}&market={market.marketId}",
        )
        for raw in (res or {}).get("positions", {}).values():
            lev = ArcusPosition(**raw).leverage
            if lev:
                return int(lev)
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self.login()
        market = await self._market(symbol)
        if leverage > market.max_leverage:
            raise ApiError(
                f"{market.marketDisplayName} allows at most {market.max_leverage}x, got {leverage}x"
            )

        ts = str(time.time_ns())
        body = {
            "address": self.address,
            "accountIndex": self.account_index,
            "marketId": market.marketId,
            "leverage": leverage,
        }
        signature = self._sign_legacy(ts, "setLeverage", body)
        await self._signed("setLeverage", body, signature=signature, ts=ts)
