# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Ship it, then read the docs
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from clients.arcus import (
    ArcusClient,
    ArcusFill,
    ArcusFunding,
    ArcusPoints,
    points_week_label,
    points_week_window,
)
from lib.cli import create_cli, noop_command, run_app, setup_app
from lib.http import ApiError
from lib.logger import logger
from lib.store import DataStore
from lib.table import AutoTable, Column, PeriodRow, render_stats
from lib.utils import gather_accs, parse_filter, short_addr, to_period_day
from strategy import AppConfig
from strategy.runner import close_all, print_positions, run_strategy

# MARK: Sync


async def sync_fills(acc: ArcusClient, ttl: int) -> list[ArcusFill]:
    store_path = f".cache/arcus_{short_addr(acc.address)}_fills.pkl"
    store = DataStore(store_path, id_key="tradeId", model=ArcusFill)
    await store.sync(lambda since: acc.fills(since=since), ttl_sec=ttl)
    return store.get_all()


async def sync_funding(acc: ArcusClient, ttl: int) -> list[ArcusFunding]:
    store_path = f".cache/arcus_{short_addr(acc.address)}_funding.pkl"
    store = DataStore(store_path, id_key="id", model=ArcusFunding)
    await store.sync(lambda since: acc.funding_payments(since=since), ttl_sec=ttl)
    return store.get_all()


async def load_points(acc: ArcusClient) -> ArcusPoints:
    """Read points live, as `info` does: the weekly list is small and the drop
    schedule it carries is what the whole report is bucketed on.

    Points ride on Privy auth, which is independent of the trading reads, so a failure
    here must not cost the account its volume and burn rows.
    """
    try:
        return await acc.points()
    except Exception as e:
        logger.warning(f"{acc.name}: points unavailable: {e}")
        return ArcusPoints()


# MARK: Reports


async def print_info(accs: list[ArcusClient]):
    tbl = AutoTable(
        Column("", justify="left"),
        Column("Account", justify="left"),
        Column("Address", justify="left"),
        Column("Volume", "{:,.0f}", total=sum),
        Column("Burn", "{:,.2f}", total=sum),
        Column("Points", "{:,.2f}", total=sum),
        Column("P/Price", "{:,.2f}", compute=lambda r: r["Burn"] / r["Points"]),
        Column(
            "$/100k",
            "${:,.2f}",
            compute=lambda r: r["Burn"] / r["Volume"] * Decimal("1e5"),
            guard=lambda r: r["Volume"] > 0,
        ),
        Column("Balance", "{:,.2f}", total=sum),
        Column("Rank", "{:,.0f}"),
        Column("Tier", justify="left"),
    )

    async def row(acc: ArcusClient):
        addr = short_addr(acc.address)
        try:
            if not await acc.registered():
                return ("✗", acc.name, addr, 0, 0, 0, 0, None, None)
            p = await acc.profile()
            return ("✓", acc.name, p.addr, p.volume, -p.pnl, p.points, p.balance, p.rank, p.mode)
        except ApiError as e:
            # Arcus is access-gated: a wallet that was never granted access answers 403
            # on every endpoint. Report it rather than failing the whole table.
            logger.warning(f"{acc.name}: {e}")
            return ("✗", acc.name, addr, 0, 0, 0, 0, None, None)

    for r in await asyncio.gather(*[row(acc) for acc in accs]):
        tbl.add_row(*r)

    tbl.print()


async def print_stats(accs: list[ArcusClient], period="week", filter_period="all", force=False):
    """Per-period trading stats bucketed on Arcus's own points drops.

    Weeks are the drop windows the points API reports, not calendar weeks: drops land
    on Wednesdays around 19:00Z and each one pays the preceding seven days, so a
    Mon-Sun grid splits a single drop across two rows and makes P/Price meaningless.

    Burn is the negated net realized P/L. Fill `closedPnl` is already net of fees, and
    funding `payment` is signed with positive meaning received.
    """
    gcnt: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    gvol: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    gpnl: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    gfee: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    gpts: defaultdict[str, defaultdict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))

    ttl = 0 if force else 3600
    all_fills, all_funding, all_points = await asyncio.gather(
        gather_accs(accs, lambda acc: sync_fills(acc, ttl)),
        gather_accs(accs, lambda acc: sync_funding(acc, ttl)),
        gather_accs(accs, load_points),
    )

    # Every account shares one drop schedule, so pool the boundaries: an account that
    # has not been paid yet still gets bucketed on the others' drops.
    boundaries = sorted({b for points in all_points for b in points.drop_boundaries()})
    by_drop = period == "week" and bool(boundaries)
    if period == "week" and not boundaries:
        logger.warning("No points drop schedule available, falling back to daily grouping")

    def label(dt: datetime) -> str:
        if not by_drop:
            return to_period_day(dt)
        return points_week_label(*points_week_window(boundaries, dt))

    for acc, fills in zip(accs, all_fills):
        for fill in fills:
            pk = label(fill.created_at)
            gvol[pk][acc.name] += fill.volume
            gpnl[pk][acc.name] += fill.closedPnl
            gfee[pk][acc.name] += fill.fee
            gcnt[pk][acc.name] += 1

    for acc, payments in zip(accs, all_funding):
        for payment in payments:
            gpnl[label(payment.created_at)][acc.name] += payment.payment

    if by_drop:
        for acc, points in zip(accs, all_points):
            for week in points.weekly:
                if week.distributed_at is None:
                    continue
                # A drop pays the window that ENDS at distributed_at, and windows are
                # half-open, so step inside it rather than landing on the next one.
                pk = label(week.distributed_at - timedelta(microseconds=1))
                gpts[pk][acc.name] += week.points

    all_periods = sorted(gvol.keys() | gpnl.keys() | gpts.keys())
    periods_to_show = parse_filter(filter_period, all_periods)

    periods_data: dict[str, list[PeriodRow]] = {}
    for pk in all_periods:
        rows = []
        for name in [x.name for x in accs]:
            vol, pnl, pts = gvol[pk][name], gpnl[pk][name], gpts[pk][name]
            if not vol and not pnl and not pts:
                continue
            rows.append(PeriodRow(name, gcnt[pk][name], vol, -pnl, pts, gfee[pk][name]))
        periods_data[pk] = rows

    # Points exist per drop only, so a daily grid has nothing to put in the column.
    render_stats(periods_data, periods_to_show, points=by_drop, points_fmt="{:,.2f}")


# MARK: Main


async def main():
    cli = await create_cli("arcus", "configs/arcus.toml", ["privkey"])
    cfg = AppConfig.load(cli.config)

    all_accs, act_accs = await setup_app(cli, cfg, ArcusClient.from_config)

    match cli.command:
        case "info":
            await print_info(all_accs)
        case "close":
            await close_all(act_accs)
        case "trade":
            await run_strategy(cfg, act_accs, cli.pool)
        case "positions":
            await print_positions(act_accs)
        case "stats":
            await print_stats(all_accs, period=cli.group, filter_period=cli.filter, force=cli.force)
        case "deposit" | "withdraw":
            noop_command("Arcus", cli.command)


if __name__ == "__main__":
    run_app(main())
