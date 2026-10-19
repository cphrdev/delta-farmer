# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License
import asyncio
from collections.abc import Awaitable, Callable, Sequence

from lib.errors import AppError

from .models import TradingClient


def _unique_exchange_clients(accs: Sequence[TradingClient]) -> list[TradingClient]:
    by_exchange: dict[str, TradingClient] = {}
    for acc in accs:
        by_exchange.setdefault(acc.exchange, acc)
    return list(by_exchange.values())


async def _run_exchange_symbols[T](
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
    fn: Callable[[TradingClient, str], Awaitable[T]],
) -> list[tuple[TradingClient, str, T | BaseException]]:
    unique_symbols = list(dict.fromkeys(symbols))
    pairs = [(acc, symbol) for acc in _unique_exchange_clients(accs) for symbol in unique_symbols]
    values = await asyncio.gather(
        *[fn(acc, symbol) for acc, symbol in pairs],
        return_exceptions=True,
    )

    results: list[tuple[TradingClient, str, T | BaseException]] = []
    for (acc, symbol), value in zip(pairs, values, strict=True):
        if isinstance(value, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise value
        results.append((acc, symbol, value))
    return results


async def ensure_listed_symbols(
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
) -> None:
    unique_symbols = list(dict.fromkeys(symbols))
    clients = _unique_exchange_clients(accs)
    values = await asyncio.gather(
        *[acc.get_symbols() for acc in clients],
        return_exceptions=True,
    )
    missing: set[tuple[str, str]] = set()
    for acc, value in zip(clients, values, strict=True):
        if isinstance(value, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise value
        if isinstance(value, Exception):
            raise AppError(
                f"Failed to check symbols on {acc.exchange}: {type(value).__name__}: {value}"
            ) from value
        listed = set(value)
        missing.update(
            (acc.exchange, symbol) for symbol in unique_symbols if symbol not in listed
        )
    if missing:
        details = "; ".join(
            f"{symbol} is not available on {exchange}" for exchange, symbol in sorted(missing)
        )
        raise AppError(f"Invalid configured symbols: {details}")


async def ensure_exchange_symbols(
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
    predicate: Callable[[TradingClient, str], Awaitable[bool]],
) -> None:
    results = await _run_exchange_symbols(accs, symbols, predicate)
    for acc, symbol, value in results:
        if isinstance(value, Exception):
            raise AppError(
                f"Failed to check {symbol} on {acc.exchange}: {type(value).__name__}: {value}"
            ) from value
    failed = {
        (acc.exchange, symbol) for acc, symbol, value in results if value is not True
    }
    if failed:
        details = "; ".join(
            f"{symbol} is not available on {exchange}" for exchange, symbol in sorted(failed)
        )
        raise AppError(f"Invalid configured symbols: {details}")


async def filter_exchange_symbols(
    accs: Sequence[TradingClient],
    symbols: Sequence[str],
    predicate: Callable[[TradingClient, str], Awaitable[bool]],
) -> list[str]:
    unique_symbols = list(dict.fromkeys(symbols))
    results = await _run_exchange_symbols(accs, unique_symbols, predicate)
    for acc, symbol, value in results:
        if isinstance(value, Exception):
            raise AppError(
                f"Failed to check {symbol} on {acc.exchange}: {type(value).__name__}: {value}"
            ) from value

    failed = {symbol for _acc, symbol, value in results if value is not True}
    return [symbol for symbol in unique_symbols if symbol not in failed]
