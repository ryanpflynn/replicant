"""Shared service plumbing: read_secret, iso, bearer tokens and the JSON log formatter."""
import datetime as dt
import json
import logging
import sys
from types import SimpleNamespace

from replicant_telemetry.common import bearer_ok, bearer_token, iso, read_secret, token_match, token_ok
from replicant_telemetry.jsonlog import JsonFormatter, json_logging


def test_read_secret(tmp_path):
    (tmp_path / "s").write_text("  tok \n")
    (tmp_path / "e").write_text("\n")
    assert read_secret(str(tmp_path / "s")) == "tok"
    assert read_secret(str(tmp_path / "e")) is None
    assert read_secret(str(tmp_path / "missing")) is None
    assert read_secret(str(tmp_path)) is None  # a directory
    assert read_secret(None) is None and read_secret("") is None


def test_iso():
    t = dt.datetime(2026, 10, 27, 12, 0, 0, 123456, tzinfo=dt.UTC)
    assert iso(t) == "2026-10-27T12:00:00.123456Z"
    assert iso(t, timespec="milliseconds") == "2026-10-27T12:00:00.123Z"
    plus2 = t.astimezone(dt.timezone(dt.timedelta(hours=2)))
    assert iso(plus2) == "2026-10-27T14:00:00.123456+02:00" and iso(plus2, utc=True) == iso(t)
    assert iso(t.replace(tzinfo=None), utc=True) == iso(t)
    assert iso(dt.date(2026, 1, 2)) == "2026-01-02"
    assert iso(None) is None and iso("x") == "x" and iso(3) == 3


def _req(auth: str | None):
    return SimpleNamespace(headers={"authorization": auth} if auth is not None else {})


def test_bearer():
    assert bearer_token(_req("Bearer  abc ")) == "abc" and bearer_token(_req("bearer x")) == "x"
    assert bearer_token(_req("Basic abc")) == "" and bearer_token(_req(None)) == ""
    assert bearer_ok(_req("Bearer s1"), "s1") and bearer_ok(_req("Bearer s2"), (None, "", "s2"))
    assert not bearer_ok(_req("Bearer s1"), None) and not bearer_ok(_req(None), "s1")
    assert not bearer_ok(_req("Bearer "), "") and not bearer_ok(_req("Bearer x"), ["y"])
    assert not token_ok("ünï", "x")  # non-ASCII never raises
    assert token_match("t2", {"t1": "a", "t2": "b"}) == "b" and token_match("", {"": "a"}) is None
    assert token_match("t3", {"t1": "a"}) is None


def _record(name: str, msg: str, exc: bool = False, **extra) -> logging.LogRecord:
    ei = None
    if exc:
        try:
            raise ValueError("boom")
        except ValueError:
            ei = sys.exc_info()
    r = logging.LogRecord(name, logging.WARNING, __file__, 1, msg, (), ei)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


def test_json_formatter_fields():
    f = JsonFormatter("svc")
    assert json.loads(f.format(_record("a.b", "hi"))) == {"level": "warning", "logger": "a.b", "service": "svc",
                                                          "msg": "hi"}
    assert "exc" in json.loads(f.format(_record("a", "x", exc=True)))
    assert "exc" not in json.loads(JsonFormatter(exc_limit=None).format(_record("a", "x", exc=True)))
    merged = JsonFormatter("svc", merge_json=True).format(_record("a", '{"msg": "m", "k": 1}'))
    assert json.loads(merged) == {"level": "warning", "logger": "a", "service": "svc", "msg": "m", "k": 1}
    audit_only = JsonFormatter("svc", merge_json="replicant.audit")
    assert json.loads(audit_only.format(_record("x", '{"k": 1}')))["msg"] == '{"k": 1}'
    assert json.loads(audit_only.format(_record("replicant.audit", '{"k": 1}')))["k"] == 1
    w = json.loads(JsonFormatter(ts=True, record_keys=("job_id", "run_id")).format(_record("w", "m", job_id="job_1")))
    assert list(w) == ["ts", "level", "logger", "msg", "job_id"]


def test_json_logging_redacts(capsys):
    json_logging(JsonFormatter("svc"))
    logging.getLogger("t").warning("Authorization: Bearer eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJydW4ifQ.c2lnbmF0dXJlLXg")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["service"] == "svc" and "eyJzdWIi" not in line["msg"]
