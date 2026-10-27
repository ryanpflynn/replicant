#!/usr/bin/env python3
"""Monthly storage (and storage-adjacent) cost model per store, at the shared-context scale.

Run: python3 tools/cost_model.py            -> markdown tables (pasted into capacity-and-cost.md)
     python3 tools/cost_model.py --jobs-per-day 1000   (sensitivity)

Prices: us-east-1 on-demand, AWS Price List API files published 2026-10-15 (S3), 2026-10-20 (RDS),
2026-09-11 (KMS) [verified 2026-10-24; see verification-log WS-10 entries]. Volumes are ESTIMATES
(assumptions table below); every one is a named constant so a measured baseline replaces it.
"""
from __future__ import annotations

import argparse

# ---- prices (USD) ----------------------------------------------------------------------------
S3_STD, S3_IA, S3_GIR = 0.023, 0.0125, 0.004           # per GB-month
S3_PUT, S3_GET = 0.005 / 1000, 0.0004 / 1000           # per request
S3_TRANS_IA, S3_TRANS_GIR = 0.01 / 1000, 0.02 / 1000   # per lifecycle transition
XREGION_GB, RTC_GB = 0.02, 0.015                       # replication us-east-1 -> us-west-2, per GB
AUR_STORAGE_STD, AUR_STORAGE_IOO, AUR_IO = 0.10, 0.225, 0.20 / 1e6
AUR_BACKUP = 0.021
AUR_GLOBAL_WRITE_IO = 0.20 / 1e6
AUR_R8G = {"large": (0.276, 0.359), "xlarge": (0.552, 0.718), "2xlarge": (1.104, 1.436)}  # (Standard, I/O-Optimized) $/h
ACU_H = 0.12
KMS_KEY_MONTH, KMS_REQ = 1.0, 0.03 / 10000
HOURS = 730

# ---- volume assumptions (per job unless noted; MB) --------------------------------------------
RUNS_PER_JOB = 5.0                 # planner, implementer, qa, reviewer + ~1 repair run on average
QA_RUNS_PER_JOB = 1.5
A = {
    # artifacts bucket, by retention class
    "art_r2y": 2 * 1.0 + 1.0 + 0.5 * QA_RUNS_PER_JOB + 2.0 * QA_RUNS_PER_JOB,   # diffs, plans/handoffs/results, qa_report, sbom
    "art_r1y": (1.0 + 12.0) * QA_RUNS_PER_JOB,                                  # junit, screenshots (40 x 300 KB)
    "art_r90d": (15 + 2 + 30) * QA_RUNS_PER_JOB + 3.0 * RUNS_PER_JOB + 1.0,     # build/health logs, traces, inputs/briefings, PR payloads
    "transcripts_r90d": 25.0 * RUNS_PER_JOB,                                    # transcript + cli_events + sandbox_log (zstd)
    "session_live_days": 10.0, "session_mb": 30.0 * 3,                          # 3 coding runs x 30 MB, ~10 days alive
    "takeover_rate": 0.01, "takeover_mb": 3000.0,                               # workspace archives, r30d
    "audit_r2y": 1.5 * 0.02 + 0.25,                                             # approval records (reference-only, ~20 KB) + workflow_history_skeleton (~0.25 MB/job)
    "art_history_r2y": 4.0 + 1.5 * 0.01,                                        # workflow_history_export (~4 MB/job, C-ART 1.9.0 moved to artifacts) + approval_text
    "memory_r2y": 0.2,                                                          # snapshot exports
    "objects_per_job": 800,                                                     # PUTs (chunked transcripts dominate)
    "gets_per_job": 400,
}
EVAL_GB_MONTH = 30 * 150 * 0.060   # suites/month x cases x GB per case output (r1y)
# r7y audit types, org-level (not per job): GB written per month
AUDIT_R7Y_GB_MONTH = {
    "audit_event_export": 0.25,       # ≈ 80k events/day × ~100 B zstd
    "audit_partition_export": 0.5,    # monthly snapshot of the audit.events partition (zstd)
    "okta_system_log_export": 0.3,    # ≈ 50k identity events/day, 2,000 users
    "deletion_ledger_and_purge_records": 0.01,
}
GOLDEN_GB = 300 * 0.5              # golden task snapshots (perm)
REGISTRY_GB = 5.0
TENANTS = 40


def s3(jobs_per_month: float) -> list[tuple[str, float, float, str]]:
    gb = lambda mb: mb * jobs_per_month / 1024  # noqa: E731  monthly ingest in GB
    rows = []
    # artifacts: r2y objects -> IA at 30 d, GIR at 120 d (only objects >128 KB: assume 70% of bytes)
    r2y_in = gb(A["art_r2y"] + A["art_history_r2y"])
    r2y = r2y_in * (1 * S3_STD + 3 * S3_IA + 20 * (0.7 * S3_GIR + 0.3 * S3_IA))
    r1y_in = gb(A["art_r1y"])
    r1y = r1y_in * (1 * S3_STD + 11 * S3_IA)
    r90_in = gb(A["art_r90d"])
    r90 = r90_in * 3 * S3_STD
    art_gb = r2y_in * 24 + r1y_in * 12 + r90_in * 3
    rows.append(("S3 artifacts", art_gb, r2y + r1y + r90, "incl. workflow_history_export (~4 MB/job); r2y→IA@30d→GIR@120d; r1y→IA@30d; r90d Standard"))
    tr_in = gb(A["transcripts_r90d"])
    rows.append(("S3 transcripts", tr_in * 3, tr_in * (1 * S3_STD + 2 * S3_IA), "r90d, IA@30d"))
    ses_gb = gb(A["session_mb"]) * A["session_live_days"] / 30 + gb(A["takeover_rate"] * A["takeover_mb"])
    rows.append(("S3 session", ses_gb, ses_gb * S3_STD, "job end+7 d (sweeper), takeover r30d"))
    au_in = gb(A["audit_r2y"])
    r7 = sum(AUDIT_R7Y_GB_MONTH.values())
    au_gb = au_in * 24 + r7 * 84
    rows.append(("S3 audit (Object Lock, compliance)", au_gb,
                 au_in * (3 * S3_STD + 21 * S3_GIR) + r7 * (3 * S3_STD + 81 * S3_GIR),
                 "approval records + history skeletons (r2y); event/partition/Okta exports, ledger (r7y); GIR@90d"))
    me_in = gb(A["memory_r2y"])
    rows.append(("S3 memory", me_in * 24, me_in * (1 * S3_STD + 23 * S3_IA), ""))
    ev_gb = EVAL_GB_MONTH * 12 + GOLDEN_GB
    rows.append(("S3 eval", ev_gb, EVAL_GB_MONTH * (S3_STD + 11 * S3_IA) + GOLDEN_GB * S3_IA, "WS-06 volumes assumed"))
    rows.append(("S3 registry", REGISTRY_GB, REGISTRY_GB * S3_STD, "perm, tiny"))
    reqs = jobs_per_month * (A["objects_per_job"] * S3_PUT + A["gets_per_job"] * S3_GET)
    trans = jobs_per_month * (40 * S3_TRANS_IA + 10 * S3_TRANS_GIR)
    rows.append(("S3 requests + transitions", 0.0, reqs + trans, "PUT/GET/lifecycle transitions"))
    repl_gb_month = (r2y_in + r1y_in + r90_in + tr_in + au_in + r7 + me_in + EVAL_GB_MONTH)
    repl_stored = art_gb + tr_in * 3 + au_gb + me_in * 24 + ev_gb + REGISTRY_GB
    rows.append(("S3 DR replicas (us-west-2, IA)", repl_stored, repl_stored * S3_IA + repl_gb_month * (XREGION_GB + RTC_GB),
                 "CRR+RTC of all classes except session"))
    kms = (TENANTS * 2 + 8) * KMS_KEY_MONTH + jobs_per_month * 200 * KMS_REQ
    rows.append(("KMS (tenant + platform keys, DR replicas)", 0.0, kms, "Bucket Keys keep requests low"))
    return rows


def postgres(jobs_per_month: float) -> list[tuple[str, float, float, str]]:
    scale = jobs_per_month / 12000
    rows = []
    f_gb = 250 + 150 * scale   # year-1 average incl. indexes (postgres.md §8)
    f_io = 300 * scale * 2.6e6  # IO/month at ~300 IOPS average
    inst = 2 * AUR_R8G["2xlarge"][0] * HOURS
    rows.append(("Aurora replicant (PG 17, 2 × r8g.2xlarge, Standard)", f_gb,
                 inst + f_gb * AUR_STORAGE_STD + f_io * AUR_IO + 150 * AUR_BACKUP, "instances included"))
    rows.append(("  of which storage + I/O + backup", f_gb, f_gb * AUR_STORAGE_STD + f_io * AUR_IO + 150 * AUR_BACKUP, ""))
    rows.append(("Aurora replicant Global DB headless secondary (us-west-2)", f_gb,
                 f_gb * AUR_STORAGE_STD + f_io * 0.5 * AUR_GLOBAL_WRITE_IO, "storage + replicated writes, no instances"))
    t_gb = 120 * scale + 30
    t_inst_ioo = 2 * AUR_R8G["xlarge"][1] * HOURS
    rows.append(("Aurora temporal (PG 16, 2 × r8g.xlarge, I/O-Optimized)", t_gb,
                 t_inst_ioo + t_gb * AUR_STORAGE_IOO + 60 * AUR_BACKUP, "I/O-heavy: see temporal-persistence.md §5"))
    rows.append(("  of which storage + backup", t_gb, t_gb * AUR_STORAGE_IOO + 60 * AUR_BACKUP, ""))
    rows.append(("Aurora temporal Global DB headless secondary", t_gb,
                 t_gb * AUR_STORAGE_IOO + 1000 * scale * 2.6e6 * 0.5 * AUR_GLOBAL_WRITE_IO, ""))
    rows.append(("Aurora langfuse (PG 16, 2 × r8g.large; databases langfuse + grafana)", 30.0,
                 2 * AUR_R8G["large"][0] * HOURS + 30 * AUR_STORAGE_STD + 2e8 * AUR_IO + 30 * AUR_BACKUP,
                 "R1-235: settled with WS-08 (PG 16)"))
    return rows


def table(title: str, rows) -> str:
    out = [f"### {title}", "", "| Store | Stored GB (steady state) | USD / month | Notes |", "|---|---:|---:|---|"]
    for name, gb_, usd, note in rows:
        out.append(f"| {name} | {gb_:,.0f} | {usd:,.0f} | {note} |")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-per-day", type=float, default=400.0, help="average (steady 300, peaks 1,000)")
    a = ap.parse_args()
    jpm = a.jobs_per_day * 30
    s, p = s3(jpm), postgres(jpm)
    print(f"Jobs/month: {jpm:,.0f}\n")
    print(table("Object storage", s))
    print(f"\n**S3 + KMS total: ${sum(r[2] for r in s):,.0f}/month**\n")
    print(table("PostgreSQL", p))
    tot = sum(r[2] for r in p if not r[0].startswith("  "))
    print(f"\n**PostgreSQL total (instances + storage + DR): ${tot:,.0f}/month**")
