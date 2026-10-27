// Browser-facing addresses of the local stack's operator UIs (spec/11-local-dev/stack.md ports, 127.0.0.1 binds).
export const LINKS = {
  temporalUi: "http://127.0.0.1:8233",
  temporalNamespace: "replicant-local",
  grafana: "http://127.0.0.1:3001",
  gitea: "http://127.0.0.1:3300",
  prometheus: "http://127.0.0.1:9090",
};

export const temporalWorkflowUrl = (jobId: string) =>
  `${LINKS.temporalUi}/namespaces/${LINKS.temporalNamespace}/workflows/${encodeURIComponent(jobId)}`;
export const grafanaJobUrl = (jobId: string) =>
  `${LINKS.grafana}/d/replicant-job?var-job_id=${encodeURIComponent(jobId)}`;
export const grafanaContainersUrl = () => `${LINKS.grafana}/d/replicant-containers`;

/** Gitea hosts as Replicant records them (in-network names) → the address a browser on this machine can open. */
const GIT_HOSTS = ["gitea:3300", "git.replicant.localhost:3300", "127.0.0.1:3300", "localhost:3300"];

export function browserGitUrl(u: string | undefined | null): string | undefined {
  if (!u) return undefined;
  for (const h of GIT_HOSTS) {
    for (const scheme of ["http://", "https://", ""]) {
      if (u.startsWith(scheme + h)) return LINKS.gitea + u.slice((scheme + h).length);
    }
  }
  return u.startsWith("http") ? u : undefined;
}

/** "git.replicant.localhost:3300/acme-payments/ledger-api" → "acme-payments/ledger-api". */
export function repoName(repo: string): string {
  const parts = repo.replace(/^https?:\/\//, "").split("/");
  return parts.length >= 3 ? parts.slice(-2).join("/") : repo;
}

export function repoShort(repo: string): string {
  return repo.split("/").pop() || repo;
}

export const repoUrl = (repo: string) => browserGitUrl(repo.startsWith("http") ? repo : "http://" + repo);
