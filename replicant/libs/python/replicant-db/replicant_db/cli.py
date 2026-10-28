"""replicant-db — the migrate image's entrypoint (`replicant-db migrate apply|verify|maintenance|status`).

Reviewed target (schema-management.md §7). Exit codes are part of the contract with the Job's
podFailurePolicy (config/k8s/migrate-job.yaml):
    0   success or no-op
    10  lock timeout (another migrate run holds the advisory lock)      -> retry is safe
    20  refused: an applied revision's script checksum changed          -> page; never retry
    21  refused: contract gate (a live workload is older than required)  -> hold; ticket
    30  migration error (SQL failed; that revision's transaction rolled back) -> page
    40  drift: migrated schema differs from the models (--verify-drift)      -> page; never retry

Invocation (identical locally and in the Job; WS-11 compose `migrate` service, XD-11-12):
    replicant-db migrate --to head --advisory-lock --verify-drift [--ensure-roles]
    replicant-db migrate status
    replicant-db maintenance run --tasks=partition_create,partition_secure,partition_drop,...
--advisory-lock is always on (accepted for explicitness). --ensure-roles (set login-role
passwords from REPLICANT_DB_ROLE_PASSWORDS_DIR) is refused unless REPLICANT_ENV=local|ci: in AWS
every login role authenticates with IAM and has no password.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

# 64-bit key for pg_advisory_lock: first 8 bytes of sha256("replicant.migrate"). Same key in every
# environment; only one migrate process per database can hold it (session-level lock, released
# automatically if the pod dies because the session ends).
LOCK_KEY = int.from_bytes(hashlib.sha256(b"replicant.migrate").digest()[:8], "big", signed=True)

HERE = pathlib.Path(__file__).parent
EXIT_LOCK, EXIT_CHECKSUM, EXIT_CONTRACT_GATE, EXIT_SQL, EXIT_DRIFT = 10, 20, 21, 30, 40


@dataclass
class RunMeta:
    image_digest: str
    git_sha: str
    k8s_job: str | None
    argocd_app: str | None
    argocd_sync_rev: str | None


def _alembic_cfg() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE / "migrations"))
    return cfg


def _local() -> bool:
    return os.environ.get("REPLICANT_ENV") in {"local", "ci"}


def _meta() -> RunMeta:
    # Local/CI images are not pushed, so they have no registry digest; compose passes a short git SHA as a
    # build arg. ops.migration_runs requires sha256:<64 hex> and 40 hex: zero-fill locally (DEVIATIONS PLATFORM-D4).
    digest = os.environ.get("REPLICANT_IMAGE_DIGEST") or ("sha256:" + "0" * 64 if _local() else None)
    sha = os.environ.get("REPLICANT_GIT_SHA") or ("0" * 40 if _local() else None)
    if digest is None or sha is None:
        raise SystemExit("REPLICANT_IMAGE_DIGEST and REPLICANT_GIT_SHA are required outside local/ci")
    if _local() and len(sha) < 40 and all(c in "0123456789abcdef" for c in sha):
        sha = sha.ljust(40, "0")
    return RunMeta(
        image_digest=digest,
        git_sha=sha,
        k8s_job=os.environ.get("POD_JOB_NAME"),
        argocd_app=os.environ.get("ARGOCD_APP_NAME"),
        argocd_sync_rev=os.environ.get("ARGOCD_APP_REVISION"),
    )


def _engine():
    """Direct to the Aurora WRITER endpoint (never a reader): session advisory lock and DDL.
    Env contract shared with every service and with WS-11 compose (XD-11-01, R1-251):
    REPLICANT_DB_DSN or REPLICANT_DB_HOST/PORT/NAME/USER, with REPLICANT_DB_PASSWORD_FILE locally and
    REPLICANT_DB_IAM_AUTH=true (IAM token as replicant_migrator) in AWS."""
    from replicant_db.session import sync_engine_from_env

    return sync_engine_from_env("replicant-db-migrate")


def _live_reader_revisions() -> dict[str, str]:
    """Contract gate input: built schema revision of every live workload that reads/writes the DB.

    Every Deployment/StatefulSet in Replicant namespaces carries the annotation
    `replicant.example.com/schema-revision` (rendered by the Helm chart from the image's
    BUILT_REVISION). Pinned replicant-worker versions are separate Deployments per build ID, so
    old pinned workers are included until drained. Uses the in-cluster ServiceAccount with a
    read-only ClusterRole on apps/deployments (config/k8s/migrate-rbac.yaml)."""
    from kubernetes import client  # type: ignore[import-not-found]
    from kubernetes import config as kcfg

    kcfg.load_incluster_config()
    apps = client.AppsV1Api()
    out: dict[str, str] = {}
    for ns in os.environ.get("REPLICANT_DB_CLIENT_NAMESPACES", "").split(","):
        for d in apps.list_namespaced_deployment(ns).items:
            rev = (d.metadata.annotations or {}).get("replicant.example.com/schema-revision")
            if rev and (d.status.replicas or 0) > 0:
                out[f"{ns}/{d.metadata.name}"] = rev
    return out


def apply(target: str, lock_timeout_s: int, skip_contract_gate: bool, verify_drift: bool = False,
          ensure_roles: bool = False) -> int:
    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    meta = _meta()
    engine = _engine()
    with engine.connect() as conn:
        t0 = time.monotonic()
        deadline = t0 + lock_timeout_s
        while not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            if time.monotonic() > deadline:
                print(json.dumps({"event": "migrate.lock_timeout", "waited_s": lock_timeout_s}))
                return EXIT_LOCK
            time.sleep(2)
        conn.commit()
        lock_wait_ms = int((time.monotonic() - t0) * 1000)

        has_ops = conn.execute(text("SELECT to_regclass('ops.migration_runs') IS NOT NULL")).scalar()
        current = conn.execute(text("SELECT version_num FROM ops.alembic_version")).scalar() if has_ops else None
        head = script.get_current_head() if target == "head" else target

        if has_ops:
            # 1. Checksum guard: an applied revision's script must never change after apply.
            for rev, sha in conn.execute(text("SELECT revision, sql_sha256 FROM ops.migration_log WHERE downgraded_at IS NULL")):
                files = list((HERE / "migrations" / "versions").glob(f"{rev}*.py"))
                if files and hashlib.sha256(files[0].read_bytes()).hexdigest() != sha:
                    _finish(conn, None, current, head, meta, lock_wait_ms, "refused_checksum", "ChecksumMismatch", rev)
                    return EXIT_CHECKSUM
            # 2. Contract gate (schema-management.md §7.4).
            pending = [] if current == head else list(script.iterate_revisions(head, current or "base"))
            contracts = [s for s in pending if getattr(s.module, "replicant_compat", "") == "contract"]
            if contracts and not skip_contract_gate:
                live = _live_reader_revisions()
                for s in contracts:
                    needed = s.module.replicant_requires_reader_revision
                    ok_revs = {r.revision for r in script.iterate_revisions(head, needed)} | {needed}
                    stale = {w: r for w, r in live.items() if r not in ok_revs}
                    if stale:
                        _finish(conn, None, current, head, meta, lock_wait_ms, "refused_contract_gate",
                                "ContractGate", json.dumps({"revision": s.revision, "stale": stale})[:4000])
                        return EXIT_CONTRACT_GATE

        run_seq = None
        if has_ops:
            run_seq = conn.execute(
                text("""INSERT INTO ops.migration_runs (mode, from_revision, target_revision, image_digest, git_sha,
                        k8s_job, argocd_app, argocd_sync_rev, lock_wait_ms)
                        VALUES ('apply', :cur, :tgt, :img, :sha, :job, :app, :srev, :lw) RETURNING run_seq"""),
                {"cur": current, "tgt": head, "img": meta.image_digest, "sha": meta.git_sha, "job": meta.k8s_job,
                 "app": meta.argocd_app, "srev": meta.argocd_sync_rev, "lw": lock_wait_ms},
            ).scalar()
            conn.commit()
        else:
            # First run ever: ops does not exist yet. Pre-create the run row right after the bootstrap
            # revision creates the table (env.py reads config.attributes["run_meta"]).
            cfg.attributes["run_meta"] = meta

        cfg.attributes["connection"] = conn
        cfg.attributes["run_seq"] = run_seq
        try:
            if current != head:
                command.upgrade(cfg, head)
            elif not (ensure_roles or verify_drift):
                _finish(conn, run_seq, current, head, meta, lock_wait_ms, "noop", None, None)
                return 0
        except Exception as exc:  # noqa: BLE001 - classify and record, then fail the Job
            conn.rollback()
            _finish(conn, run_seq, current, head, meta, lock_wait_ms, "failed", type(exc).__name__, str(exc)[:4000])
            print(json.dumps({"event": "migrate.failed", "error": type(exc).__name__, "detail": str(exc)[:2000]}))
            return EXIT_SQL
        new = conn.execute(text("SELECT version_num FROM ops.alembic_version")).scalar()
        if ensure_roles:
            _ensure_role_passwords(conn)
        if verify_drift:
            try:
                command.check(cfg)  # autogenerate diff of models vs the migrated schema, same connection
            except Exception as exc:  # noqa: BLE001 - AutogenerateDiffsDetected
                _finish(conn, cfg.attributes.get("run_seq"), current, head, meta, lock_wait_ms, "failed",
                        "SchemaDrift", str(exc)[:4000], new)
                print(json.dumps({"event": "migrate.drift", "detail": str(exc)[:2000]}))
                return EXIT_DRIFT
        _finish(conn, cfg.attributes.get("run_seq"), current, head, meta, lock_wait_ms, "succeeded", None, None, new)
        if _local() and os.environ.get("REPLICANT_MIGRATE_LOCAL_MAINTENANCE", "true") == "true":
            # Local/CI have no hourly maintenance CronJob: create this month's (and premake) partitions and
            # isolate every leaf partition right after migrating, under the same advisory lock (PLATFORM-D5).
            _local_maintenance(conn)
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        conn.commit()
        print(json.dumps({"event": "migrate.succeeded", "from": current, "to": new}))
        return 0


def _local_maintenance(conn) -> None:  # noqa: ANN001
    import datetime as dt

    from replicant_db import maintenance as mnt

    with conn.begin():
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text("SELECT set_config('replicant.principal_scope', 'system:all-tenants', true)"))
        created = mnt.partition_create(conn, dt.date.today())
        mnt.tenant_partitions(conn)
        mnt.partition_secure(conn)
    print(json.dumps({"event": "migrate.local_maintenance", "partitions_created": len(created)}))


def _finish(conn, run_seq, current, head, meta, lock_wait_ms, outcome, err_class, err, new=None) -> None:  # noqa: ANN001
    if run_seq is None:
        if conn.execute(text("SELECT to_regclass('ops.migration_runs') IS NULL")).scalar():
            return
        run_seq = conn.execute(
            text("""INSERT INTO ops.migration_runs (mode, from_revision, target_revision, image_digest, git_sha,
                    k8s_job, lock_wait_ms) VALUES ('apply', :cur, :tgt, :img, :sha, :job, :lw) RETURNING run_seq"""),
            {"cur": current, "tgt": head, "img": meta.image_digest, "sha": meta.git_sha, "job": meta.k8s_job, "lw": lock_wait_ms},
        ).scalar()
    conn.execute(
        text("""UPDATE ops.migration_runs SET finished_at = clock_timestamp(), outcome = :o, to_revision = :new,
                error_class = :ec, error_summary = :es,
                applied = ARRAY(SELECT revision FROM ops.migration_log WHERE applied_in_run = :r ORDER BY applied_at)
                WHERE run_seq = :r"""),
        {"o": outcome, "new": new or current, "ec": err_class, "es": err, "r": run_seq},
    )
    conn.commit()


LOCAL_LOGIN_ROLES = (
    "svc_control_plane_api", "svc_identity_service", "svc_replicant_projector", "svc_memory_service",
    "svc_ecosystem_indexer", "svc_usage_ingestor", "svc_eval_worker", "svc_github_integration",
    "svc_slack_integration", "svc_jira_integration", "svc_github_token_broker", "svc_replicant_worker",
    "svc_publisher_worker", "svc_context_mcp", "svc_retention_sweeper", "svc_audit_exporter", "grafana_ro",
)


def _ensure_role_passwords(conn) -> None:  # noqa: ANN001
    """Local/CI only: roles exist (created by migrations); set their passwords from secret files
    named db_<role> in REPLICANT_DB_ROLE_PASSWORDS_DIR. Missing files are skipped."""
    if os.environ.get("REPLICANT_ENV") not in {"local", "ci"}:
        raise SystemExit("--ensure-roles is only allowed when REPLICANT_ENV is local or ci")
    from psycopg import sql  # ALTER ROLE takes no bind parameters: compose with safe literals

    d = pathlib.Path(os.environ["REPLICANT_DB_ROLE_PASSWORDS_DIR"])
    raw = conn.connection.driver_connection
    for role in LOCAL_LOGIN_ROLES:
        f = d / f"db_{role}"
        if f.exists():
            raw.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(f.read_text().strip())))
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="replicant-db")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate", help="apply migrations (default) or show status")
    m.add_argument("mode", nargs="?", default="apply", choices=["apply", "status"])
    m.add_argument("--to", "--target", dest="target", default="head")
    m.add_argument("--advisory-lock", action="store_true", default=True, help="always on; accepted for explicitness")
    m.add_argument("--lock-timeout", type=int, default=int(os.environ.get("REPLICANT_MIGRATE_LOCK_TIMEOUT_SECONDS", "600")))
    m.add_argument("--verify-drift", action="store_true", help="alembic check after upgrade; exit 40 on drift")
    m.add_argument("--ensure-roles", action="store_true", help="local/ci only: set login-role passwords from files")
    m.add_argument("--skip-contract-gate", action="store_true", help="local/ci only")
    mt = sub.add_parser("maintenance").add_subparsers(dest="mmode", required=True)
    run = mt.add_parser("run")
    run.add_argument("--tasks", default="partition_create,partition_secure,partition_drop")
    run.add_argument("--lock-timeout", type=int, default=30)
    run.add_argument("--max-reindex-per-run", type=int, default=1)
    args = p.parse_args(argv)
    if args.cmd == "migrate" and args.mode == "apply":
        if (args.skip_contract_gate or args.ensure_roles) and os.environ.get("REPLICANT_ENV") in {"dev", "staging", "prod"}:
            print("--skip-contract-gate/--ensure-roles are local/ci only", file=sys.stderr)
            return 2
        return apply(args.target, args.lock_timeout, args.skip_contract_gate, args.verify_drift, args.ensure_roles)
    if args.cmd == "migrate" and args.mode == "status":
        with _engine().connect() as conn:
            print(json.dumps(dict(conn.execute(text("SELECT * FROM ops.schema_status")).mappings().one()), default=str))
        return 0
    if args.cmd == "maintenance":
        import datetime as dt

        from replicant_db import maintenance as mnt
        with _engine().connect() as conn:
            if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
                print(json.dumps({"event": "maintenance.skipped", "reason": "migration in progress"}))
                return 0
            conn.commit()
            with conn.begin():
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text("SELECT set_config('replicant.principal_scope', 'system:all-tenants', true)"))
                tasks = set(args.tasks.split(","))
                today = dt.date.today()
                if "partition_create" in tasks:
                    mnt.partition_create(conn, today)
                if "partition_create" in tasks:  # XD-FIX-R2-051: per-tenant memory partitions
                    mnt.tenant_partitions(conn)
                if "partition_secure" in tasks:  # XD-ORCH-04 sweep: every leaf partition isolated
                    mnt.partition_secure(conn)
                if "partition_drop" in tasks:
                    mnt.partition_drop(conn, today, audit_export_verified=_audit_export_verified)
                    mnt.audit_drop(conn)  # XD-FIX-R1-194
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            conn.commit()
        return 0
    return 2


def _audit_export_verified(table: str, month) -> bool:  # noqa: ANN001
    """True iff s3://<audit bucket>/org/audit-exports/<table>/<YYYY-MM>/manifest.json exists and its
    row_count equals the partition's count (checked by the caller's next step). Implemented with
    replicant-storage; stubbed False here so nothing is ever dropped without the export."""
    return False


if __name__ == "__main__":
    sys.exit(main())
