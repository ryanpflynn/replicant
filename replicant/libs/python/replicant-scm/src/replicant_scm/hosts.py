"""Canonical repository host form (XD-FIX-R1-273): the URL authority, lowercase, default port elided, non-default
port kept. So the local Gitea is `git.replicant.localhost:3300` everywhere (`repo.host`, `ReplicantRepos`, CLI `--repo`)."""
from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_host(value: str) -> str:
    """Accepts a bare authority (`Git.Replicant.Localhost:3300`) or a URL (`http://git.replicant.localhost:3300/x`)."""
    v = value.strip()
    if "://" not in v:
        v = "http://" + v
    parts = urlsplit(v)
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"no host in {value!r}")
    port = parts.port
    if port is None or _DEFAULT_PORTS.get(parts.scheme) == port:
        return host
    return f"{host}:{port}"


def repo_ref(host: str, owner: str, name: str) -> str:
    """`<host>/<owner>/<name>` as used by `ReplicantRepos` and `PrEventSignal.repo`."""
    return f"{canonical_host(host)}/{owner}/{name}"
