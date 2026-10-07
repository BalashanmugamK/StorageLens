"""Exact Poisson confidence intervals — dependency-free.

Why it lives here: every savings number the engine produces rides on
one observed quantity, the 30-day download count. That count is a
Poisson sample, so its sampling uncertainty is computable exactly —
meaning each predicted saving can carry an interval instead of a
lonely point estimate, at zero model cost.

The interval is the exact Garwood construction:

    lower = chi2inv(alpha/2,   2k)     / 2
    upper = chi2inv(1-alpha/2, 2k+2)   / 2

with the k == 0 special case lower = 0,
upper = -ln(alpha) (chi2inv(1-alpha, 2) / 2).

For even degrees of freedom 2m the chi-square quantile is
2 * gammaincinv(m, p), where gammaincinv inverts the lower
regularized incomplete gamma P(a, x). P is evaluated by the
Numerical Recipes pair: power series for x < a + 1, Lentz
continued fraction otherwise. The inverse is found by bisection on
P's monotonicity in x — no scipy, no approximations, safe inside
the Lambda bundle and the offline experiments alike.
"""

import math

_ALPHA = 0.05
_EPS = 1e-14


def _serm(a: float, x: float) -> float:
    """P(a, x) by power series; accurate for x < a + 1."""
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(500):
        ap += 1
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * _EPS:
            break
    return total * math.exp(
        -x + a * math.log(x) - math.lgamma(a)
    )


def _gser_q(a: float, x: float) -> float:
    """Q(a, x) = 1 - P(a, x) by Lentz continued fraction; for x >= a+1."""
    tiny = 1e-30
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(
        -x + a * math.log(x) - math.lgamma(a)
    ) * h


def _gammainc(a: float, x: float) -> float:
    """Regularized lower incomplete gamma P(a, x), a > 0, x >= 0."""
    if x <= 0:
        return 0.0
    if x < a + 1.0:
        return _serm(a, x)
    return 1.0 - _gser_q(a, x)


def _gammaincinv(a: float, p: float) -> float:
    """Inverse of P(a, x) in x (P is strictly increasing). Bisection
    on an expanding bracket; converges to machine precision for the
    integer a values the interval uses (a = k or k + 1)."""
    if p <= 0:
        return 0.0
    if p >= 1:
        return math.inf

    hi = max(1.0, a)
    while _gammainc(a, hi) < p:
        hi *= 2.0
        if hi > 1e12:
            break

    lo = 0.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _gammainc(a, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12 * max(hi, 1e-12):
            break
    return (lo + hi) / 2.0


def poisson_interval(
    count: int,
    alpha: float = _ALPHA,
) -> tuple[float, float]:
    """Exact (1-alpha) Poisson/Garwood interval for `count` events.

    Returns (low, high) annual-equivalent bounds of the true mean.
    Always well-defined: for zero count the lower bound is 0 and
    the upper falls out of the same formula as
    chi2inv(1-alpha/2, 2) / 2 = -ln(alpha/2).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")

    if count == 0:
        # chi2 with 0 df is 0, so the lower bound is exactly 0;
        # the upper still comes from the 2(k+1)=2 df formula.
        return (0.0, -math.log(alpha / 2.0))

    lower = _gammaincinv(count, alpha / 2.0)
    upper = _gammaincinv(count + 1, 1.0 - alpha / 2.0)
    return (lower, upper)