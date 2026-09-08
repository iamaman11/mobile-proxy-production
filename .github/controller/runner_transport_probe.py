from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "production" / "runner-transport-policy.json"
RUNNER_SERVICE = "mobile-proxy-phone-runner.service"
WATCHDOG_TIMER = "mobile-proxy-runner-transport-health.timer"
WATCHDOG_PROJECTION = Path("/run/mobile-proxy-runner-health/observation.json")
WATCHDOG_PROJECTION_SCHEMA = "runner-transport-health-projection.v1"
_MAX_PROJECTION_BYTES = 4096
_MAX_PROJECTION_COUNTER = 1000
_WATCHDOG_DECISIONS = frozenset({"HEALTHY", "OBSERVE", "RESTART_ELIGIBLE", "RATE_LIMITED", "UNKNOWN"})

_PROXY_KEYS = frozenset({
    "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
})
_CUSTOM_CA_KEYS = frozenset({
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
    "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
})
_GIT_TLS_OVERRIDE_KEYS = frozenset({"GIT_SSL_NO_VERIFY", "GIT_SSL_CAINFO", "GIT_HTTP_SSLVERIFY"})
_RUNTIME_TLS_OVERRIDE_KEYS = frozenset({"NODE_TLS_REJECT_UNAUTHORIZED", "PYTHONHTTPSVERIFY"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

_ALLOWED_PROBE_RESULTS = frozenset({
    "SUCCESS", "PROXY_DNS_FAILURE", "DNS_FAILURE", "CONNECT_FAILURE", "TLS_FAILURE",
    "TIMEOUT", "TRANSPORT_FAILURE", "TOOL_UNAVAILABLE", "UNKNOWN_FAILURE", "BUDGET_EXHAUSTED",
})

CommandRunner = Callable[[Sequence[str], int, Mapping[str, str] | None], tuple[int, str]]


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("RUNNER_TRANSPORT_POLICY_UNAVAILABLE") from exc
    expected = {
        "schema", "active_probe_budget_seconds", "attempt_timeout_seconds", "max_destinations",
        "max_attempts_per_destination", "max_concurrency", "tls_verification_required",
        "classifications", "failure_domains", "destinations",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("RUNNER_TRANSPORT_POLICY_SCHEMA_INVALID")
    if value.get("schema") != "runner-transport-policy.v1":
        raise ValueError("RUNNER_TRANSPORT_POLICY_SCHEMA_INVALID")
    if value.get("tls_verification_required") is not True:
        raise ValueError("RUNNER_TRANSPORT_TLS_VERIFICATION_MUST_BE_ENABLED")

    numeric_limits = {
        "active_probe_budget_seconds": (1, 120),
        "attempt_timeout_seconds": (1, 10),
        "max_destinations": (1, 5),
        "max_attempts_per_destination": (1, 3),
        "max_concurrency": (1, 2),
    }
    for field, (minimum, maximum) in numeric_limits.items():
        item = value.get(field)
        if type(item) is not int or not minimum <= item <= maximum:
            raise ValueError("RUNNER_TRANSPORT_POLICY_LIMIT_INVALID")

    if value.get("classifications") != ["HEALTHY", "TRANSPORT_DEGRADED", "TRANSPORT_UNAVAILABLE"]:
        raise ValueError("RUNNER_TRANSPORT_POLICY_CLASSIFICATION_INVALID")
    if value.get("failure_domains") != ["UNKNOWN", "RUNNER_SERVICE", "DNS_RESOLUTION", "TLS_VALIDATION"]:
        raise ValueError("RUNNER_TRANSPORT_POLICY_FAILURE_DOMAIN_INVALID")

    destinations = value.get("destinations")
    if not isinstance(destinations, list) or not destinations or len(destinations) > int(value["max_destinations"]):
        raise ValueError("RUNNER_TRANSPORT_POLICY_DESTINATIONS_INVALID")
    roles: set[str] = set()
    hosts: set[str] = set()
    for destination in destinations:
        if not isinstance(destination, dict) or set(destination) != {"role", "url"}:
            raise ValueError("RUNNER_TRANSPORT_POLICY_DESTINATION_INVALID")
        role = destination.get("role")
        url = destination.get("url")
        if not isinstance(role, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", role) or role in roles:
            raise ValueError("RUNNER_TRANSPORT_POLICY_DESTINATION_INVALID")
        if not isinstance(url, str) or len(url) > 256:
            raise ValueError("RUNNER_TRANSPORT_POLICY_DESTINATION_INVALID")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.path != "/" or parsed.query or parsed.fragment or "*" in parsed.hostname
        ):
            raise ValueError("RUNNER_TRANSPORT_POLICY_DESTINATION_INVALID")
        host = parsed.hostname.lower()
        if host in hosts:
            raise ValueError("RUNNER_TRANSPORT_POLICY_DESTINATION_INVALID")
        roles.add(role)
        hosts.add(host)

    theoretical = (
        ((len(destinations) + int(value["max_concurrency"]) - 1) // int(value["max_concurrency"]))
        * int(value["max_attempts_per_destination"])
        * int(value["attempt_timeout_seconds"])
    )
    if theoretical > int(value["active_probe_budget_seconds"]):
        raise ValueError("RUNNER_TRANSPORT_POLICY_BUDGET_INCONSISTENT")
    return value


def _run(command: Sequence[str], timeout_seconds: int, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""
    return completed.returncode, completed.stdout[:4096]


def _systemctl_value(unit: str, prop: str, runner: CommandRunner) -> str | None:
    code, stdout = runner(("systemctl", "show", unit, f"--property={prop}", "--value"), 3, None)
    if code != 0:
        return None
    value = stdout.strip()
    if not value or len(value) > 128:
        return None
    return value


def _read_environment_names(path: Path) -> set[str] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    names: set[str] = set()
    for field in raw.split(b"\0"):
        if b"=" not in field:
            continue
        raw_name = field.split(b"=", 1)[0]
        try:
            name = raw_name.decode("ascii")
        except UnicodeDecodeError:
            continue
        if _ENV_NAME.fullmatch(name):
            names.add(name)
    return names


def _presence(names: set[str]) -> dict[str, bool]:
    return {
        "proxy_present": bool(names & _PROXY_KEYS),
        "custom_ca_present": bool(names & _CUSTOM_CA_KEYS),
        "git_tls_override_present": bool(names & _GIT_TLS_OVERRIDE_KEYS),
        "runtime_tls_override_present": bool(names & _RUNTIME_TLS_OVERRIDE_KEYS),
    }


def collect_runner_runtime(
    *,
    runner: CommandRunner = _run,
    environment: Mapping[str, str] | None = None,
    proc_root: Path = Path("/proc"),
) -> tuple[dict[str, object], dict[str, object]]:
    environment = dict(os.environ if environment is None else environment)
    active = _systemctl_value(RUNNER_SERVICE, "ActiveState", runner)
    sub = _systemctl_value(RUNNER_SERVICE, "SubState", runner)
    pid_text = _systemctl_value(RUNNER_SERVICE, "MainPID", runner)
    main_pid = int(pid_text) if pid_text is not None and pid_text.isdigit() and int(pid_text) > 0 else None

    if active == "active":
        service_state = "ACTIVE"
    elif active in {"inactive", "failed", "deactivating", "activating"}:
        service_state = "INACTIVE"
    else:
        service_state = "UNKNOWN"

    direct_names: set[str] | None = None
    if main_pid is not None:
        direct_names = _read_environment_names(proc_root / str(main_pid) / "environ")
    observer_names = {name for name in environment if _ENV_NAME.fullmatch(name)}
    selected_names = direct_names if direct_names is not None else observer_names

    runner_state: dict[str, object] = {
        "service_state": service_state,
        "service_substate_observed": sub is not None,
        "runner_activity": "BUSY" if environment.get("GITHUB_ACTIONS", "").lower() == "true" else "UNKNOWN",
        "service_environment_observed": direct_names is not None,
    }
    environment_presence: dict[str, object] = {
        **_presence(selected_names),
        "source": "SERVICE_PROCESS" if direct_names is not None else "OBSERVER_PROCESS",
        "service_environment_observed": direct_names is not None,
    }
    if direct_names is not None:
        environment_presence["observer_environment_matches_presence"] = _presence(direct_names) == _presence(observer_names)
    else:
        environment_presence["observer_environment_matches_presence"] = None
    return runner_state, environment_presence


def _parse_watchdog_projection(path: Path, *, now_epoch: int) -> dict[str, object] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_PROJECTION_BYTES:
            return None
        if info.st_mode & 0o022:
            return None
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    expected = {
        "schema", "observed_at_epoch", "expires_at_epoch", "decision", "cooldown_active",
        "restart_budget_remaining", "post_restart_grace_active", "restart_count_window",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return None
    if value.get("schema") != WATCHDOG_PROJECTION_SCHEMA:
        return None
    observed = value.get("observed_at_epoch")
    expires = value.get("expires_at_epoch")
    if type(observed) is not int or type(expires) is not int or observed < 0 or expires <= observed:
        return None
    if not observed <= now_epoch <= expires:
        return None
    if value.get("decision") not in _WATCHDOG_DECISIONS:
        return None
    for key in ("cooldown_active", "post_restart_grace_active"):
        if type(value.get(key)) is not bool:
            return None
    for key in ("restart_budget_remaining", "restart_count_window"):
        item = value.get(key)
        if type(item) is not int or not 0 <= item <= _MAX_PROJECTION_COUNTER:
            return None
    return value


def collect_watchdog(
    *,
    runner: CommandRunner = _run,
    projection_path: Path = WATCHDOG_PROJECTION,
    now_epoch: int | None = None,
) -> dict[str, object]:
    now = int(time.time()) if now_epoch is None else now_epoch
    timer_state = _systemctl_value(WATCHDOG_TIMER, "ActiveState", runner)
    projection = _parse_watchdog_projection(projection_path, now_epoch=now)
    result: dict[str, object] = {
        "timer_state": "ACTIVE" if timer_state == "active" else ("INACTIVE" if timer_state else "UNKNOWN"),
        "state_readable": projection is not None,
        "decision": "UNKNOWN",
        "cooldown_active": None,
        "restart_budget_remaining": None,
        "post_restart_grace_active": None,
        "restart_count_window": None,
    }
    if projection is None:
        return result
    result.update({
        "decision": projection["decision"],
        "cooldown_active": projection["cooldown_active"],
        "restart_budget_remaining": projection["restart_budget_remaining"],
        "post_restart_grace_active": projection["post_restart_grace_active"],
        "restart_count_window": projection["restart_count_window"],
    })
    return result


def classify_curl_exit(code: int) -> str:
    if code == 0:
        return "SUCCESS"
    if code == 5:
        return "PROXY_DNS_FAILURE"
    if code == 6:
        return "DNS_FAILURE"
    if code == 7:
        return "CONNECT_FAILURE"
    if code == 28 or code == 124:
        return "TIMEOUT"
    if code in {35, 51, 53, 58, 59, 60, 77, 80, 82, 83, 90, 91}:
        return "TLS_FAILURE"
    if code in {52, 55, 56, 92, 95, 96, 97}:
        return "TRANSPORT_FAILURE"
    if code == 127:
        return "TOOL_UNAVAILABLE"
    return "UNKNOWN_FAILURE"


def _probe_one_role(
    destination: Mapping[str, object],
    *,
    attempts: int,
    timeout_seconds: int,
    deadline: float,
    runner: CommandRunner,
    environment: Mapping[str, str],
) -> dict[str, object]:
    role = str(destination["role"])
    url = str(destination["url"])
    records: list[dict[str, object]] = []
    for attempt in range(1, attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            records.append({"attempt": attempt, "result": "BUDGET_EXHAUSTED", "duration_ms": 0})
            break
        bounded_timeout = min(timeout_seconds, max(1, int(remaining)))
        started = time.monotonic()
        if shutil.which("curl") is None and runner is _run:
            code = 127
        else:
            code, _ = runner(
                (
                    "curl", "--silent", "--show-error", "--head", "--output", "/dev/null",
                    "--retry", "0", "--connect-timeout", str(bounded_timeout),
                    "--max-time", str(bounded_timeout), "--proto", "=https", url,
                ),
                bounded_timeout + 1,
                environment,
            )
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        result = classify_curl_exit(code)
        if result not in _ALLOWED_PROBE_RESULTS:
            raise AssertionError("runner transport probe result differs")
        records.append({"attempt": attempt, "result": result, "duration_ms": duration_ms})
        if result == "SUCCESS":
            break
    final = records[-1]["result"] if records else "BUDGET_EXHAUSTED"
    return {"role": role, "attempts": records, "final_result": final}


def run_active_probes(
    policy: Mapping[str, object],
    *,
    runner: CommandRunner = _run,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    environment = dict(os.environ if environment is None else environment)
    destinations = list(policy["destinations"])
    deadline = time.monotonic() + int(policy["active_probe_budget_seconds"])
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=int(policy["max_concurrency"])) as executor:
        futures = [
            executor.submit(
                _probe_one_role,
                destination,
                attempts=int(policy["max_attempts_per_destination"]),
                timeout_seconds=int(policy["attempt_timeout_seconds"]),
                deadline=deadline,
                runner=runner,
                environment=environment,
            )
            for destination in destinations
        ]
        for future in futures:
            results.append(future.result())
    results.sort(key=lambda item: str(item["role"]))
    retry_observed = any(len(item["attempts"]) > 1 for item in results)
    budget_exhausted = any(
        attempt["result"] == "BUDGET_EXHAUSTED"
        for item in results
        for attempt in item["attempts"]
    )
    return {
        "roles": results,
        "retry_observed": retry_observed,
        "budget_exhausted": budget_exhausted,
    }
