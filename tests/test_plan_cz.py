"""Unit tests for the desktop launcher's pre-flight overview — the parts that
decide what the user is told: server grouping, priority ordering, and which
channels land in the sync queue. Rendering itself is left untested; it is
formatting, and a golden-file test on a box-drawing layout is pure churn."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

import plan_cz  # noqa: E402


def _touch(p: Path) -> Path:
    p.write_text("{}")
    return p


# ------------------------------------------------------------------------ plural


@pytest.mark.parametrize("n,expected", [
    (0, "souborů"), (1, "soubor"), (2, "soubory"), (4, "soubory"),
    (5, "souborů"), (21, "souborů"),
])
def test_plural_czech_thresholds(n: int, expected: str) -> None:
    assert plan_cz.plural(n, "soubor", "soubory", "souborů") == expected


# ----------------------------------------------------------------------- server_of


def test_server_read_from_existing_export(tmp_path: Path) -> None:
    _touch(tmp_path / "Outlands Community - COMMUNITY - pvm [42].json")
    assert plan_cz.server_of(tmp_path, "42", "oc-pvm", None) == "Outlands Community"


def test_server_explicit_key_wins_over_archive(tmp_path: Path) -> None:
    _touch(tmp_path / "Stale Name - C - pvm [42].json")
    assert plan_cz.server_of(tmp_path, "42", "oc-pvm", "Real Server") == "Real Server"


def test_server_falls_back_to_oc_prefix(tmp_path: Path) -> None:
    assert plan_cz.server_of(tmp_path, "42", "oc-taming", None) == "Outlands Community"
    assert plan_cz.server_of(tmp_path, "42", "taming", None) == "UO Outlands"


# ------------------------------------------------------------- collect / grouped


def _cfg(tmp_path: Path) -> dict:
    return {
        "output_dir": str(tmp_path),
        "channels": {
            "oc-pvm": {"id": "1520269586132500530"},
            "pvm": {"id": "529041672999403554"},
            "oc-new": {"id": "1520906212319695108"},
        },
    }


def test_collect_flags_stale_and_fresh_channels(tmp_path: Path) -> None:
    today = date.today()
    old = today - timedelta(days=30)
    _touch(tmp_path / f"Outlands Community - C - pvm [1520269586132500530] (after {old}).json")
    _touch(tmp_path / f"UO Outlands - C - pvm [529041672999403554] (after {today}).json")

    _, rows = plan_cz.collect(_cfg(tmp_path), tmp_path / "channels.yaml")
    by_name = {r["name"]: r for r in rows}

    assert by_name["oc-pvm"]["todo"] is True
    assert by_name["oc-pvm"]["behind"] == 30
    # Already synced through today — dce sync would skip it, so must the plan.
    assert by_name["pvm"]["todo"] is False
    # Registered but never downloaded.
    assert by_name["oc-new"]["todo"] is True
    assert by_name["oc-new"]["last"] is None


def test_collect_counts_a_full_export_without_an_after_marker(tmp_path: Path) -> None:
    """A file with no `(after X)` gives dce nothing to resume from: the channel
    still needs a full re-pull, but the archive is not empty. The overview
    distinguishes the two, so the row data has to as well."""
    _touch(tmp_path / "Outlands Community - C - pvm [1520269586132500530].json")
    _, rows = plan_cz.collect(_cfg(tmp_path), tmp_path / "channels.yaml")
    row = next(r for r in rows if r["name"] == "oc-pvm")
    assert row["last"] is None
    assert row["files"] == 1
    assert row["todo"] is True


def test_priority_server_leads_the_groups(tmp_path: Path) -> None:
    _touch(tmp_path / "Outlands Community - C - pvm [1520269586132500530].json")
    _touch(tmp_path / "UO Outlands - C - pvm [529041672999403554].json")
    _, rows = plan_cz.collect(_cfg(tmp_path), tmp_path / "channels.yaml")

    groups = plan_cz.grouped(rows, "Outlands Community")
    assert groups[0][0] == "Outlands Community"
    assert [g[0] for g in groups[1:]] == sorted(g[0] for g in groups[1:])

    # Priority is a setting, not a constant baked into the ordering.
    assert plan_cz.grouped(rows, "UO Outlands")[0][0] == "UO Outlands"


def test_emit_plan_lists_only_stale_channels(tmp_path: Path, capsys) -> None:
    today = date.today()
    _touch(tmp_path / f"UO Outlands - C - pvm [529041672999403554] (after {today}).json")
    plan_cz.emit_plan(_cfg(tmp_path), tmp_path / "channels.yaml", "Outlands Community")

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("Outlands Community\t")
    assert "oc-pvm" in lines[0] and "oc-new" in lines[0]
    # `pvm` is current, so it must not reach the sync queue at all.
    assert all("pvm\t" not in ln.replace("oc-pvm", "") for ln in lines[1:])


def test_target_example_shows_the_incremental_marker(tmp_path: Path) -> None:
    _touch(tmp_path / "Outlands Community - COMMUNITY - pvm [1520269586132500530] (after 2026-07-21).json")
    name = plan_cz.target_example(tmp_path, "1520269586132500530", "oc-pvm", date(2026, 7, 21),
                                 date(2026, 8, 11))
    assert name == ("Outlands Community - COMMUNITY - pvm [1520269586132500530] "
                    "(after 2026-07-21) (pulled 2026-08-11).json")
