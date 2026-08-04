"""Constant sanity tests for the fixed conventions in README.md."""

from __future__ import annotations

from nrecon.constants import (
    C_AIR,
    FS_HZ,
    METRES_PER_TAP,
    TS_NS,
    directed_links,
)


def test_metres_per_tap_matches_radar_map_exactly():
    assert METRES_PER_TAP == 299702547.0 / 998400000.0


def test_ts_ns_within_tolerance():
    assert abs(TS_NS - 1.0016026) < 1e-6


def test_directed_links_count():
    assert len(directed_links(5)) == 20


def test_directed_links_lexicographic_ordering():
    links = directed_links(5)
    assert links == sorted(links)


def test_directed_links_no_self_links():
    links = directed_links(5)
    assert all(tx != rx for tx, rx in links)
