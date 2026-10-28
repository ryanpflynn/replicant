"""stdout JSON log lines of the Python services and workers, redacted by `RedactingFormatter`.

One formatter with switches for the fields each service has always written (field names and order unchanged):

    {"ts"?, "level", "logger", "service"?, <log context>?, "msg", <record keys>?, <log context>?, "exc"?}

`core.JsonLogFormatter` is a different formatter: the spec reference for node logs.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterable
from typing import Literal


def _log_ctx(record: logging.LogRecord) -> dict:
    """job_id / run_id / trace_id / span_id of the line (replicant_telemetry.runtime); {} on any error."""
    try:
        from replicant_telemetry import log_context
        return log_context(record)
    except Exception:  # noqa: BLE001 - logging must never fail
        return {}


class JsonFormatter(logging.Formatter):
    """* `service`: adds `"service": <name>`.
    * `ts`: adds `"ts"` (logging's default asctime).
    * `merge_json`: a message that is a JSON object is merged into the line instead of `msg` (True: any logger; a
      string: only that logger's lines); the merged line has no `exc`.
    * `context`: the log context (`log_context`) right after the base fields ("head") or after `msg` and the
      record keys, without overriding them ("tail").
    * `record_keys`: record attributes added when truthy (e.g. `extra={"job_id": ...}`).
    * `exc_limit`: the formatted traceback's last N characters as `exc`; None omits tracebacks."""

    def __init__(self, service: str | None = None, *, ts: bool = False, merge_json: bool | str = False,
                 context: Literal["head", "tail"] | None = None, record_keys: Iterable[str] = (),
                 exc_limit: int | None = 4000) -> None:
        super().__init__()
        self.service, self.ts, self.merge_json, self.context = service, ts, merge_json, context
        self.record_keys, self.exc_limit = tuple(record_keys), exc_limit

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        out: dict = {"ts": self.formatTime(record)} if self.ts else {}
        out.update(level=record.levelname.lower(), logger=record.name)
        if self.service:
            out["service"] = self.service
        if self.context == "head":
            out.update(_log_ctx(record))
        if self.merge_json and msg.startswith("{") and (self.merge_json is True or record.name == self.merge_json):
            try:
                return json.dumps({**out, **json.loads(msg)})
            except ValueError:
                pass
        out["msg"] = msg
        for k in self.record_keys:
            if getattr(record, k, None):
                out[k] = getattr(record, k)
        if self.context == "tail":
            out.update({k: v for k, v in _log_ctx(record).items() if k not in out})
        if record.exc_info and self.exc_limit:
            out["exc"] = self.formatException(record.exc_info)[-self.exc_limit:]
        return json.dumps(out)


def json_logging(formatter: logging.Formatter, *, format_env: Literal["ignore", "json_or_default", "json_or_plain"]
                 = "ignore", filters: Iterable[logging.Filter] = ()) -> None:
    """Root logging to stdout through `RedactingFormatter(formatter)` at REPLICANT_LOG_LEVEL (default INFO).

    `format_env` reads REPLICANT_LOG_FORMAT (default json): "json_or_default" leaves the handler's default format
    for any other value, "json_or_plain" uses a redacted `asctime level name message` line."""
    from replicant_telemetry.redact import RedactingFormatter
    h = logging.StreamHandler(sys.stdout)
    for f in filters:
        h.addFilter(f)
    if format_env == "ignore" or os.environ.get("REPLICANT_LOG_FORMAT", "json") == "json":
        h.setFormatter(RedactingFormatter(formatter))
    elif format_env == "json_or_plain":
        h.setFormatter(RedactingFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")))
    logging.basicConfig(level=os.environ.get("REPLICANT_LOG_LEVEL", "INFO"), handlers=[h], force=True)


__all__ = ["JsonFormatter", "json_logging"]
