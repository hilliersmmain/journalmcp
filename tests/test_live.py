"""Opt-in runs against the real systemd. Deselected by default; never in CI."""

from __future__ import annotations

import pytest

from journalmcp import core


@pytest.mark.live
def test_list_units_against_the_real_user_manager() -> None:
    result = core.list_units("user")
    assert result.scope == "user"
    assert len(result.units) > 0
    assert all(u.unit for u in result.units)


@pytest.mark.live
def test_journal_against_the_real_system_journal() -> None:
    result = core.journal("system", lines=5)
    assert len(result.entries) <= 5
    assert all(set(e) <= set(core.JOURNAL_FIELDS) for e in result.entries)


@pytest.mark.live
def test_system_summary_against_the_real_managers() -> None:
    result = core.system_summary()
    assert result.state in core.SYSTEM_STATES
    assert result.boot_time is not None
