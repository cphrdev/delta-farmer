import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from clients.arcus import (
    _OP_CANCEL,
    _OP_PLACE,
    _SIDE_CODE,
    _TIF_CODE,
    ArcusClient,
    ArcusMarket,
    TradingHours,
    to_api_side,
    to_domain_side,
    to_domain_status,
)
from lib.http import ApiError
from strategy import OrderStatus

KEY = "0x" + "11" * 32


def client() -> ArcusClient:
    return ArcusClient(name="test", privkey=KEY)


def market(hours: TradingHours | None = None, status: str = "ONLINE") -> ArcusMarket:
    return ArcusMarket(
        marketId=1,
        marketDisplayName="BTC-USD",
        baseAsset="BTC",
        status=status,
        tickSize=Decimal("0.5"),
        stepSize=Decimal("0.001"),
        minOrderSize=Decimal("0.001"),
        minOrderNotional=Decimal(5),
        initialMarginFraction=Decimal("0.05"),
        markPrice=Decimal(100_000),
    )


# MARK: canonical payload encoding


def test_enum_codes_match_arcus_scheme():
    # Fixed by the Arcus order-signing scheme; changing them silently breaks every signature.
    assert (_OP_PLACE, _OP_CANCEL) == (1, 2)
    assert _SIDE_CODE == {"BUY": 0, "SELL": 1}
    assert _TIF_CODE == {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}


def test_to_units_requires_exact_multiple():
    assert ArcusClient._to_units(Decimal("100000.5"), Decimal("0.5"), "price") == 200001
    assert ArcusClient._to_units(Decimal("0.003"), Decimal("0.001"), "qty") == 3
    with pytest.raises(ApiError, match="not a multiple"):
        ArcusClient._to_units(Decimal("100000.25"), Decimal("0.5"), "price")


def test_place_payload_is_canonical_and_signed_verbatim():
    acc = client()
    mkt = market()
    ts = "1712345678000000000"
    good_til_us = 4102444800000000

    payload = (
        f'{{"ad":"{acc.address.lower()}","ai":0,"ct":{ts},'
        f'"g":{good_til_us * 1000},"m":{mkt.marketId},"op":1,'
        f'"p":200000,"q":3,"r":0,"s":0,"t":0,"v":1}}'
    )

    parsed = json.loads(payload)
    # keys must be alphabetical, address lowercased, ct == X-Timestamp
    assert list(parsed) == sorted(parsed)
    assert parsed["ad"] == parsed["ad"].lower()
    assert str(parsed["ct"]) == ts
    # `g` is nanoseconds while the request body carries microseconds
    assert parsed["g"] == good_til_us * 1000
    # signature is over the raw string with no prefix, and verifies against the api key
    sig = acc._sign_typed(payload)
    assert len(sig) == 128
    acc._signer.public_key().verify(bytes.fromhex(sig), payload.encode())


def test_legacy_signature_is_timestamp_action_canonical_json():
    acc = client()
    ts = "1712345678000000000"
    body = {"marketId": 1, "address": acc.address, "accountIndex": 0}
    sig = acc._sign_legacy(ts, "cancelAllOrders", body)

    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert canonical == f'{{"accountIndex":0,"address":"{acc.address}","marketId":1}}'
    acc._signer.public_key().verify(bytes.fromhex(sig), f"{ts}cancelAllOrders{canonical}".encode())


def test_api_key_is_deterministic_for_a_wallet():
    assert client().api_key == client().api_key
    assert len(client().api_key) == 64
    other = ArcusClient(name="other", privkey="0x" + "22" * 32)
    assert other.api_key != client().api_key


# MARK: domain mapping


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FILLED", OrderStatus.FILLED),
        ("OPEN", OrderStatus.OPEN),
        ("PARTIALLY_FILLED", OrderStatus.OPEN),
        ("CANCEL_ACKNOWLEDGED", OrderStatus.OPEN),  # cancel in flight, not yet terminal
        ("CANCELED", OrderStatus.CANCELED),
        ("REJECTED", OrderStatus.CANCELED),
        ("LIQUIDATED", OrderStatus.CANCELED),
    ],
)
def test_status_mapping(raw, expected):
    assert to_domain_status(raw) == expected


def test_side_mapping_round_trips():
    assert to_domain_side("BUY") == "bid" and to_domain_side("LONG") == "bid"
    assert to_domain_side("SELL") == "ask" and to_domain_side("SHORT") == "ask"
    assert to_api_side("bid") == "BUY" and to_api_side("ask") == "SELL"


def test_max_leverage_from_initial_margin_fraction():
    assert market().max_leverage == 20


# MARK: api key naming


def test_api_wallet_name_is_unique_stable_and_unrevealing():
    """The label is visible in the Arcus UI, so a shared literal would link the accounts."""
    from eth_account import Account

    from clients.arcus import api_wallet_name

    addrs = [Account.from_key("0x" + f"{i:02x}" * 32).address for i in range(1, 60)]
    names = [api_wallet_name(a) for a in addrs]

    assert len(set(names)) == len(names), "names must differ per account"
    assert all(api_wallet_name(a) == n for a, n in zip(addrs, names)), "must be stable"
    # case-insensitive on the address, since callers may pass either form
    assert api_wallet_name(addrs[0].lower()) == names[0]
    for n in names:
        assert 1 <= len(n) <= 64  # server bound
        assert "delta" not in n and "farmer" not in n and "arcus" not in n


# MARK: privy siwe message


def test_siwe_message_is_byte_exact():
    """Privy verifies the EIP-4361 text it generated; any drift here fails auth silently."""
    from datetime import datetime

    from clients.arcus import PRIVY_DOMAIN, PRIVY_STATEMENT, PRIVY_URI

    acc = client()
    at = datetime(2026, 10, 23, 18, 30, tzinfo=UTC)
    msg = acc._siwe_message("abc123", at)

    assert msg.startswith(f"{PRIVY_DOMAIN} wants you to sign in with your Ethereum account:\n")
    assert f"\n{acc.address}\n\n{PRIVY_STATEMENT}\n\n" in msg
    assert f"URI: {PRIVY_URI}\nVersion: 1\nChain ID: 4663\n" in msg
    assert "Nonce: abc123\nIssued At: 2026-10-23T18:30:00Z\n" in msg
    assert msg.endswith("Resources:\n- https://privy.io")
    # checksummed address, not lowercased - unlike the order payload's `ad`
    assert acc.address in msg and acc.address.lower() not in msg


# MARK: symbol identity


@pytest.mark.asyncio
async def test_bare_asset_is_rejected_with_a_hint():
    """Positions report marketDisplayName and strategy/trade.py matches by exact string,
    so accepting an alias here would silently orphan every position at close time."""
    acc = StubMarketsClient({"BTC-USD": market()})
    assert (await acc._market("BTC-USD")).marketId == 1
    with pytest.raises(ApiError, match="did you mean BTC-USD"):
        await acc._market("BTC")


@pytest.mark.asyncio
async def test_position_symbol_matches_the_configured_symbol():
    acc = StubMarketsClient({"BTC-USD": market()})
    configured = "BTC-USD"
    assert (await acc._market(configured)).marketDisplayName == configured


# MARK: trading hours


class StubMarketsClient(ArcusClient):
    """Pins the whole market map so symbol resolution can be tested offline."""

    def __init__(self, markets: dict[str, ArcusMarket]):
        super().__init__(name="test", privkey=KEY)
        self.stub = markets

    async def _markets(self) -> dict[str, ArcusMarket]:
        return self.stub


class StubMarketClient(ArcusClient):
    """Pins one market so trading-hours logic can be exercised without the API."""

    def __init__(self, mkt: ArcusMarket):
        super().__init__(name="test", privkey=KEY)
        self.stub = mkt

    async def _market(self, symbol: str) -> ArcusMarket:
        return self.stub


async def tradeable(mkt: ArcusMarket, at: datetime) -> bool:
    return await StubMarketClient(mkt).is_symbol_tradeable("BTC-USD", at)


@pytest.mark.asyncio
async def test_offline_market_is_never_tradeable():
    assert not await tradeable(market(status="OFFLINE"), datetime(2026, 5, 29, 12, tzinfo=UTC))


@pytest.mark.asyncio
async def test_market_without_hours_is_always_tradeable():
    assert await tradeable(market(), datetime(2026, 5, 29, 3, tzinfo=UTC))


@pytest.mark.asyncio
async def test_regular_hours_window_is_enforced():
    hours = TradingHours(startSecondsOfDay=13 * 3600 + 1800, endSecondsOfDay=20 * 3600)
    mkt = market()
    mkt.regularTradingHours = hours
    assert await tradeable(mkt, datetime(2026, 5, 29, 15, tzinfo=UTC))
    assert not await tradeable(mkt, datetime(2026, 5, 29, 21, tzinfo=UTC))
    assert not await tradeable(mkt, datetime(2026, 5, 29, 13, tzinfo=UTC))


@pytest.mark.asyncio
async def test_overnight_session_wraps_midnight():
    hours = TradingHours(startSecondsOfDay=22 * 3600, endSecondsOfDay=5 * 3600, isOvernight=True)
    mkt = market()
    mkt.regularTradingHours = hours
    assert await tradeable(mkt, datetime(2026, 5, 29, 23, tzinfo=UTC))
    assert await tradeable(mkt, datetime(2026, 5, 29, 2, tzinfo=UTC))
    assert not await tradeable(mkt, datetime(2026, 5, 29, 12, tzinfo=UTC))
