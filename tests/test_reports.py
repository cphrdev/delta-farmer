from decimal import Decimal
from unittest.mock import Mock

import pytest

from lib import reports
from lib.cli import create_cli, run_app
from lib.errors import AppError
from lib.table import AutoTable, Column, PeriodRow, render_stats


@pytest.fixture(autouse=True)
def _reset_reports():
    reports.flush()
    yield
    reports.flush()


def test_info_snapshot_overwrites_last_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reports.begin("info", "omni")
    tbl = AutoTable(Column("Account", justify="left"), Column("Balance", "{:.2f}"))
    tbl.add_row("acc1", 12.5)
    tbl.print()
    path = reports.flush()

    assert path == tmp_path / "info_data" / "omni.txt"
    first = path.read_text()
    assert "acc1" in first
    assert "12.50" in first

    reports.begin("info", "omni")
    tbl = AutoTable(Column("Account", justify="left"), Column("Balance", "{:.2f}"))
    tbl.add_row("acc2", 99)
    tbl.print()
    reports.flush()

    second = path.read_text()
    assert "acc2" in second
    assert "99.00" in second
    assert "acc1" not in second


def test_stats_snapshot_uses_stats_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reports.begin("stats", "nado")
    render_stats(
        {"W01": [PeriodRow("acc1", 2, Decimal(1000), Decimal(1), Decimal(3), Decimal("0.5"))]},
        ["W01"],
        fees=False,
    )
    path = reports.flush()

    assert path == tmp_path / "stats_data" / "nado.txt"
    text = path.read_text()
    assert "acc1" in text
    assert "W01" in text


def test_print_without_begin_does_not_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    AutoTable(Column("Account", justify="left")).print()
    assert reports.flush() is None
    assert not (tmp_path / "info_data").exists()
    assert not (tmp_path / "stats_data").exists()


async def test_create_cli_starts_info_snapshot(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lib.cli.telemetry.init", Mock())
    monkeypatch.setattr(
        "sys.argv",
        ["omni", "-c", str(tmp_path / "missing.toml"), "info"],
    )

    args = await create_cli("omni", str(tmp_path / "missing.toml"), ["privkey"])

    assert args.command == "info"
    AutoTable(Column("Account", justify="left")).add_row("only")
    tbl = AutoTable(Column("Account", justify="left"))
    tbl.add_row("only")
    tbl.print()
    path = reports.flush()
    assert path == tmp_path / "info_data" / "omni.txt"
    assert "only" in path.read_text()


def test_run_app_flushes_snapshot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def main():
        reports.begin("info", "rise")
        tbl = AutoTable(Column("Account", justify="left"))
        tbl.add_row("acc")
        tbl.print()

    run_app(main())
    assert (tmp_path / "info_data" / "rise.txt").read_text().find("acc") >= 0


def test_run_app_flushes_after_app_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def main():
        reports.begin("stats", "pacifica")
        tbl = AutoTable(Column("Account", justify="left"))
        tbl.add_row("acc")
        tbl.print()
        raise AppError("boom")

    run_app(main())
    assert "acc" in (tmp_path / "stats_data" / "pacifica.txt").read_text()
