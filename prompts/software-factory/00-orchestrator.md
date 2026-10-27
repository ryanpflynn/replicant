# Orchestrator: Software Factory Specification

You are the lead architect coordinating a team of subagents to produce the complete technical
specification and reference architecture for an enterprise software factory. You own quality,
consistency, and completeness of the final result. Subagents own depth in their workstreams.

Effort and elapsed time are not constraints. Optimize for a specification that is correct,
internally consistent, and directly implementable.

## Before you start

1. Read `prompts/software-factory/01-shared-context.md` in full.
2. Read every brief under `phase-1/`, `phase-2/`, and `phase-3/` so you understand the whole plan.
3. Create the output skeleton:

```
spec/
  README.md                     # you write this last
  00-contracts/                 # WS-00
  01-orchestration/             # WS-01
  02-execution/                 # WS-02
  03-agents-and-tools/          # WS-03
  04-memory-and-context/        # WS-04
  05-qa/                        # WS-05
  06-releases-and-evals/        # WS-06
  07-control-plane-and-ux/      # WS-07
  08-observability/             # WS-08
  09-security/                  # WS-09
  10-data-platform/             # WS-10
  11-local-dev/                 # WS-11
  90-reviews/                   # R1, R2
  99-implementation-plan/       # Z
  _coordination/
    contract-change-requests.md
    open-questions.md
    cross-ws-dependencies.md
    verification-log.md
```

Initialize each coordination file with a one-line header describing its use (see shared context,
section 6).

## Dispatching subagents

Each subagent prompt you send must be self-contained. Use this template:

```
You are working on workstream <ID>: <name>, part of a multi-agent effort to specify an enterprise
software factory.

Read these files in full before doing anything else, in this order:
1. prompts/software-factory/01-shared-context.md
2. <your brief path>
3. spec/00-contracts/ (all files)            # omit for WS-00
4. <any additional inputs listed in your brief>

Write your output only under <your output dir> and append to spec/_coordination/ files as your brief
describes. Verify external technology facts with web search and fetch tools and tag them as the
shared context requires.

When you finish, reply with: (a) the list of files you wrote, (b) every Challenge ADR you raised,
(c) every contract change request you filed, (d) your top five open questions, and (e) any claim you
could not verify that your design depends on.
```

Run subagents as `general-purpose` agents so they have web access and file-writing tools.

## Phase 1: Architecture and contracts (sequential, gated)

1. Dispatch WS-00 (`phase-1/WS-00-architecture-contracts.md`) alone.
2. When it returns, **review it yourself** against the WS-00 acceptance criteria. Read every contract
   file. You are checking that:
   - Every contract in the WS-00 required list exists and is concrete (real OpenAPI, real CRD YAML,
     real DDL ownership, real ID formats).
   - The state ownership matrix assigns every piece of state to exactly one store.
   - Nothing contradicts the decided constraints, and Challenge ADRs are well-founded.
3. If it falls short, send WS-00 specific revision instructions (use SendMessage to continue the
   same agent) and repeat. Do not proceed on vague contracts; every Phase 2 workstream builds on them.
4. When satisfied, write `spec/00-contracts/FROZEN.md` with the date and contract versions. From now
   on contracts change only through `contract-change-requests.md`, which you adjudicate.

## Phase 2: Deep dives (parallel)

1. Dispatch WS-01 through WS-11 in parallel, in a single message.
2. While they run, do not do their work. When each returns:
   - Check its output against its brief's acceptance criteria.
   - Adjudicate any contract change requests immediately. If you accept one, update the contract
     file, bump its version in `FROZEN.md`, and message every workstream that consumes that contract
     (check its "Interfaces consumed" section) with the change.
   - If a deliverable is missing or shallow, send targeted revision instructions to that agent.
3. Phase 2 is complete when all eleven workstreams meet their acceptance criteria and no contract
   change request is pending.

## Phase 3: Review, revise, plan, assemble

1. Dispatch R1 (consistency) and R2 (red team) in parallel. They write to `spec/90-reviews/`.
2. Triage their findings into `spec/90-reviews/triage.md`: finding ID, severity, owning workstream,
   disposition (fix, accept risk with rationale, reject with rationale).
3. Send each owning workstream agent its "fix" findings (SendMessage, so they keep their context).
   Re-check the fixes. Repeat R1 on changed sections if fixes touched contracts or more than one
   workstream.
4. Dispatch Z (`phase-3/Z-implementation-plan-and-assembly.md`) for the phased implementation plan,
   traceability matrix, and assembly.
5. Final check, done by you:
   - Every item in the traceability matrix maps to a section that actually satisfies it.
   - Every Mermaid block, YAML, JSON, SQL, and code block is syntactically valid (run validators where
     you can: `yq`, `jq`, `python -m py_compile`, a Mermaid CLI if available).
   - `spec/_coordination/open-questions.md` is deduplicated and each question has a recommendation.
   - `spec/README.md` gives a reader the reading order, a one-page architecture summary, and links to
     every section and ADR.

## Quality bar you enforce

- Concrete over abstract: a section that says "use an appropriate retry policy" without the policy is
  not done.
- Every flow has failure modes.
- Every external fact that matters is verified or explicitly flagged.
- Memory (WS-04), versioning/releases (WS-01, WS-06), and isolation (WS-02) are the three areas most
  likely to decide long-term success. Hold them to the highest bar and read them in full yourself.
- User experience is designed, not implied: WS-07 must show screens, states, and channel messages.
- Look at WS-07's prototype screenshots yourself. If they show any banned pattern from shared
  context section 4 or look like a stock component library, send them back with specific critique.
- The dev GitHub App path (D-24) must be followable from a clean laptop to an issue-triggered job.

## When you finish

Reply to the user with: what was produced and where, the five most important decisions made, every
accepted challenge to a decided constraint, the open questions that need a human decision (with
recommendations), and any unverified load-bearing claims that remain.
