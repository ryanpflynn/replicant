#!/usr/bin/env python3
"""WS-06 canary guardrail evaluator and its operating characteristics (promotion.md §6).

Usage:
    python3 canary_guardrails.py          # false-rollback and detection rates by simulation

The evaluator compares the candidate cohort with the concurrent control cohort (jobs resolved to the
default bundle in the same window). For a rate metric with threshold theta (absolute points):
    fire  iff  n_c, n_k >= min_n  and  P(p_c > p_k | data) > prob  and  observed diff >= f * theta
with independent Jeffreys posteriors Beta(x + 0.5, n - x + 0.5), evaluated by a normal approximation
to the difference of the two Beta distributions (accurate for n >= 20; the production evaluator uses
20,000 Monte Carlo draws seeded by (pipeline_id, evaluated_at) so that results are reproducible).
The check runs every 10 minutes over cumulative data. That is repeated testing, which inflates the
false-alarm rate; the simulation below measures the inflated rate. A false rollback is safe (it reverts
to the known-good default), so a few percent per canary is acceptable. A missed regression is not.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist

Z = NormalDist()


def beta_mean_var(x: int, n: int) -> tuple[float, float]:
    a, b = x + 0.5, n - x + 0.5
    m = a / (a + b)
    v = a * b / ((a + b) ** 2 * (a + b + 1))
    return m, v


def prob_worse(xc: int, nc: int, xk: int, nk: int, theta: float, direction: str) -> float:
    """P(candidate rate exceeds control by > theta) for 'increase'; mirrored for 'decrease'."""
    mc, vc = beta_mean_var(xc, nc)
    mk, vk = beta_mean_var(xk, nk)
    diff = (mc - mk) if direction == "increase" else (mk - mc)
    sd = math.sqrt(vc + vk)
    return 1.0 - Z.cdf((theta - diff) / sd)


SCHEDULE = [  # (hours, candidate jobs/hour, control jobs/hour) for the default canary stages (promotion.md §6.1)
    (24, 45 / 24, 255 / 24),    # ring-early: early-ring tenants (about 15% of 300 jobs/day)
    (24, 70 / 24, 230 / 24),    # pct-10: early ring + 10% of the rest
    (24, 172 / 24, 128 / 24),   # pct-50: early ring + 50% of the rest
]


def fires(xc: int, nc: int, xk: int, nk: int, theta: float, direction: str, *, prob: float,
          observed_min_fraction: float, min_n: int) -> bool:
    """Trigger rule (normative): enough data, P(candidate worse than control) > prob, and the observed
    difference is at least observed_min_fraction * theta."""
    if nc < min_n or nk < min_n:
        return False
    observed = (xc / nc - xk / nk) if direction == "increase" else (xk / nk - xc / nc)
    return observed >= observed_min_fraction * theta and prob_worse(xc, nc, xk, nk, 0.0, direction) > prob


def simulate(base: float, delta: float, theta: float, direction: str, *, min_n: int, prob: float,
             observed_min_fraction: float, trials: int = 1000, seed: int = 5) -> tuple[float, float]:
    """Fraction of staged canaries in which the rule fires, and the median hours to fire."""
    rng = random.Random(seed)
    p_c = base + delta if direction == "increase" else base - delta
    fired_hours = []
    for _ in range(trials):
        xc = nc = xk = nk = 0
        t = 0
        fired_at = None
        for hours, cand, ctrl in SCHEDULE:
            for _ in range(hours * 6):                  # 10-minute evaluations
                t += 1
                a, b = _poisson(rng, cand / 6), _poisson(rng, ctrl / 6)
                xc += sum(rng.random() < p_c for _ in range(a))
                xk += sum(rng.random() < base for _ in range(b))
                nc, nk = nc + a, nk + b
                if fires(xc, nc, xk, nk, theta, direction, prob=prob,
                         observed_min_fraction=observed_min_fraction, min_n=min_n):
                    fired_at = t / 6
                    break
            if fired_at is not None:
                break
        if fired_at is not None:
            fired_hours.append(fired_at)
    frac = len(fired_hours) / trials
    med = sorted(fired_hours)[len(fired_hours) // 2] if fired_hours else float("nan")
    return frac, med


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth; lam is small (< 10) per 10-minute step.
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def main() -> None:
    cases = [  # (trigger, base rate, theta, direction, min_n)
        ("R1 bundle-attributable run failure", 0.05, 0.05, "increase", 20),
        ("R3 job failure rate", 0.20, 0.10, "increase", 30),
        ("R4 QA first-pass rate", 0.70, 0.15, "decrease", 30),
        ("R10 scope violation rate", 0.03, 0.03, "increase", 30),
    ]
    tiers = [("pause (P>0.99, observed >= theta/2)", 0.99, 0.5),
             ("rollback (P>0.999, observed >= theta)", 0.999, 1.0)]
    print("trigger | tier | false-fire rate at true delta 0 | fire rate at delta = theta (median hours)")
    for name, base, theta, direction, min_n in cases:
        for label, prob, frac in tiers:
            fa, _ = simulate(base, 0.0, theta, direction, min_n=min_n, prob=prob, observed_min_fraction=frac)
            det, med = simulate(base, theta, theta, direction, min_n=min_n, prob=prob, observed_min_fraction=frac)
            print(f"{name} | {label} | {fa:.3f} | {det:.3f} ({med:.1f} h)")


if __name__ == "__main__":
    main()
