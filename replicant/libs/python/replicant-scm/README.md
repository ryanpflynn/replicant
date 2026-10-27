# replicant-scm (CONTROL)

The shared SCM abstraction of github-app.md G-9 / XD-11-05: canonical repo host form (XD-FIX-R1-273), webhook
signature checks, the normalized event model (`NormalizedEvent`, GitHub `<event>.<action>` vocabulary), and the Gitea
adapter (`replicant_scm.gitea.normalize`, `GiteaClient`). Used by github-integration and github-token-broker; the
publisher may reuse `GiteaClient`. The github adapter lands with M3.

```sh
.venv-ctl/bin/python -m pytest -q libs/python/replicant-scm/tests
```
