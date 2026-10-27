# R2: Red Team Review

**Output:** `spec/90-reviews/R2-red-team.md`

## Purpose

Attack the design. Assume it will be built exactly as written and run for three years at the
assumed scale. Find what breaks, what gets abused, what users hate, and what becomes impossible
to change. Be specific and adversarial; generic advice is not useful.

## Lenses

1. **Security**: walk each threat in `spec/09-security/threat-model.md` against the actual designs
   in other sections. Write concrete attack narratives: a prompt-injected issue that tries to push
   code, exfiltrate the gateway token, poison memory, approve itself, or reach another tenant.
   Does any path succeed?
2. **Failure modes**: kill each component at the worst moment (Runner mid-create, controller during
   artifact upload, worker during Continue-As-New, Postgres failover during an approval, Temporal
   unavailable for an hour, S3 throttling, gateway outage, node loss during a 40-minute QA run).
   Is the outcome defined, and is it acceptable?
3. **Long-term viability**: a breaking LangGraph release, deprecation of the Temporal integration,
   a CLI changing its output format or session layout, a model retirement, 10x growth in repos,
   memory at 100,000 entries, a runtime CVE forcing a switch from the default isolation runtime.
   How hard is each change in this design?
4. **User experience**: walk each persona through a bad day: a job stuck for hours, an approver with
   40 pending gates, a requester whose job silently produced a wrong fix, a reviewer whose comment
   was misread and stored as memory, an operator rolling back during an incident. Where does the
   design leave them confused or stuck?
   Then review the rendered prototype screenshots in `spec/07-control-plane-and-ux/design/` cold,
   as a demanding design lead would. Does it look modern and crafted, or generic? Name every
   instance of the banned patterns in shared context section 4, every place hierarchy or density
   fails an operator, and every state that is missing or unclear.
5. **Cost**: find unbounded spend (repair loops, eval runs, retries that re-run LLM calls, oversized
   workspaces, log volume) and check each has a cap.
6. **Unverified claims**: list every `[unverified]` tag that is load-bearing and assess the
   fallback. Spot-check a sample of `[verified]` claims yourself.

## Output format

Findings with ID (`R2-NNN`), lens, severity (critical, high, medium, low), the scenario as a
narrative, the design sections involved, why the current design fails, and a recommended fix. End
with the ten findings you would fix first.
