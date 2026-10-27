"""Shared SCM abstraction (WS-07 github-app.md G-9; XD-11-05).

One normalized event model for every provider. The `gitea` adapter is the local-profile implementation; the `github`
adapter lands with M3. Everything after normalization (JobRequest, `pr.event`, gate decisions) is provider-agnostic.
"""
from replicant_scm.events import NormalizedEvent, ScmActor, ScmIssue, ScmPullRequest, ScmRepo
from replicant_scm.hosts import canonical_host, repo_ref
from replicant_scm.signatures import verify_gitea_signature, verify_github_signature

__all__ = [
    "NormalizedEvent", "ScmActor", "ScmIssue", "ScmPullRequest", "ScmRepo", "canonical_host", "repo_ref",
    "verify_gitea_signature", "verify_github_signature",
]
