#!/usr/bin/env python3
"""WS-06 eval sizing: statistical power and cost of a full evaluation run.

Reference implementation for spec/06-releases-and-evals/eval-harness.md §6 and §8.
Pure standard library (Python 3.12+), so it runs anywhere without dependencies:

    python3 eval_sizing.py            # print every table used in the spec
    python3 eval_sizing.py --simulate # also run the Monte Carlo check (slow, ~1 min)

Statistical model (hierarchical, paired by task):
  * Task i has a latent success probability p_i under the incumbent bundle A.
    p_i ~ Beta with mean MU and intra-class correlation RHO, i.e. Var(p_i) = RHO*MU*(1-MU).
    RHO is the share of outcome variance that is *between tasks* (task difficulty);
    1-RHO is the share that is run-to-run LLM nondeterminism.
  * Under candidate B, logit(q_i) = logit(p_i) + BETA + u_i, u_i ~ N(0, SIGMA_U^2).
    BETA is calibrated so that E[q_i - p_i] = DELTA; SIGMA_U gives per-task effect heterogeneity.
  * Each arm runs R independent repetitions per task. The test statistic is the mean over tasks of
    d_i = mean_r(Y_B,i,r) - mean_r(Y_A,i,r), analysed as a paired (cluster-level) comparison.
  * Var(d_i) = Var(q_i - p_i) + (E[p(1-p)] + E[q(1-q)]) / R
    so the number of tasks T needed for standard error SE* is T = Var(d_i) / SE*^2.
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass
from statistics import NormalDist

Z = NormalDist()


def logit(x: float) -> float:
    return math.log(x / (1.0 - x))


def expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True)
class Population:
    mu: float = 0.60        # incumbent mean success rate on the golden set
    rho: float = 0.50       # intra-class correlation (between-task share of variance)
    sigma_u: float = 0.60   # SD of per-task effect on the logit scale (heterogeneity)
    delta: float = 0.05     # true mean improvement (absolute, success-rate points / 100)

    def beta_params(self) -> tuple[float, float]:
        s = (1.0 - self.rho) / self.rho          # a + b for the Beta distribution
        return self.mu * s, (1.0 - self.mu) * s


def _quadrature_draws(pop: Population, n: int = 40_000, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    a, b = pop.beta_params()
    out = []
    for _ in range(n):
        p = rng.betavariate(a, b)
        out.append(min(max(p, 1e-6), 1 - 1e-6))
    return out


def calibrate_beta(pop: Population, ps: list[float], seed: int = 11) -> float:
    """Find BETA such that E[q - p] = delta (bisection on a fixed sample)."""
    rng = random.Random(seed)
    us = [rng.gauss(0.0, pop.sigma_u) for _ in ps]

    def mean_diff(beta: float) -> float:
        return sum(expit(logit(p) + beta + u) - p for p, u in zip(ps, us)) / len(ps)

    lo, hi = -3.0, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if mean_diff(mid) < pop.delta:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def moments(pop: Population) -> dict[str, float]:
    """Monte Carlo moments of the task-level quantities (deterministic seed)."""
    ps = _quadrature_draws(pop)
    beta = calibrate_beta(pop, ps)
    rng = random.Random(13)
    diffs, pq, qq = [], [], []
    for p in ps:
        q = expit(logit(p) + beta + rng.gauss(0.0, pop.sigma_u))
        diffs.append(q - p)
        pq.append(p * (1 - p))
        qq.append(q * (1 - q))
    n = len(ps)
    md = sum(diffs) / n
    var_eff = sum((d - md) ** 2 for d in diffs) / (n - 1)
    return {
        "beta": beta,
        "mean_diff": md,
        "var_effect": var_eff,           # Var(q_i - p_i)
        "within_a": sum(pq) / n,         # E[p(1-p)]
        "within_b": sum(qq) / n,         # E[q(1-q)]
    }


def var_d(m: dict[str, float], reps: int) -> float:
    return m["var_effect"] + (m["within_a"] + m["within_b"]) / reps


def tasks_needed(m: dict[str, float], reps: int, *, delta: float, alpha: float, power: float,
                 one_sided: bool) -> int:
    za = Z.inv_cdf(1 - alpha) if one_sided else Z.inv_cdf(1 - alpha / 2)
    zb = Z.inv_cdf(power)
    se_star = delta / (za + zb)
    return math.ceil(var_d(m, reps) / se_star ** 2)


def mde(m: dict[str, float], tasks: int, reps: int, *, alpha: float = 0.05,
        power: float = 0.80, one_sided: bool = False) -> float:
    za = Z.inv_cdf(1 - alpha) if one_sided else Z.inv_cdf(1 - alpha / 2)
    zb = Z.inv_cdf(power)
    return (za + zb) * math.sqrt(var_d(m, reps) / tasks)


def mcnemar_pairs(psi: float, delta: float, alpha: float = 0.05, power: float = 0.80) -> int:
    """Single-rep paired binary (McNemar) sample size; psi = discordant-pair rate p10+p01."""
    za, zb = Z.inv_cdf(1 - alpha / 2), Z.inv_cdf(power)
    return math.ceil((za * math.sqrt(psi) + zb * math.sqrt(psi - delta ** 2)) ** 2 / delta ** 2)


def unpaired_per_arm(p0: float, p1: float, alpha: float = 0.05, power: float = 0.80) -> int:
    za, zb = Z.inv_cdf(1 - alpha / 2), Z.inv_cdf(power)
    pbar = (p0 + p1) / 2
    num = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p0 * (1 - p0) + p1 * (1 - p1))) ** 2
    return math.ceil(num / (p1 - p0) ** 2)


def simulate_power(pop: Population, tasks: int, reps: int, *, trials: int = 2000,
                   alpha: float = 0.05, seed: int = 1) -> float:
    """Empirical power of the paired cluster-level z-test (two-sided)."""
    rng = random.Random(seed)
    a, b = pop.beta_params()
    beta = calibrate_beta(pop, _quadrature_draws(pop, 20_000))
    crit = Z.inv_cdf(1 - alpha / 2)
    hits = 0
    for _ in range(trials):
        ds = []
        for _ in range(tasks):
            p = min(max(rng.betavariate(a, b), 1e-6), 1 - 1e-6)
            q = expit(logit(p) + beta + rng.gauss(0.0, pop.sigma_u))
            ya = sum(rng.random() < p for _ in range(reps)) / reps
            yb = sum(rng.random() < q for _ in range(reps)) / reps
            ds.append(yb - ya)
        md = sum(ds) / tasks
        sd = math.sqrt(sum((d - md) ** 2 for d in ds) / (tasks - 1))
        if abs(md / (sd / math.sqrt(tasks))) > crit:
            hits += 1
    return hits / trials


# --------------------------------------------------------------------------------------------
# Cost model (eval-harness.md §8). Prices: Anthropic first-party list prices per MTok
# [verified: https://platform.claude.com/docs/en/about-claude/pricing, 2026-10-24]. The gateway
# may apply negotiated rates; the cost ledger (usage-ingestor) is authoritative after a run.
# --------------------------------------------------------------------------------------------

PRICES = {  # USD per million tokens: input, cache_write_5m, cache_read, output
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    # Reviewer on Codex: price not verified; modelled at Sonnet 5.5 parity (assumption A-C5).
    "codex-reviewer-model": (2.00, 2.50, 0.10, 10.00),
}


@dataclass(frozen=True)
class RunProfile:
    model: str
    total_input_mtok: float   # all prompt tokens over the run (uncached + cache write + cache read)
    cache_read_share: float
    cache_write_share: float
    output_mtok: float
    sandbox_minutes: float
    vcpu: float

    def llm_usd(self) -> float:
        p_in, p_w, p_r, p_out = PRICES[self.model]
        t = self.total_input_mtok
        uncached = t * (1 - self.cache_read_share - self.cache_write_share)
        return (uncached * p_in + t * self.cache_write_share * p_w + t * self.cache_read_share * p_r
                + self.output_mtok * p_out)


VCPU_HOUR_USD = 6.265 / 96 * 1.25  # m8id.24xlarge on-demand (the sbx-kata shape, 2x the 12xlarge price) / 96 vCPU, +25% packing and overhead


def case_cost(profiles: list[RunProfile], judge_calls: int, judge_in_mtok: float,
              judge_out_mtok: float, judge_model: str = "claude-opus-5-5",
              judge_batch: bool = False) -> dict[str, float]:
    llm = sum(r.llm_usd() for r in profiles)
    compute = sum(r.sandbox_minutes / 60 * r.vcpu * VCPU_HOUR_USD for r in profiles)
    p_in, _, _, p_out = PRICES[judge_model]
    judge = judge_calls * (judge_in_mtok * p_in + judge_out_mtok * p_out) * (0.5 if judge_batch else 1.0)
    return {"agent_llm": llm, "compute": compute, "judge": judge, "total": llm + compute + judge}


BUGFIX_PROFILE = [
    # planner (read-only), implementer, QA (no LLM: deterministic build/test/Playwright), reviewer
    RunProfile("claude-sonnet-5-5", 1.2, 0.90, 0.07, 0.020, 6, 4),
    RunProfile("claude-sonnet-5-5", 3.6, 0.92, 0.06, 0.060, 22, 6),
    RunProfile("claude-sonnet-5-5", 0.0, 0.0, 0.0, 0.0, 12, 6),
    RunProfile("codex-reviewer-model", 0.8, 0.85, 0.10, 0.015, 6, 4),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    args = ap.parse_args()

    print("== Baselines (single rep, success 0.60 -> 0.65) ==")
    print(f"unpaired two-proportion, per arm: {unpaired_per_arm(0.60, 0.65)}")
    for psi in (0.15, 0.25, 0.35):
        print(f"McNemar paired, discordance {psi:.2f}: {mcnemar_pairs(psi, 0.05)} task pairs")

    print("\n== Hierarchical paired design: tasks T needed (runs per arm = T*R) ==")
    print("rho  sigma_u  R | superiority 5pt (2-sided a=.05, power .8) | non-inferiority margin 5pt (1-sided a=.05, power .8)")
    for rho in (0.3, 0.5, 0.7):
        for su in (0.4, 0.6):
            m = moments(Population(rho=rho, sigma_u=su))
            m0 = moments(Population(rho=rho, sigma_u=su, delta=0.0))
            for reps in (1, 2, 3, 5):
                t_sup = tasks_needed(m, reps, delta=0.05, alpha=0.05, power=0.8, one_sided=False)
                t_ni = tasks_needed(m0, reps, delta=0.05, alpha=0.05, power=0.8, one_sided=True)
                print(f"{rho:.1f}  {su:.1f}     {reps} | T={t_sup:5d} runs/arm={t_sup*reps:5d}"
                      f"                        | T={t_ni:5d} runs/arm={t_ni*reps:5d}")
    m = moments(Population())
    print(f"\nreference population moments: {m}")

    print("\n== MDE (2-sided a=.05, power .8) for the reference population ==")
    for tasks in (60, 150, 300, 450, 600, 900):
        row = "  ".join(f"R={r}: {100*mde(m, tasks, r):4.1f}pt" for r in (1, 2, 3))
        print(f"T={tasks:4d}  {row}")

    print("\n== Promotion-policy tiers: non-inferiority power at true difference 0 (one-sided a=.05) ==")
    m0 = moments(Population(delta=0.0))
    for tier, tasks, reps, margin in (("T1", 60, 1, 0.16), ("T2", 300, 1, 0.075), ("T3", 450, 2, 0.045)):
        se = math.sqrt(var_d(m0, reps) / tasks)
        pw = Z.cdf(margin / se - Z.inv_cdf(0.95))
        print(f"{tier}: T={tasks} R={reps} SE={100*se:.2f}pt margin={100*margin:.1f}pt power={pw:.3f}")

    print("\n== Cost per eval case (bugfix-shaped profile) ==")
    cc = case_cost(BUGFIX_PROFILE, judge_calls=3, judge_in_mtok=0.040, judge_out_mtok=0.002)
    cb = case_cost(BUGFIX_PROFILE, judge_calls=3, judge_in_mtok=0.040, judge_out_mtok=0.002, judge_batch=True)
    print({k: round(v, 2) for k, v in cc.items()}, "(judge synchronous)")
    print({k: round(v, 2) for k, v in cb.items()}, "(judge via batch)")
    for label, mult in (("as modelled", 1.0), ("token use x2", 2.0), ("token use x3", 3.0)):
        agent = cc["agent_llm"] * mult
        total = agent + cc["compute"] + cc["judge"]
        print(f"{label:>12}: per case ${total:5.2f}")
        for tier, cases in (("smoke", 60), ("standard (300 tasks x1 rep x2 arms)", 600),
                            ("full (450 tasks x2 reps x2 arms)", 1800),
                            ("full, incumbent arm reused", 900)):
            print(f"{'':>14}{tier:<40} {cases:5d} cases  ${cases*total:9,.0f}")

    print("\n== Annual run rate (A-C8: 4 T2 with reused baseline + 1 T3 + 7 sentinel T1 per week) ==")
    for label, mult in (("1x tokens", 1.0), ("2x tokens", 2.0)):
        per_case = cc["agent_llm"] * mult + cc["compute"] + cc["judge"]
        weekly = (4 * 300 + 1800 + 7 * 60) * per_case
        prod_job = cc["agent_llm"] * mult + cc["compute"]          # production jobs have no judge
        print(f"{label}: per case ${per_case:.2f}; weekly ${weekly:,.0f}; annual ${52*weekly:,.0f}; "
              f"sentinel/yr ${52*7*60*per_case:,.0f}; production 300 jobs/day/yr ${365*300*prod_job:,.0f} "
              f"(${prod_job:.2f}/job)")

    if args.simulate:
        print("\n== Monte Carlo check (2,000 trials each) ==")
        pop = Population()
        for tasks, reps in ((450, 2), (300, 2), (830, 1)):
            print(f"T={tasks} R={reps}: empirical power {simulate_power(pop, tasks, reps):.3f}")


if __name__ == "__main__":
    main()
