import json
from datetime import UTC, datetime, timedelta
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


# MARK: realized pnl source


def test_leaderboard_row_must_match_our_address():
    """Without window=all the address filter is ignored and the global top-N returns, so
    a row for someone else must never be mistaken for ours."""
    from clients.arcus import ArcusLeaderboard

    acc = client()
    entries: list[dict[str, object]] = [
        {"address": "0xdeadbeef00000000000000000000000000000000", "volume": 1, "pnl": 999},
        {"address": acc.address.lower(), "volume": 129863166402551, "pnl": -87673610457},
    ]
    match = next((e for e in entries if str(e["address"]).lower() == acc.address.lower()), None)
    assert match is not None
    row = ArcusLeaderboard.model_validate(match)
    assert row.pnl == Decimal(-87673610457)
    assert row.volume / (Decimal(10) ** 9) == Decimal("129863.166402551")


def test_quantum_scaling():
    from clients.arcus import QUANTUMS

    assert Decimal(10) ** 9 == QUANTUMS  # 1e9 quantums == $1
    assert Decimal(-87673610457) / QUANTUMS == Decimal("-87.673610457")


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
async def test_equity_perps_trade_continuously():
    """Arcus equity perps trade nights and weekends; regularTradingHours only selects the
    margin/pricing regime. Gating on the clock here idles ~60% of the week for nothing."""
    mkt = market()
    mkt.regularTradingHours = TradingHours(
        startSecondsOfDay=4 * 3600, endSecondsOfDay=20 * 3600, timezone="America/New_York"
    )
    for at in (
        datetime(2026, 10, 24, 15, tzinfo=UTC),  # Saturday
        datetime(2026, 10, 25, 7, tzinfo=UTC),  # Sunday 02:00 ET
        datetime(2026, 10, 26, 2, tzinfo=UTC),  # Sunday 22:00 ET
        datetime(2026, 10, 26, 15, tzinfo=UTC),  # Monday 11:00 ET
    ):
        assert await tradeable(mkt, at), at


def test_off_hours_leverage_uses_the_margin_surcharge():
    mkt = market()
    mkt.initialMarginFraction = Decimal("0.02")
    mkt.offHoursInitialMarginFraction = Decimal("0.03")  # +50% surcharge
    mkt.isOutsideRth = False
    assert mkt.max_leverage == 50
    mkt.isOutsideRth = True
    assert mkt.max_leverage == 33


def test_prices_are_clamped_into_the_off_hours_band():
    """Off-hours trades must sit inside a band around the last settlement price; a plain
    5% cross lands outside it and the order is rejected."""
    mkt = market()
    mkt.lowerTradingBound = Decimal("752.92")
    mkt.upperTradingBound = Decimal("768.12")
    assert mkt.clamp_to_bounds(Decimal("798.68")) == Decimal("768.12")
    assert mkt.clamp_to_bounds(Decimal("700.00")) == Decimal("752.92")
    assert mkt.clamp_to_bounds(Decimal("760.50")) == Decimal("760.50")
    # crypto has no bands - the price passes through untouched
    crypto = market()
    assert crypto.clamp_to_bounds(Decimal("84786.13")) == Decimal("84786.13")


# MARK: history / stats


_FILL = {
    "tradeId": "27364502",
    "orderId": "09a2d6864d424737",
    "marketId": 23,
    "marketDisplayName": "SPY-USD",
    "side": "SELL",
    "role": "TAKER",
    "originalSize": "2.7887712",
    "size": "2.7887712",
    "price": "758.92",
    "fee": "0.450804752",
    "closedPnl": "-0.450804752",
    "positionEffect": "OPEN_SHORT",
    "remainingSize": "0",
    "createdAt": 1793580239036631,
}


def test_fill_volume_and_timestamp_use_plain_decimals():
    from clients.arcus import ArcusFill

    fill = ArcusFill(**_FILL)

    # /v1/fills is NOT quantum-scaled, unlike /v1/account/stats.
    assert fill.volume == Decimal("758.92") * Decimal("2.7887712")
    assert fill.created_at == datetime.fromtimestamp(1793580239.036631, tz=UTC)
    assert fill.created_at.tzinfo == UTC


def test_closed_pnl_is_already_net_of_the_fill_fee():
    """An opening leg realizes no price PnL, so closedPnl is exactly -fee.

    Burn must therefore use closedPnl alone; subtracting `fee` again double-counts it.
    """
    from clients.arcus import ArcusFill

    fill = ArcusFill(**_FILL)

    assert fill.closedPnl == -fill.fee
    assert fill.closedPnl - fill.fee != -fill.fee  # the double-count this guards against


def test_funding_payment_sign_and_synthetic_id():
    from clients.arcus import ArcusFunding

    received = ArcusFunding(marketId=23, payment=Decimal("0.010878076"), time=1793581200000000)
    paid = ArcusFunding(marketId=20, payment=Decimal("-0.011269597"), time=1793577600000000)

    assert received.payment > 0  # positive means the account received it
    assert paid.payment < 0
    # No payment id exists; one market settles once per timestamp.
    assert received.id == "23-1793581200000000"
    assert received.id != paid.id


async def test_history_always_sends_an_explicit_from_bound():
    """Without `from` the API silently answers with only the last 30 days."""
    from clients.arcus import _MIN_MICROS

    acc = client()
    calls: list[str] = []

    async def call(_method, path):
        calls.append(path)
        return {"fills": [_FILL]}

    acc._call = call
    await acc.fills()

    assert f"from={_MIN_MICROS}" in calls[0]
    assert "to=" not in calls[0]


async def test_history_pages_backwards_and_dedupes_the_overlapping_bound():
    from clients.arcus import _HISTORY_PAGE

    acc = client()
    calls: list[str] = []
    # Oldest row of page 1 repeats as newest of page 2: both bounds are inclusive.
    base = 1_793_580_239_036_631  # real scale: microseconds, well above the API floor
    page1 = [{**_FILL, "tradeId": str(i), "createdAt": base - i} for i in range(_HISTORY_PAGE)]
    page2 = [{**_FILL, "tradeId": str(_HISTORY_PAGE - 1), "createdAt": base - 999}, _FILL]

    async def call(_method, path):
        calls.append(path)
        return {"fills": page1 if len(calls) == 1 else page2}

    acc._call = call
    fills = await acc.fills()

    assert len(calls) == 2
    assert f"to={base - 999}" in calls[1]  # walked back to the oldest seen
    ids = [f.tradeId for f in fills]
    assert len(ids) == len(set(ids))  # the repeated row collapsed
    assert len(ids) == _HISTORY_PAGE + 1


async def test_history_stops_on_a_short_page():
    acc = client()
    calls: list[str] = []

    async def call(_method, path):
        calls.append(path)
        return {"fills": [_FILL]}

    acc._call = call
    await acc.funding_payments()

    assert len(calls) == 1


async def test_history_tolerates_an_empty_answer():
    acc = client()

    async def call(_method, _path):
        return None

    acc._call = call

    assert await acc.fills() == []
    assert await acc.funding_payments() == []


_POINTS = {
    "address": "0x18dafd733ba63361a367d6bc43996ecf6a14a1bc",
    "season": "Season 1",
    "season_rank": 4036,
    "tier": "Silver",
    "season_points": 21.3,
    "total_points": 21.3,
    "seasons": [{"season": "Season 1", "active": True, "points_visible": True, "points": 21.3}],
    "weekly": [
        {
            "season": "Season 1",
            "week_start": "2026-10-28",
            "distributed_at": "2026-10-28T19:00:00.000Z",
            "points": 21.3,
        }
    ],
    "next_drop_at": "2026-11-04T20:00:00.000Z",
}

# The two real boundaries, an hour apart in UTC because the schedule follows a US
# local time across the DST change.
_BOUNDS = [datetime(2026, 10, 28, 19, tzinfo=UTC), datetime(2026, 11, 4, 20, tzinfo=UTC)]


def test_points_response_exposes_the_weekly_drops():
    from clients.arcus import ArcusPoints

    points = ArcusPoints(**_POINTS)

    assert points.season_points == Decimal("21.3")
    assert len(points.weekly) == 1
    assert points.weekly[0].distributed_at == datetime(2026, 10, 28, 19, tzinfo=UTC)
    assert points.weekly[0].points == Decimal("21.3")
    # The pending drop counts as a boundary, so the current week can still be bucketed.
    assert points.drop_boundaries() == _BOUNDS


def test_drop_boundaries_are_sorted_and_deduped():
    from clients.arcus import ArcusPoints

    points = ArcusPoints(
        next_drop_at=datetime(2026, 10, 28, 19, tzinfo=UTC),
        weekly=[
            {"distributed_at": "2026-11-04T20:00:00Z", "points": 1},
            {"distributed_at": "2026-10-28T19:00:00Z", "points": 2},
            {"points": 3},  # a drop with no timestamp must not break the schedule
        ],
    )

    assert points.drop_boundaries() == _BOUNDS


def test_a_drop_pays_the_window_that_ends_at_it():
    """Measured, not assumed.

    Over [D-7d, D) the 2026-10-28 drop puts all three accounts at 81.3-82.8 points per
    $1M (21.3/258k, 15.48/187k, 24.18/297k). Reading `week_start` as the start of the
    rewarded week instead scatters them over 164-256 per $1M, and would also mean the
    drop was paid before the week it rewards had happened.
    """
    from clients.arcus import points_week_window

    drop = datetime(2026, 10, 28, 19, tzinfo=UTC)
    start, end = points_week_window(_BOUNDS, drop - timedelta(microseconds=1))

    assert end == drop
    assert start == drop - timedelta(days=7)


def test_a_trade_at_the_drop_instant_belongs_to_the_next_window():
    # Windows are half-open: [start, end). The drop pays everything strictly before it.
    from clients.arcus import points_week_window

    before = points_week_window(_BOUNDS, datetime(2026, 10, 28, 18, 59, tzinfo=UTC))
    at = points_week_window(_BOUNDS, datetime(2026, 10, 28, 19, tzinfo=UTC))

    assert before[1] == datetime(2026, 10, 28, 19, tzinfo=UTC)
    assert at[0] == datetime(2026, 10, 28, 19, tzinfo=UTC)


def test_a_known_window_keeps_the_real_dst_stretched_length():
    """The grid is not a 7-day modulus: Arcus shifted 19:00Z -> 20:00Z over DST.

    Bucketing on a fixed modulus would misplace every trade in that extra hour.
    """
    from clients.arcus import POINTS_WEEK, points_week_window

    start, end = points_week_window(_BOUNDS, datetime(2026, 11, 1, 12, tzinfo=UTC))

    assert (start, end) == (_BOUNDS[0], _BOUNDS[1])
    assert end - start == POINTS_WEEK + timedelta(hours=1)


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 7, tzinfo=UTC),  # long before the first known drop
        datetime(2026, 10, 14, 19, tzinfo=UTC),
        datetime(2026, 10, 25, tzinfo=UTC),  # inside the known schedule
        datetime(2026, 11, 1, 12, tzinfo=UTC),
        datetime(2026, 11, 5, tzinfo=UTC),  # just after the last known drop
        datetime(2026, 11, 20, tzinfo=UTC),  # several weeks after it
    ],
)
def test_every_window_contains_its_moment_and_lasts_about_a_week(moment):
    from clients.arcus import POINTS_WEEK, points_week_window

    start, end = points_week_window(_BOUNDS, moment)

    assert start <= moment < end
    assert POINTS_WEEK <= end - start <= POINTS_WEEK + timedelta(hours=2)


def test_the_schedule_extends_past_both_ends_of_what_the_api_reports():
    from clients.arcus import points_week_label, points_week_window

    old = points_week_label(*points_week_window(_BOUNDS, datetime(2026, 10, 15, tzinfo=UTC)))
    new = points_week_label(*points_week_window(_BOUNDS, datetime(2026, 11, 20, tzinfo=UTC)))

    assert old == "2026-W43 Oct14-Oct21"
    assert new == "2026-W48 Nov18-Nov25"


def test_a_single_known_boundary_is_enough():
    # A fresh account has no weekly entries yet, only next_drop_at.
    from clients.arcus import points_week_label, points_week_window

    label = points_week_label(*points_week_window([_BOUNDS[1]], datetime(2026, 11, 1, tzinfo=UTC)))

    assert label == "2026-W45 Oct28-Nov04"


def test_window_labels_are_numbered_by_their_drop_and_sort_chronologically():
    from clients.arcus import points_week_label, points_week_window

    labels = [
        points_week_label(*points_week_window(_BOUNDS, m))
        for m in (
            datetime(2026, 10, 25, tzinfo=UTC),
            datetime(2026, 11, 1, tzinfo=UTC),
            datetime(2026, 11, 8, tzinfo=UTC),
        )
    ]

    assert labels == ["2026-W44 Oct21-Oct28", "2026-W45 Oct28-Nov04", "2026-W46 Nov04-Nov11"]
    assert sorted(labels) == labels  # what print_stats relies on to order rows
