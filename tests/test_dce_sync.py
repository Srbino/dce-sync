"""Unit tests for dce_sync helpers — focused on filename parsing and channel
target resolution. Subprocess-driving paths (sync, discover, upgrade-check)
need a live DCE.Cli + token and are left to the CI smoke test."""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import pytest

import dce_sync


# ---------------------------------------------------------------- parse_last_after


def _touch(p: Path, content: str = "{}") -> Path:
    p.write_text(content)
    return p


def test_parse_last_after_legacy_filename(tmp_path: Path) -> None:
    _touch(tmp_path / "G - C [123] (after 2026-05-11).json")
    assert dce_sync.parse_last_after(tmp_path, "123") == date(2026, 5, 11)


def test_parse_last_after_pulled_stamp(tmp_path: Path) -> None:
    _touch(tmp_path / "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json")
    assert dce_sync.parse_last_after(tmp_path, "123") == date(2026, 5, 11)


def test_parse_last_after_picks_latest(tmp_path: Path) -> None:
    _touch(tmp_path / "G - C [123] (after 2026-04-01).json")
    _touch(tmp_path / "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json")
    _touch(tmp_path / "G - C [123] (after 2026-03-15).json")
    assert dce_sync.parse_last_after(tmp_path, "123") == date(2026, 5, 11)


def test_parse_last_after_other_channel_ignored(tmp_path: Path) -> None:
    _touch(tmp_path / "G - C [999] (after 2026-05-11).json")
    assert dce_sync.parse_last_after(tmp_path, "123") is None


def test_parse_last_after_empty_dir(tmp_path: Path) -> None:
    assert dce_sync.parse_last_after(tmp_path, "123") is None


def test_parse_last_after_missing_dir(tmp_path: Path) -> None:
    assert dce_sync.parse_last_after(tmp_path / "does-not-exist", "123") is None


def test_parse_last_after_skips_non_after_files(tmp_path: Path) -> None:
    # Manually-named file with channel id but no (after) marker
    _touch(tmp_path / "G - C [123].json")
    assert dce_sync.parse_last_after(tmp_path, "123") is None


# ---------------------------------------------------------------- _stamp_pulled_date


def test_stamp_adds_pulled_suffix(tmp_path: Path) -> None:
    src = _touch(tmp_path / "G - C [123] (after 2026-05-11).json")
    dce_sync._stamp_pulled_date(tmp_path, "123", date(2026, 5, 20))
    assert not src.exists()
    stamped = tmp_path / "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json"
    assert stamped.exists()


def test_stamp_idempotent_on_already_stamped(tmp_path: Path) -> None:
    name = "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json"
    src = _touch(tmp_path / name)
    dce_sync._stamp_pulled_date(tmp_path, "123", date(2026, 5, 20))
    assert src.exists()
    # No double-stamp
    assert "(pulled 2026-05-20) (pulled" not in src.name
    assert list(tmp_path.iterdir()) == [src]


def test_stamp_replaces_same_day_snapshot(tmp_path: Path) -> None:
    # Older same-day pulled file already there.
    old = _touch(tmp_path / "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json",
                 "OLD")
    new = _touch(tmp_path / "G - C [123] (after 2026-05-11).json", "NEW")
    # Force a clear mtime gap so `newest` is unambiguous.
    past = time.time() - 60
    os.utime(old, (past, past))
    dce_sync._stamp_pulled_date(tmp_path, "123", date(2026, 5, 20))
    target = tmp_path / "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json"
    assert target.exists()
    assert target.read_text() == "NEW"


def test_stamp_noop_when_no_channel_file(tmp_path: Path) -> None:
    _touch(tmp_path / "G - C [999] (after 2026-05-11).json")
    dce_sync._stamp_pulled_date(tmp_path, "123", date(2026, 5, 20))
    # Untouched
    assert (tmp_path / "G - C [999] (after 2026-05-11).json").exists()


# ---------------------------------------------------------------- _AFTER_RX (merge target)


def test_after_rx_matches_legacy() -> None:
    m = dce_sync._AFTER_RX.match("G - C [123] (after 2026-05-11).json")
    assert m is not None
    assert m.group("date") == "2026-05-11"
    assert m.group("pulled") is None


def test_after_rx_matches_pulled() -> None:
    m = dce_sync._AFTER_RX.match(
        "G - C [123] (after 2026-05-11) (pulled 2026-05-20).json")
    assert m is not None
    assert m.group("date") == "2026-05-11"
    assert m.group("pulled") == " (pulled 2026-05-20)"


# ---------------------------------------------------------------- _expand_channel_targets


def test_expand_passthrough_plain_names() -> None:
    channels = {"pvm": {"id": "1"}, "taming": {"id": "2"}}
    assert dce_sync._expand_channel_targets(["pvm"], channels) == ["pvm"]


def test_expand_glob_matches_subset() -> None:
    channels = {"pvm": {"id": "1"}, "taming": {"id": "2"},
                "ships-pvm": {"id": "3"}}
    result = dce_sync._expand_channel_targets(["*pvm*"], channels)
    assert set(result) == {"pvm", "ships-pvm"}


def test_expand_dedup_across_overlapping_specs() -> None:
    channels = {"pvm": {"id": "1"}, "taming": {"id": "2"}}
    result = dce_sync._expand_channel_targets(["pvm", "*"], channels)
    assert result.count("pvm") == 1
    assert set(result) == {"pvm", "taming"}


def test_expand_empty_glob_match_dies() -> None:
    channels = {"pvm": {"id": "1"}}
    with pytest.raises(SystemExit):
        dce_sync._expand_channel_targets(["nope*"], channels)


def test_expand_plain_unknown_passes_through() -> None:
    # Plain names are NOT validated by the helper -- downstream code checks
    # for unknowns and reports the typo cleanly.
    channels = {"pvm": {"id": "1"}}
    assert dce_sync._expand_channel_targets(["typo"], channels) == ["typo"]


# ---------------------------------------------------------------- parse_since / parse_until


def test_parse_since_units() -> None:
    today = date.today()
    assert (today - dce_sync.parse_since("7d")).days == 7
    assert (today - dce_sync.parse_since("3w")).days == 21
    assert (today - dce_sync.parse_since("2m")).days == 60
    assert (today - dce_sync.parse_since("1y")).days == 365


def test_parse_since_rejects_bad_unit() -> None:
    with pytest.raises(SystemExit):
        dce_sync.parse_since("3z")


def test_parse_since_rejects_zero() -> None:
    with pytest.raises(SystemExit):
        dce_sync.parse_since("0d")


def test_parse_until_valid() -> None:
    assert dce_sync.parse_until("2026-05-20") == date(2026, 5, 20)


def test_parse_until_rejects_bad_format() -> None:
    with pytest.raises(SystemExit):
        dce_sync.parse_until("20.5.2026")
