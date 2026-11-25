"""Structured logging, data-quality counters, /health endpoint and the polling loop."""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    """Every log line is one JSON object, so it can be queried and alerted on.

    log_event() passes a dict as the message; it is merged into the line.
    Anything else (e.g. a library's plain-text warning) lands under "message".
    """

    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "service": self.service,
        }
        if isinstance(record.msg, dict):
            line.update(record.msg)
        else:
            line["message"] = record.getMessage()
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def setup_logging(service: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """log_event(log, logging.WARNING, "DATA_QUALITY_ERROR", source="NDW", value=-1)"""
    logger.log(level, {"event": event, **fields})


# --------------------------------------------------------------------------- #
# Data quality + health state (one instance per service = per source)
# --------------------------------------------------------------------------- #
class ServiceState:
    """In-memory counters that /health reports.

    bad_data_count is kept per source because each source runs in its own
    service (process): the air service's state is luchtmeetnet_bad_data_count,
    the traffic service's state is ndw_bad_data_count.
    """

    def __init__(self, source: str, logger: logging.Logger, poll_interval: float,
                 threshold: int = 10, window_seconds: int = 3600, clock=time.time):
        self.source = source
        self.log = logger
        self.poll_interval = poll_interval
        self.threshold = threshold
        self.window_seconds = window_seconds
        self.clock = clock
        self.started_at = clock()
        self.last_successful_fetch: str | None = None  # ISO time of our last good fetch
        self._last_success_epoch: float | None = None
        self.bad_data_count = 0                         # since the service started
        self._bad_times: deque[float] = deque()         # for the rolling 1-hour window
        self._threshold_alerted = False
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            now = self.clock()
            self._last_success_epoch = now
            self.last_successful_fetch = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")

    def record_bad_data(self, n: int = 1) -> None:
        """Count bad readings; log ONE error when more than `threshold` occur within an hour."""
        with self._lock:
            now = self.clock()
            self._prune(now)  # also re-arms the alert if the last hour has calmed down
            self.bad_data_count += n
            self._bad_times.extend([now] * n)
            in_window = len(self._bad_times)
            if in_window > self.threshold and not self._threshold_alerted:
                self._threshold_alerted = True  # one ERROR per incident, not one per bad reading
                log_event(self.log, logging.ERROR, "BAD_DATA_THRESHOLD_EXCEEDED",
                          source=self.source, count=in_window, window_minutes=self.window_seconds // 60)

    def bad_data_last_hour(self) -> int:
        with self._lock:
            self._prune(self.clock())
            return len(self._bad_times)

    def _prune(self, now: float) -> None:
        while self._bad_times and now - self._bad_times[0] > self.window_seconds:
            self._bad_times.popleft()
        if len(self._bad_times) <= self.threshold:
            self._threshold_alerted = False

    def health(self) -> tuple[int, dict]:
        """(HTTP status, body). Healthy = a successful fetch within ~2 poll intervals."""
        now = self.clock()
        grace = 2 * self.poll_interval + 300
        if self._last_success_epoch is not None and now - self._last_success_epoch <= grace:
            status, code = "ok", 200
        elif self._last_success_epoch is None and now - self.started_at <= grace:
            status, code = "starting", 200
        else:
            status, code = "stale", 503
        return code, {
            "source": self.source,
            "status": status,
            "last_successful_fetch": self.last_successful_fetch,
            "bad_data_count": self.bad_data_count,
            "bad_data_count_last_hour": self.bad_data_last_hour(),
        }


def start_health_server(state: ServiceState, port: int = 8000) -> ThreadingHTTPServer:
    """Serve GET /health on a background thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (name required by http.server)
            if self.path.rstrip("/") != "/health":
                self.send_error(404)
                return
            code, body = state.health()
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # silence http.server's plain-text access log
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    return server


# --------------------------------------------------------------------------- #
# Polling loop
# --------------------------------------------------------------------------- #
def run_forever(cycle, interval_seconds: float, logger: logging.Logger) -> None:
    """Run cycle() now, then every interval_seconds, until SIGTERM/SIGINT.

    Docker sends SIGTERM on `docker compose stop`; without a handler, Python as
    PID 1 would ignore it and get killed 10 s later mid-write.
    """
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        started = time.monotonic()
        try:
            cycle()
        except Exception:  # noqa: BLE001 - one bad cycle must not kill the service
            logger.exception({"event": "cycle_crashed"})
        elapsed = time.monotonic() - started
        stop.wait(max(0.0, interval_seconds - elapsed))
    log_event(logger, logging.INFO, "service_stopped")
