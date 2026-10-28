import json
import shutil
import stat
from pathlib import Path

from replicant_cli import compose, secrets
from replicant_cli.paths import Paths, repo_root

ROOT = repo_root()


def _fake_repo(tmp: Path) -> Paths:
    shutil.copytree(ROOT / "dev" / "compose" / "config" / "s3", tmp / "dev" / "compose" / "config" / "s3")
    shutil.copy(ROOT / "dev" / "compose" / "compose.yaml", tmp / "dev" / "compose" / "compose.yaml")
    return Paths(tmp)


def test_secrets_generated_complete_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "_podman_socket", lambda: "/run/user/501/podman/podman.sock")
    p = _fake_repo(tmp_path)
    created = secrets.ensure(p)
    assert created and secrets.ensure(p) == []          # idempotent
    import yaml
    doc = yaml.safe_load((ROOT / "dev" / "compose" / "compose.yaml").read_text())
    for name, spec in doc["secrets"].items():
        f = p.secrets / Path(spec["file"]).name
        assert f.is_file() and f.stat().st_size > 0, name
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, name
    jwks = json.loads((p.generated / "run-grant-jwks.json").read_text())
    assert jwks["keys"][0]["kid"] == "rg-local-1" and "d" not in jwks["keys"][0]
    s3 = json.loads((p.generated / "s3.json").read_text())
    shim = next(i for i in s3["identities"] if i["name"] == "gateway-shim")
    ini = (p.secrets / "s3_gateway_shim_credentials").read_text()
    assert shim["credentials"][0]["secretKey"] in ini
    assert "REPLICANT_DEV_HOME=../.secrets" in p.env_file.read_text()
    assert len(p.secret("temporal_codec_key")) == 64


def test_interpolate():
    env = {"A": "x"}
    assert compose.interpolate("${A:-d}/${B:-def}/${C}", env) == "x/def/"


def test_v0_profile_services():
    svcs = compose.load_services(Paths(ROOT), "v0")
    assert len(svcs) == 27 and "sbx-edge" in svcs and "memory-service" in svcs and "temporal-schema" not in svcs and "dex" not in svcs
    assert {"control-plane-web", "slack-integration", "slack-mock"} <= set(svcs)          # RENAME-D6, NOTIFY
    assert svcs["migrate"].dockerfile.name == "Dockerfile.migrate"


def test_import_provider_key_never_echoes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(secrets, "_podman_socket", lambda: "/run/x.sock")
    p = _fake_repo(tmp_path)
    secrets.ensure(p)
    assert secrets.import_provider_key(p) is False                     # no dev/.env
    assert (p.secrets / "anthropic_api_key").read_text() == ""          # mounted empty in script mode
    (tmp_path / "dev" / ".env").write_text('# dev keys\nexport ANTHROPIC_API_KEY="sk-ant-test-123"\n')
    assert secrets.import_provider_key(p) is True
    f = p.secrets / "anthropic_api_key"
    assert f.read_text() == "sk-ant-test-123" and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "sk-ant" not in capsys.readouterr().out
    secrets.set_env(p, {"REPLICANT_GATEWAY_MODE": "passthrough"})
    assert "REPLICANT_GATEWAY_MODE=passthrough" in p.env_file.read_text()


def test_gateway_mode_reads_compose_env(tmp_path, monkeypatch):
    from replicant_cli import testing
    monkeypatch.setattr(secrets, "_podman_socket", lambda: "/run/x.sock")
    p = _fake_repo(tmp_path)
    assert testing.gateway_mode(p) == "script"                         # no .env yet
    secrets.ensure(p)
    assert testing.gateway_mode(p) == "script"                         # default written by ensure
    secrets.set_env(p, {"REPLICANT_GATEWAY_MODE": "passthrough"})
    secrets.ensure(p)                                                  # `dev up` keeps it (FIX-D8)
    assert testing.gateway_mode(p) == "passthrough"


def test_auto_prune_only_below_threshold(tmp_path, monkeypatch):
    from replicant_cli import prune
    calls = []
    monkeypatch.setattr(prune, "run", lambda p, **kw: calls.append(kw) or 0)
    monkeypatch.setattr(prune, "free_gib", lambda: 50.0)
    assert prune.auto(Paths(tmp_path), lambda m: None) is False and not calls
    monkeypatch.setattr(prune, "free_gib", lambda: 4.0)
    msgs: list[str] = []
    assert prune.auto(Paths(tmp_path), msgs.append) is True
    assert calls == [calls[0]] and calls[0]["volumes"] is True and calls[0]["images"] is False and calls[0]["yes"]
    assert "auto-pruning" in msgs[0]


def test_worker_env_groups_select_and_deselect():
    from replicant_cli import testing
    names = [n for g, _ in testing.WORKER_ENV_GROUPS for n in g]
    assert {"license_prestep", "mid_run_can", "dependency_outage"} <= set(names)
    assert testing._scenario_k(("a", "b")) == "test_scenario and (a or b)"
