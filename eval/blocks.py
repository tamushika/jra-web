"""Paired event-day block bootstrap for model comparisons."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Hashable, Iterator, Mapping, Sequence


def race_date_block(race_id: Any) -> str:
    """Extract the JST event-date block from the first eight race-id digits."""

    text = str(race_id)
    block = text[:8]
    if len(block) != 8 or not block.isdigit():
        raise ValueError(f"race_id does not start with YYYYMMDD: {text!r}")
    try:
        date(int(block[:4]), int(block[4:6]), int(block[6:8]))
    except ValueError as exc:
        raise ValueError(
            f"race_id does not start with a real JST date: {text!r}"
        ) from exc
    return block


def group_by_race_date(
    rows: Sequence[Any],
    race_id_getter: Callable[[Any], Any] | None = None,
) -> dict[str, list[Any]]:
    """Group rows without splitting races from the same JST event date."""

    if race_id_getter is None:
        def race_id_getter(row: Any) -> Any:
            if isinstance(row, Mapping):
                return row["race_id"]
            return getattr(row, "race_id")

    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[race_date_block(race_id_getter(row))].append(row)
    return dict(grouped)


@dataclass(frozen=True)
class BootstrapResult(Mapping[str, Any]):
    """Deterministic bootstrap result, available by attribute or mapping key."""

    observed_difference: float
    differences: tuple[float, ...]
    ci_low: float
    ci_high: float
    p_value: float
    confidence_level: float
    n_resamples: int
    n_blocks: int
    seed: int

    def _mapping(self) -> dict[str, Any]:
        return {
            "observed_difference": self.observed_difference,
            "differences": self.differences,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "p_value": self.p_value,
            "confidence_level": self.confidence_level,
            "n_resamples": self.n_resamples,
            "n_blocks": self.n_blocks,
            "seed": self.seed,
        }

    def __getitem__(self, key: str) -> Any:
        return self._mapping()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())


def _as_rows(block: Any) -> list[Any]:
    if isinstance(block, Mapping) or isinstance(block, (str, bytes)):
        return [block]
    try:
        return list(block)
    except TypeError:
        return [block]


def _normalise_paired_blocks(
    blocks_a: Mapping[Hashable, Any] | Sequence[Any],
    blocks_b: Mapping[Hashable, Any] | Sequence[Any],
) -> tuple[list[list[Any]], list[list[Any]]]:
    if isinstance(blocks_a, Mapping) != isinstance(blocks_b, Mapping):
        raise TypeError("blocks_a and blocks_b must use the same container type")
    if isinstance(blocks_a, Mapping):
        keys_a = set(blocks_a)
        keys_b = set(blocks_b)
        if keys_a != keys_b:
            missing_a = sorted((repr(key) for key in keys_b - keys_a))
            missing_b = sorted((repr(key) for key in keys_a - keys_b))
            raise ValueError(
                "paired block keys differ "
                f"(missing_a={missing_a}, missing_b={missing_b})"
            )
        keys = sorted(keys_a, key=lambda key: (type(key).__name__, repr(key)))
        normal_a = [_as_rows(blocks_a[key]) for key in keys]
        normal_b = [_as_rows(blocks_b[key]) for key in keys]
    else:
        if isinstance(blocks_a, (str, bytes)) or isinstance(blocks_b, (str, bytes)):
            raise TypeError("blocks must be a mapping or a sequence of blocks")
        if len(blocks_a) != len(blocks_b):
            raise ValueError("paired block sequences must have equal length")
        normal_a = [_as_rows(block) for block in blocks_a]
        normal_b = [_as_rows(block) for block in blocks_b]
    if not normal_a:
        raise ValueError("at least one paired block is required")
    return normal_a, normal_b


def _combine(blocks: Sequence[Sequence[Any]], indexes: Sequence[int]) -> list[Any]:
    combined: list[Any] = []
    for index in indexes:
        combined.extend(blocks[index])
    return combined


def _metric(metric_fn: Callable[[Sequence[Any]], float], rows: list[Any]) -> float:
    value = float(metric_fn(rows))
    if not math.isfinite(value):
        raise ValueError("metric_fn must return a finite number")
    return value


def _quantile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate a quantile of an empty sample")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return float(
        sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight
    )


def paired_block_bootstrap(
    metric_fn: Callable[[Sequence[Any]], float],
    blocks_a: Mapping[Hashable, Any] | Sequence[Any],
    blocks_b: Mapping[Hashable, Any] | Sequence[Any],
    n_resamples: int,
    seed: int,
) -> BootstrapResult:
    """Bootstrap ``metric(A) - metric(B)`` by paired event-day blocks.

    Every draw resamples whole aligned blocks with replacement.  The reported
    interval is the 95% percentile interval.  The two-sided p-value uses an
    add-one correction, avoiding an impossible zero from finite simulation.
    """

    if isinstance(n_resamples, bool) or not isinstance(n_resamples, int):
        raise TypeError("n_resamples must be an integer")
    if n_resamples <= 0:
        raise ValueError("n_resamples must be positive")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")

    normal_a, normal_b = _normalise_paired_blocks(blocks_a, blocks_b)
    n_blocks = len(normal_a)
    full_indexes = list(range(n_blocks))
    observed = _metric(metric_fn, _combine(normal_a, full_indexes)) - _metric(
        metric_fn, _combine(normal_b, full_indexes)
    )

    rng = random.Random(seed)
    differences: list[float] = []
    for _ in range(n_resamples):
        indexes = [rng.randrange(n_blocks) for _ in range(n_blocks)]
        difference = _metric(metric_fn, _combine(normal_a, indexes)) - _metric(
            metric_fn, _combine(normal_b, indexes)
        )
        differences.append(difference)

    ordered = sorted(differences)
    non_positive = sum(value <= 0.0 for value in differences)
    non_negative = sum(value >= 0.0 for value in differences)
    p_value = min(
        1.0,
        2.0
        * min(
            (non_positive + 1) / (n_resamples + 1),
            (non_negative + 1) / (n_resamples + 1),
        ),
    )
    return BootstrapResult(
        observed_difference=observed,
        differences=tuple(differences),
        ci_low=_quantile(ordered, 0.025),
        ci_high=_quantile(ordered, 0.975),
        p_value=p_value,
        confidence_level=0.95,
        n_resamples=n_resamples,
        n_blocks=n_blocks,
        seed=seed,
    )
