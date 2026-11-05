"""Snowflake ids: layout, ordering, the clock going backwards, and refusing to guess.

The generator is driven through an injected clock throughout. A real clock cannot
produce "the same millisecond twice" or "time went backwards" on demand, and those
are the two cases an id generator exists to survive.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

import py_common.utils.snowflake as snowflake
from py_common.config import SnowflakeSettings
from py_common.utils import (
    SnowflakeGenerator,
    configure_snowflake,
    new_snowflake_id,
    snowflake_timestamp_ms,
)
from py_common.utils.snowflake import DEFAULT_EPOCH_MS, MAX_SEQUENCE


@pytest.fixture(autouse=True)
def _unconfigured() -> Any:
    """The process-wide generator is module state; leave it as found."""
    saved = snowflake._default
    snowflake._default = None
    yield
    snowflake._default = saved


def test_ids_grow_and_carry_their_time_and_worker() -> None:
    clock = iter([DEFAULT_EPOCH_MS + 10, DEFAULT_EPOCH_MS + 10, DEFAULT_EPOCH_MS + 11])
    gen = SnowflakeGenerator(3, clock=lambda: next(clock))
    a, b, c = gen.next_id(), gen.next_id(), gen.next_id()

    assert a < b < c
    assert snowflake_timestamp_ms(a) == DEFAULT_EPOCH_MS + 10
    assert snowflake_timestamp_ms(c) == DEFAULT_EPOCH_MS + 11
    assert (a >> 12) & 0x3FF == 3
    assert (b & MAX_SEQUENCE) == 1  # second id in the same millisecond


def test_the_bit_layout_is_pinned() -> None:
    """41 bits of time, 10 of worker, 12 of sequence -- fixed by value.

    Ids already issued are stored in databases; a layout drift would not break a
    test of relative order, only every row written before it.
    """
    gen = SnowflakeGenerator(5, clock=lambda: DEFAULT_EPOCH_MS + 1000)
    assert gen.next_id() == (1000 << 22) | (5 << 12) | 0


def test_the_default_epoch_continues_an_existing_id_space() -> None:
    """2026-01-01T00:00:00Z: the epoch thousand-lives issued its ids from, which is
    why moving here does not change anything it has already stored."""
    assert DEFAULT_EPOCH_MS == 1_767_225_600_000


def test_a_custom_epoch_is_honoured_both_ways() -> None:
    epoch = 1_000_000_000_000
    gen = SnowflakeGenerator(1, epoch_ms=epoch, clock=lambda: epoch + 7)
    issued = gen.next_id()

    assert issued >> 22 == 7
    assert snowflake_timestamp_ms(issued, epoch_ms=epoch) == epoch + 7


def test_ids_stay_positive_for_a_bigint_column() -> None:
    gen = SnowflakeGenerator(1023, clock=lambda: DEFAULT_EPOCH_MS + (1 << 41) - 1)
    assert 0 < gen.next_id() < 1 << 63


def test_clock_going_backwards_never_repeats_an_id() -> None:
    times = iter([DEFAULT_EPOCH_MS + 100, DEFAULT_EPOCH_MS + 50, DEFAULT_EPOCH_MS + 50])
    gen = SnowflakeGenerator(0, clock=lambda: next(times))
    ids = [gen.next_id() for _ in range(3)]

    assert len(set(ids)) == 3
    assert ids == sorted(ids)


def test_sequence_overflow_waits_for_the_next_millisecond() -> None:
    state = {"now": DEFAULT_EPOCH_MS + 5, "calls": 0}

    def clock() -> int:
        state["calls"] += 1
        if state["calls"] > MAX_SEQUENCE + 2:
            state["now"] = DEFAULT_EPOCH_MS + 6
        return state["now"]

    gen = SnowflakeGenerator(0, clock=clock)
    ids = [gen.next_id() for _ in range(MAX_SEQUENCE + 2)]

    assert len(set(ids)) == len(ids)
    assert snowflake_timestamp_ms(ids[-1]) == DEFAULT_EPOCH_MS + 6


def test_unique_across_threads() -> None:
    gen = SnowflakeGenerator(1)
    out: list[int] = []
    lock = threading.Lock()

    def work() -> None:
        local = [gen.next_id() for _ in range(2000)]
        with lock:
            out.extend(local)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(out)) == 8000


@pytest.mark.parametrize("worker_id", [-1, 1024])
def test_worker_id_is_bounded(worker_id: int) -> None:
    with pytest.raises(ValueError, match="worker_id"):
        SnowflakeGenerator(worker_id)


def test_an_unconfigured_process_refuses_to_guess() -> None:
    """Defaulting to worker 0 would let two forgetful processes collide silently."""
    with pytest.raises(RuntimeError, match="configure_snowflake"):
        new_snowflake_id()


def test_configure_then_new_ids_use_that_worker() -> None:
    configure_snowflake(9)
    a, b = new_snowflake_id(), new_snowflake_id()

    assert a < b
    assert (a >> 12) & 0x3FF == 9


def test_the_settings_group_bounds_the_worker_id() -> None:
    assert SnowflakeSettings().worker_id == 0
    assert SnowflakeSettings(worker_id=1023).worker_id == 1023
    with pytest.raises(ValueError):
        SnowflakeSettings(worker_id=1024)
