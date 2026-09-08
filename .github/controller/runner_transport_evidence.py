from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

ASSIGNMENT_NORMAL_SLO_MS = 20_000
ASSIGNMENT_BOUNDED_SLO_MS = 60_000
REPEATED_ERROR_THRESHOLD = 2
MAX_WORKER_LOGS = 32
MAX_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
_CONTEXT_RADIUS = 4

_TIMESTAMP_RE = re.compile(r"(?P<value>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
_RECORD_RE = re.compile(
    r"^\[(?:(?:RUNNER|WORKER)\s+)?"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+"
    r"(?P<level>[A-Z]+)\s+(?P<component>[^\]]+)\]\s*(?P<message>.*)$",
    re.IGNORECASE,
)
_RUNNER_VERSION_RE = re.compile(
    r"(?:current\s+runner\s+version|runner\s+version)\s*:\s*['\"]?(?P<version>\d+\.\d+\.\d+)",
    re.IGNORECASE,
)
_SESSION_SUCCESS_PATTERNS = (
    "listening for jobs",
    "message queue loop started",
    "session created",
    "connected to github",
    "connected to the broker",
    "connection established",
)

_COUNTER_KEYS = (
    "broker_reconnect",
    "runserver_reconnect",
    "tls_error",
    "eof_error",
    "connection_reset",
    "action_download_error",
    "artifact_transport_error",
    "transport_error_lines",
)

_FAILURE_CLASS_BY_COUNTER = {
    "broker_reconnect": "BROKER_RECONNECT",
    "runserver_reconnect": "RUNSERVER_RECONNECT",
    "tls_error": "TLS_ERROR",
    "eof_error": "EOF_ERROR",
    "connection_reset": "CONNECTION_RESET",
    "action_download_error": "ACTION_DOWNLOAD_TRANSPORT",
    "artifact_transport_error": "ARTIFACT_TRANSPORT",
}


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.search(line)
    if match is None:
        return None
    try:
        normalized = match.group("value").replace(" ", "T").replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except ValueError:
        return None


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_record(line: str) -> tuple[str, str] | None:
    match = _RECORD_RE.match(line.rstrip("\r\n"))
    if match is None:
        return None
    return match.group("component").strip().lower(), match.group("message").strip().lower()


def _first_timestamp(path: Path) -> datetime | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(512):
                line = handle.readline()
                if not line:
                    break
                parsed = _parse_timestamp(line)
                if parsed is not None:
                    return parsed
    except OSError:
        return None
    return None


def _safe_recent_paths(diag: Path, pattern: str) -> list[Path]:
    entries: list[tuple[int, Path]] = []
    try:
        candidates = list(diag.glob(pattern))
    except OSError:
        return []
    for path in candidates:
        try:
            entries.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in entries]


def _select_current_listener(listeners: list[Path]) -> tuple[Path | None, bool]:
    """Select by embedded session timestamp; mtime is only an ambiguity signal."""
    candidates: list[tuple[datetime, int, Path]] = []
    for path in listeners:
        started = _first_timestamp(path)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if started is None:
            continue
        candidates.append((started, mtime, path))
    if not candidates:
        return None, True

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected_started, selected_mtime, selected = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == selected_started:
        return selected, True

    for path in listeners:
        if path == selected:
            continue
        if _first_timestamp(path) is not None:
            continue
        try:
            if path.stat().st_mtime_ns > selected_mtime:
                return selected, True
        except OSError:
            return selected, True
    return selected, False


def _line_flags(line: str) -> set[str]:
    parsed = _parse_record(line)
    if parsed is None:
        return set()
    component, message = parsed
    flags: set[str] = set()

    broker = "brokerserver" in component or "brokermessagelistener" in component
    runserver = "runserver" in component or "run server" in component
    retryish = any(item in message for item in ("reconnect", "retry", "backoff", "back-off"))
    if broker and retryish:
        flags.add("broker_reconnect")
    if runserver and retryish:
        flags.add("runserver_reconnect")

    if any(
        item in message
        for item in (
            "ssl connection could not be established",
            "tls handshake",
            "tls connection",
            "authenticationexception",
            "certificate verify failed",
        )
    ):
        flags.add("tls_error")
    if any(item in message for item in ("unexpected eof", "unexpected end of file", "zero bytes from the transport stream")):
        flags.add("eof_error")
    if any(item in message for item in ("econnreset", "connection reset by peer", "connection was reset", "forcibly closed by the remote host")):
        flags.add("connection_reset")
    if (
        "failed to resolve action download info" in message
        or "failed to download action" in message
        or ("action download" in message and "error" in message)
    ):
        flags.add("action_download_error")
    if "failed to createartifact" in message or (
        "artifact" in message and any(item in message for item in ("econnreset", "connection reset", "unable to make request"))
    ):
        flags.add("artifact_transport_error")

    if flags:
        flags.add("transport_error_lines")
    return flags


def _expected_local_poll_cancellation(lines: list[str], index: int) -> bool:
    current = _parse_record(lines[index])
    if current is None:
        return False
    component, message = current
    if "brokerserver" not in component:
        return False
    if not any(item in message for item in ("retry", "backoff", "back-off")):
        return False

    start = max(0, index - _CONTEXT_RADIUS)
    end = min(len(lines), index + _CONTEXT_RADIUS + 1)
    parsed_window = [_parse_record(line) for line in lines[start:end]]
    window = " ".join(
        f"{component_value} {message_value}"
        for item in parsed_window
        if item is not None
        for component_value, message_value in (item,)
    )

    socket_125 = "socketexception" in window and re.search(r"(?<!\d)125(?!\d)", window) is not None
    operation_cancelled = any(
        item in window
        for item in (
            "operation canceled",
            "operation cancelled",
            "operation was canceled",
            "operation was cancelled",
        )
    )
    local_cancellation = any(
        item in window
        for item in (
            "local token",
            "localtoken",
            "cancellationtoken",
            "onjobstatus",
        )
    )
    demonstrated_outer_reconnect = any(
        item in window
        for item in (
            "get next message failed",
            "getnextmessage failed",
            "reconnecting to broker",
            "broker reconnect loop",
        )
    )
    return socket_125 and operation_cancelled and local_cancellation and not demonstrated_outer_reconnect


def _scan_file(
    path: Path,
    *,
    remaining_bytes: int,
    counters: dict[str, int],
    listener: bool,
    context_counters: dict[str, int] | None = None,
) -> tuple[int, bool, str | None, datetime | None]:
    consumed = 0
    truncated = False
    runner_version: str | None = None
    last_session_success: datetime | None = None
    lines: list[str] = []
    try:
        with path.open("rb") as handle:
            while True:
                raw = handle.readline()
                if not raw:
                    break
                if consumed + len(raw) > remaining_bytes:
                    truncated = True
                    break
                consumed += len(raw)
                line = raw.decode("utf-8", errors="replace")
                lines.append(line)
                if runner_version is None:
                    match = _RUNNER_VERSION_RE.search(line)
                    if match is not None:
                        runner_version = match.group("version")
                if listener:
                    parsed_record = _parse_record(line)
                    if parsed_record is not None:
                        _, message = parsed_record
                        if any(pattern in message for pattern in _SESSION_SUCCESS_PATTERNS):
                            parsed = _parse_timestamp(line)
                            if parsed is not None and (last_session_success is None or parsed > last_session_success):
                                last_session_success = parsed
    except OSError:
        truncated = True

    for index, line in enumerate(lines):
        flags = _line_flags(line)
        if "broker_reconnect" in flags and _expected_local_poll_cancellation(lines, index):
            flags.discard("broker_reconnect")
            if context_counters is not None:
                context_counters["expected_local_poll_cancellation"] += 1
            if flags == {"transport_error_lines"}:
                flags.clear()
        for key in flags:
            counters[key] += 1

    return consumed, truncated, runner_version, last_session_success


def _derive_diag_dir(runner_temp: Path) -> Path | None:
    try:
        value = runner_temp.resolve()
    except OSError:
        return None
    if value.name != "_temp" or value.parent.name != "_work":
        return None
    diag = value.parent.parent / "_diag"
    if not diag.is_dir():
        return None
    return diag


def collect_runner_transport_evidence(
    *,
    runner_temp: Path,
    assignment_latency_ms: int,
    include_context: bool = False,
) -> dict[str, object]:
    counters = {key: 0 for key in _COUNTER_KEYS}
    context_counters = {"expected_local_poll_cancellation": 0}
    evidence: dict[str, object] = {
        "diagnostic_window": "current_listener_session",
        "diagnostic_evidence_available": False,
        "diagnostics_truncated": False,
        "runner_version": None,
        "listener_session_started_at_utc": None,
        "last_successful_session_establishment_at_utc": None,
        "listener_files_scanned": 0,
        "worker_files_scanned": 0,
        "error_counters": counters,
        "failure_classes": [],
        "assignment_normal_slo_ms": ASSIGNMENT_NORMAL_SLO_MS,
        "assignment_bounded_slo_ms": ASSIGNMENT_BOUNDED_SLO_MS,
        "assignment_slo_exceeded": assignment_latency_ms > ASSIGNMENT_BOUNDED_SLO_MS,
        "repeated_transport_error": False,
        "transport_degraded": False,
    }
    if include_context:
        evidence["context_counters"] = context_counters

    diag = _derive_diag_dir(runner_temp)
    if diag is None:
        evidence["failure_classes"] = ["DIAGNOSTIC_EVIDENCE_UNAVAILABLE"]
        evidence["transport_degraded"] = True
        return evidence

    listeners = _safe_recent_paths(diag, "Runner_*.log")
    if not listeners:
        evidence["failure_classes"] = ["DIAGNOSTIC_EVIDENCE_UNAVAILABLE"]
        evidence["transport_degraded"] = True
        return evidence

    listener_path, selection_unresolved = _select_current_listener(listeners)
    if listener_path is None:
        listener_path = listeners[0]
    session_started = _first_timestamp(listener_path)
    evidence["listener_session_started_at_utc"] = _format_timestamp(session_started)

    remaining = MAX_DIAGNOSTIC_BYTES
    used, truncated, version, last_success = _scan_file(
        listener_path,
        remaining_bytes=remaining,
        counters=counters,
        listener=True,
        context_counters=context_counters if include_context else None,
    )
    remaining -= used
    evidence["listener_files_scanned"] = 1
    evidence["runner_version"] = version
    evidence["last_successful_session_establishment_at_utc"] = _format_timestamp(last_success)
    evidence["diagnostics_truncated"] = truncated

    window_unresolved = selection_unresolved or session_started is None
    workers: list[Path] = []
    if not window_unresolved:
        for candidate in _safe_recent_paths(diag, "Worker_*.log"):
            started = _first_timestamp(candidate)
            if started is None:
                window_unresolved = True
                workers = []
                break
            if started < session_started:
                continue
            if len(workers) >= MAX_WORKER_LOGS:
                evidence["diagnostics_truncated"] = True
                break
            workers.append(candidate)

    for worker in reversed(workers):
        if remaining <= 0:
            evidence["diagnostics_truncated"] = True
            break
        used, worker_truncated, worker_version, _ = _scan_file(
            worker,
            remaining_bytes=remaining,
            counters=counters,
            listener=False,
            context_counters=context_counters if include_context else None,
        )
        remaining -= used
        if evidence["runner_version"] is None and worker_version is not None:
            evidence["runner_version"] = worker_version
        if worker_truncated:
            evidence["diagnostics_truncated"] = True
            break
        evidence["worker_files_scanned"] = int(evidence["worker_files_scanned"]) + 1

    evidence["diagnostic_evidence_available"] = True
    failure_classes = sorted(
        value for key, value in _FAILURE_CLASS_BY_COUNTER.items() if counters.get(key, 0) > 0
    )
    if window_unresolved:
        failure_classes.append("SESSION_WINDOW_UNRESOLVED")
    if bool(evidence["diagnostics_truncated"]):
        failure_classes.append("DIAGNOSTIC_WINDOW_TRUNCATED")
    if evidence["runner_version"] is None:
        failure_classes.append("RUNNER_VERSION_UNAVAILABLE")
    if evidence["last_successful_session_establishment_at_utc"] is None:
        failure_classes.append("SESSION_ESTABLISHMENT_UNOBSERVED")

    repeated = (
        counters["transport_error_lines"] >= REPEATED_ERROR_THRESHOLD
        or counters["broker_reconnect"] >= REPEATED_ERROR_THRESHOLD
        or counters["runserver_reconnect"] >= REPEATED_ERROR_THRESHOLD
    )
    evidence["repeated_transport_error"] = repeated
    evidence["failure_classes"] = sorted(set(failure_classes))
    evidence["transport_degraded"] = bool(
        evidence["assignment_slo_exceeded"]
        or repeated
        or window_unresolved
        or evidence["diagnostics_truncated"]
        or evidence["runner_version"] is None
        or evidence["last_successful_session_establishment_at_utc"] is None
    )
    return evidence


def classify_preflight(*, phone_ready: bool, transport: dict[str, object]) -> str:
    if not phone_ready:
        return "NOT_READY"
    if transport.get("transport_degraded") is True:
        return "TRANSPORT_DEGRADED"
    return "READY"
