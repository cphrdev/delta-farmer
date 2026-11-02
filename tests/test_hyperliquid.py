from decimal import Decimal

from clients.entropy import EntropyClient
from clients.hyperliquid import HyperLiquidClient
from clients.jumper import JumperClient
from lib.cli import _scope_markets
from strategy.models import AppConfig

KEY = "0x" + "03" * 32

# Positions as they sit on one wallet while several HIP-3 apps trade it at once:
# jumper on the xyz dex, entropy on io, plus a native HL coin.
WALLET = {
    "": [("BTC", "1")],
    "xyz": [("xyz:SP500", "2")],
    "io": [("io:NBIS", "3")],
    "hyna": [("hyna:ETH", "4")],
}


def _fake_info(client, seen: list[str | None]):
    async def info(**kwargs):
        if kwargs.get("type") == "perpDexs":
            return [None, {"name": "xyz"}, {"name": "io"}, {"name": "hyna"}]
        dex = kwargs.get("dex", "")
        seen.append(dex)
        if kwargs.get("type") == "openOrders":
            return [{"oid": i, "coin": c} for i, (c, _) in enumerate(WALLET.get(dex, []))]
        return {
            "assetPositions": [
                {"position": {"coin": coin, "szi": szi, "entryPx": "100"}}
                for coin, szi in WALLET.get(dex, [])
            ]
        }

    client._info = info
    return seen


def test_an_unscoped_client_still_sweeps_every_dex():
    # Legacy behaviour, kept for a bare client nothing has scoped.
    client = HyperLiquidClient("test", KEY)

    assert client._dex_scope is None


async def test_scope_limits_the_sweep_to_the_configured_dex():
    client = JumperClient("test", KEY)
    seen = _fake_info(client, [])

    client.scope_markets(["xyz:SP500"])

    assert await client._dex_names() == ["xyz"]
    assert [p.symbol for p in await client.positions()] == ["xyz:SP500"]
    assert seen == ["xyz"]  # io and native were never even queried


async def test_a_scoped_client_leaves_another_apps_positions_alone():
    """The regression: jumper's cleanup must not close entropy's io: positions.

    Both apps run on the same Hyperliquid wallet. jumper has no dex_prefix, so
    before scoping it enumerated every dex and closed everything it found.
    """
    client = JumperClient("test", KEY)
    _fake_info(client, [])
    closed: list[str] = []

    async def close_position(position):
        closed.append(position.symbol)
        return True

    client.close_position = close_position
    client.scope_markets(["xyz:SP500"])
    count = await client.close_all_positions()

    assert closed == ["xyz:SP500"]
    assert count == 1
    assert "io:NBIS" not in closed and "BTC" not in closed and "hyna:ETH" not in closed


async def test_an_unscoped_client_closes_everything_on_the_wallet():
    # Documents what the bug looked like, so a regression is visible as a diff here.
    client = JumperClient("test", KEY)
    _fake_info(client, [])
    closed: list[str] = []

    async def close_position(position):
        closed.append(position.symbol)
        return True

    client.close_position = close_position
    await client.close_all_positions()

    assert sorted(closed) == ["BTC", "hyna:ETH", "io:NBIS", "xyz:SP500"]


async def test_scope_covers_the_whole_dex_so_orphans_are_still_cleaned():
    # Scoping by dex, not by exact symbol: a position left on xyz by an earlier
    # config must still be recoverable by a client now trading only xyz:SP500.
    client = JumperClient("test", KEY)

    async def info(**kwargs):
        if kwargs.get("type") == "perpDexs":
            return [None, {"name": "xyz"}]
        return {
            "assetPositions": [{"position": {"coin": "xyz:XYZ100", "szi": "5", "entryPx": "10"}}]
        }

    client._info = info
    client.scope_markets(["xyz:SP500"])

    assert [p.symbol for p in await client.positions()] == ["xyz:XYZ100"]


async def test_orders_are_cancelled_only_on_the_scoped_dex():
    client = JumperClient("test", KEY)
    seen = _fake_info(client, [])
    sent: list[dict] = []

    async def run(action, params):
        sent.append({"action": action, **params})
        return {"status": "submitted"}

    client._run = run
    client.scope_markets(["xyz:SP500"])

    assert await client.cancel_all_orders() == 1
    assert seen == ["xyz"]
    assert sent == [{"action": "cancelOrder", "ids": ["0"]}]


async def test_a_dex_prefixed_client_is_unaffected_by_scope():
    # entropy was never exposed to this bug: dex_prefix already pins the sweep.
    client = EntropyClient("test", KEY)
    seen = _fake_info(client, [])

    client.scope_markets(["xyz:SP500"])  # a wrong scope must not widen or move it

    assert await client._dex_names() == ["io"]
    assert [p.symbol for p in await client.positions()] == ["io:NBIS"]
    assert seen == ["io"]


def test_native_symbols_scope_to_native_markets_only():
    client = HyperLiquidClient("test", KEY)

    client.scope_markets(["BTC", "ETH"])

    assert client._dex_scope == frozenset({""})


def test_a_mixed_scope_keeps_every_configured_dex():
    client = JumperClient("test", KEY)

    client.scope_markets(["BTC", "xyz:SP500", "io:NBIS"])

    assert client._dex_scope == frozenset({"", "xyz", "io"})


async def test_a_short_position_is_reported_as_an_ask_to_close():
    # Guards the szi -> side/size mapping that close_position inverts.
    client = JumperClient("test", KEY)

    async def info(**kwargs):
        return {
            "assetPositions": [{"position": {"coin": "xyz:SP500", "szi": "-2.5", "entryPx": "100"}}]
        }

    client._info = info
    client.scope_markets(["xyz:SP500"])
    position = (await client.positions())[0]

    assert position.side == "ask"
    assert position.size == Decimal("2.5")


def test_all_symbols_unions_pools_and_legacy_strategy():
    pooled = AppConfig.model_validate(
        {
            "accounts": [{"name": n, "privkey": "secret"} for n in ("one", "two", "three", "four")],
            "pools": {
                "a": {
                    "accounts": ["one", "two"],
                    "symbols": ["xyz:SP500", "BTC"],
                    "trade_size_usd": [100, 100],
                    "trade_duration": [60, 60],
                    "trade_cooldown": [10, 10],
                },
                "b": {
                    "accounts": ["three", "four"],
                    "symbols": ["BTC", "io:NBIS"],
                    "trade_size_usd": [100, 100],
                    "trade_duration": [60, 60],
                    "trade_cooldown": [10, 10],
                },
            },
        }
    )
    legacy = AppConfig.model_validate(
        {
            "accounts": [{"name": n, "privkey": "secret"} for n in ("one", "two")],
            "symbols": ["xyz:SP500"],
            "trade_size_usd": [100, 100],
            "trade_duration": [60, 60],
            "trade_cooldown": [10, 10],
        }
    )

    assert pooled.all_symbols == ["xyz:SP500", "BTC", "io:NBIS"]  # deduped, config order
    assert legacy.all_symbols == ["xyz:SP500"]


# MARK: opt-in wiring


def _cfg(**extra) -> AppConfig:
    return AppConfig.model_validate(
        {
            "accounts": [{"name": n, "privkey": "secret"} for n in ("one", "two")],
            "symbols": ["xyz:SP500"],
            "trade_size_usd": [100, 100],
            "trade_duration": [60, 60],
            "trade_cooldown": [10, 10],
            **extra,
        }
    )


def test_cleanup_scoping_is_off_by_default():
    """Default keeps the long-standing behaviour: cleanup sweeps the whole wallet."""
    clients = [JumperClient("test", KEY)]

    _scope_markets(clients, _cfg())

    assert clients[0]._dex_scope is None


def test_cleanup_scoping_applies_when_opted_in():
    clients = [JumperClient("test", KEY)]

    _scope_markets(clients, _cfg(scope_cleanup=True))

    assert clients[0]._dex_scope == frozenset({"xyz"})


async def test_opting_in_is_what_spares_the_other_apps_positions():
    # Same wallet, same cleanup call: the flag is the only difference.
    outcomes = {}
    for scope_cleanup in (False, True):
        client = JumperClient("test", KEY)
        _fake_info(client, [])
        closed: list[str] = []

        async def close_position(position, closed=closed):
            closed.append(position.symbol)
            return True

        client.close_position = close_position
        _scope_markets([client], _cfg(scope_cleanup=scope_cleanup))
        await client.close_all_positions()
        outcomes[scope_cleanup] = sorted(closed)

    assert outcomes[False] == ["BTC", "hyna:ETH", "io:NBIS", "xyz:SP500"]
    assert outcomes[True] == ["xyz:SP500"]


def test_the_flag_defaults_off_on_a_pooled_config_too():
    cfg = AppConfig.model_validate(
        {
            "accounts": [{"name": n, "privkey": "secret"} for n in ("one", "two")],
            "pools": {
                "a": {
                    "accounts": ["one", "two"],
                    "symbols": ["xyz:SP500"],
                    "trade_size_usd": [100, 100],
                    "trade_duration": [60, 60],
                    "trade_cooldown": [10, 10],
                }
            },
        }
    )

    assert cfg.scope_cleanup is False
