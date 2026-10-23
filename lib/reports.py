# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | If it compiles, ship it
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from rich.console import Console

_KINDS = frozenset({"info", "stats"})


@dataclass
class _Snapshot:
    kind: str
    exchange: str
    chunks: list[str] = field(default_factory=list)


_snapshot: _Snapshot | None = None


def begin(kind: str, exchange: str) -> None:
    global _snapshot
    if kind not in _KINDS or not exchange:
        return
    _snapshot = _Snapshot(kind=kind, exchange=exchange)


def capture(renderable: object) -> None:
    if _snapshot is None:
        return
    console = Console(
        file=StringIO(),
        record=True,
        width=200,
        color_system=None,
        force_terminal=True,
    )
    console.print(renderable)
    text = console.export_text().rstrip()
    if text:
        _snapshot.chunks.append(text)


def flush(root: Path | None = None) -> Path | None:
    global _snapshot
    snap = _snapshot
    _snapshot = None
    if snap is None or not snap.chunks:
        return None

    folder = (root or Path(".")) / f"{snap.kind}_data"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{snap.exchange}.txt"
    path.write_text("\n\n".join(snap.chunks) + "\n")
    return path.resolve()
