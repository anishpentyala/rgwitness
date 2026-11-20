"""Compare row-group claims with unfiltered decoded scalar values."""
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Claim:
    rows: int
    nulls: int | None
    has_bounds: bool
    lower: Any = None
    upper: Any = None


@dataclass(frozen=True)
class Observed:
    rows: int
    nulls: int
    lower: Any
    upper: Any


def observe(chunks: Iterable[Iterable[Any]], *, limit: int | None = None) -> Observed:
    rows = nulls = 0
    lower = upper = None
    for chunk in chunks:
        for value in chunk:
            rows += 1
            if limit is not None and rows > limit:
                raise ValueError('decoded row budget exceeded')
            if value is None:
                nulls += 1
            elif lower is None:
                lower = upper = value
            else:
                lower = min(lower, value)
                upper = max(upper, value)
    return Observed(rows, nulls, lower, upper)


def compare(claim: Claim, actual: Observed) -> dict:
    """Conservative bounds are valid; missing claims are not verified claims."""
    violations = []
    unchecked = []
    if claim.rows != actual.rows:
        violations.append('row_count')
    if claim.nulls is None:
        unchecked.append('null_count_missing')
    elif claim.nulls != actual.nulls:
        violations.append('null_count')
    if claim.has_bounds:
        if claim.lower is None or claim.upper is None:
            violations.append('incomplete_bounds')
        elif claim.lower > claim.upper:
            violations.append('reversed_bounds')
        elif actual.lower is None:
            violations.append('bounds_without_values')
        else:
            if claim.lower > actual.lower:
                violations.append('lower_excludes_value')
            if claim.upper < actual.upper:
                violations.append('upper_excludes_value')
    elif actual.lower is not None:
        unchecked.append('bounds_missing')
    return {'claim': asdict(claim), 'observed': asdict(actual),
            'violations': violations, 'unchecked': unchecked}
