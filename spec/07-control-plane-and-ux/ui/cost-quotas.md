# Cost and quotas

Routes `/cost` (my tenants), `/cost/tenants/:tenantId`, `/cost/jobs/:jobId` (link from job detail).
Data: `GET /v1/cost/tenants/{t}` (from `cost` rollups, WS-03 owns the ledger), tenant settings for
budgets and quotas. Permission `cost:read`; quotas editable by tenant admins within platform caps.

```
┌ Cost · payments · October 2026 ─────────────────────────────────────────────── [⌘K] ◐ ┐
│ Month to date $6,412 of $10,000 budget ▬▬▬▬▬▬▬▬▬▬▬▬▬────────  64% · alerts at 80, 100% │ (1)
│ Projected month end $9,140 (from trailing 7-day rate)                                │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ Daily cost, last 30 days (stacked: model tokens · sandbox compute)          Table view │ (2)
│ ▁▂▂▃▂▃▅▃▂▂▃▃▄▃▂▃▃▄▆▄▃▃▄▃▂▃▃▄▃▃                                                       │
├──────────────────────────────┬────────┬───────┬────────────┬────────────┬────────────┤
│ GROUP BY [repo ▾]            │  COST  │ JOBS  │ $/JOB p50  │ TOKENS in  │ CPU-hours  │ (3)
│ ledger-api                   │ $2,914 │  311  │   $6.80    │  1.9 B     │   1,204    │
│ checkout-web                 │ $1,622 │  140  │   $9.10    │  0.8 B     │     980    │
│ …                                                                                      │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ Quotas (tenant admin)      value / platform cap                                        │ (4)
│ Concurrent runs            12 / 20   now 12 ▬▬▬▬▬▬▬▬▬▬▬▬  (3 jobs waiting capacity)    │
│ Active jobs                60 / 200                                                    │
│ Jobs per requester per hour 10 / 30                                                    │
│ Monthly budget             $10,000                         [Edit quotas]               │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

1. Budget bar uses ink, attention at ≥ 80%, danger over 100%; the projection is labelled as such.
2. One chart, one question ("where is the money going over time"); stacked two series, validated
   palette, table view.
3. Grouping: repo, workflow type, requester, model, bundle, day.
4. Quota edits validate against platform caps (422 with the cap); changes are operational config
   (ADR-013) and audited.

States: Loading (skeleton), Empty ("No cost recorded yet this month"), Cost data delayed (banner when
rollup lag > 5 min: "Costs lag by 7 min"), Error (region).
