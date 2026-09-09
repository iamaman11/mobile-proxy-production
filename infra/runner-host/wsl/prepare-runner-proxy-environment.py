#!/usr/bin/python3
"""Manage the existing GitHub runner's service proxy in its supported .env file.

The helper runs only as the runner service's root ExecStartPre. It derives the
runner application directory from that service command's WorkingDirectory,
recomputes the current private WSL gateway, owns one bounded marker block in
the existing runner .env, preserves every unrelated line and file owner/mode,
and never prints proxy values. No network probe, recovery, phone or provider
access is performed.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

PROXY_PORT = 17890
MAX_ENV_BYTES = 65536
MANAGED_BEGIN = '# mobile-proxy-runner-proxy begin'
MANAGED_END = '# mobile-proxy-runner-proxy end'
MANAGED_KEYS = (
    'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
    'all_proxy', 'ALL_PROXY', 'no_proxy', 'NO_PROXY',
)
_MANAGED_KEY_SET = frozenset(MANAGED_KEYS)


def select_gateway(routes: object) -> str:
    if not isinstance(routes, list) or not routes:
        raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_UNAVAILABLE')
    candidates: list[tuple[int, str]] = []
    for route in routes:
        if not isinstance(route, dict) or route.get('dst') != 'default':
            raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_INVALID')
        if route.get('type', 'unicast') != 'unicast' or route.get('nexthops'):
            raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_UNSUPPORTED')
        metric = route.get('metric', 0)
        if type(metric) is not int or metric < 0:
            raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_INVALID')
        try:
            address = ipaddress.IPv4Address(route.get('gateway', ''))
        except ipaddress.AddressValueError:
            raise ValueError('RUNNER_PROXY_GATEWAY_INVALID') from None
        if (
            not address.is_private
            or address.is_loopback
            or address.is_unspecified
            or address.is_multicast
            or address.is_link_local
            or address.is_reserved
        ):
            raise ValueError('RUNNER_PROXY_GATEWAY_NOT_PRIVATE_UNICAST')
        candidates.append((metric, str(address)))
    best = min(metric for metric, _ in candidates)
    gateways = {gateway for metric, gateway in candidates if metric == best}
    if len(gateways) != 1:
        raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_AMBIGUOUS')
    return gateways.pop()


def render_environment(gateway: str) -> str:
    # Validate again so this function cannot introduce .env syntax.
    gateway = select_gateway([{'dst': 'default', 'gateway': gateway}])
    http = f'http://{gateway}:{PROXY_PORT}'
    socks = f'socks5h://{gateway}:{PROXY_PORT}'
    values = {
        'http_proxy': http,
        'https_proxy': http,
        'HTTP_PROXY': http,
        'HTTPS_PROXY': http,
        'all_proxy': socks,
        'ALL_PROXY': socks,
        'no_proxy': 'localhost,127.0.0.1,::1',
        'NO_PROXY': 'localhost,127.0.0.1,::1',
    }
    return ''.join(f'{key}={values[key]}\n' for key in MANAGED_KEYS)


def _line_key(line: str) -> str | None:
    value = line.rstrip('\r\n')
    if not value or value.lstrip().startswith('#') or '=' not in value:
        return None
    key = value.split('=', 1)[0]
    return key if key else None


def reconcile_environment(current: str, managed_environment: str | None) -> str:
    if '\x00' in current:
        raise ValueError('RUNNER_PROXY_ENVIRONMENT_INVALID')
    lines = current.splitlines(keepends=True)
    begin = [index for index, line in enumerate(lines) if line.rstrip('\r\n') == MANAGED_BEGIN]
    end = [index for index, line in enumerate(lines) if line.rstrip('\r\n') == MANAGED_END]
    if (not begin and end) or (begin and not end) or len(begin) > 1 or len(end) > 1:
        raise ValueError('RUNNER_PROXY_MANAGED_BLOCK_INVALID')

    block_start: int | None = None
    block_end: int | None = None
    if begin:
        block_start, block_end = begin[0], end[0]
        if block_start >= block_end:
            raise ValueError('RUNNER_PROXY_MANAGED_BLOCK_INVALID')
        inside = lines[block_start + 1:block_end]
        inside_keys = [_line_key(line) for line in inside]
        if inside_keys != list(MANAGED_KEYS):
            raise ValueError('RUNNER_PROXY_MANAGED_BLOCK_INVALID')

    for index, line in enumerate(lines):
        if block_start is not None and block_start <= index <= block_end:
            continue
        if _line_key(line) in _MANAGED_KEY_SET:
            raise ValueError('RUNNER_PROXY_EXISTING_ENV_CONFLICT')

    if managed_environment is None:
        if block_start is None:
            return current
        return ''.join(lines[:block_start] + lines[block_end + 1:])

    expected_keys = [_line_key(line) for line in managed_environment.splitlines(keepends=True)]
    if expected_keys != list(MANAGED_KEYS):
        raise ValueError('RUNNER_PROXY_RENDERED_ENVIRONMENT_INVALID')
    block = MANAGED_BEGIN + '\n' + managed_environment + MANAGED_END + '\n'
    if block_start is not None:
        return ''.join(lines[:block_start]) + block + ''.join(lines[block_end + 1:])

    prefix = current
    if prefix and not prefix.endswith(('\n', '\r')):
        prefix += '\n'
    return prefix + block


def runner_environment_path(working_directory: Path | None = None) -> Path:
    directory = Path.cwd() if working_directory is None else working_directory
    try:
        info = directory.lstat()
    except OSError as exc:
        raise ValueError('RUNNER_PROXY_WORKING_DIRECTORY_UNAVAILABLE') from exc
    if not directory.is_absolute() or not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
        raise ValueError('RUNNER_PROXY_WORKING_DIRECTORY_UNSAFE')
    return directory / '.env'


def _validate_runner_env(path: Path) -> os.stat_result:
    try:
        parent = path.parent.lstat()
        info = path.lstat()
    except OSError as exc:
        raise ValueError('RUNNER_PROXY_RUNNER_ENV_UNAVAILABLE') from exc
    if not stat.S_ISDIR(parent.st_mode) or path.parent.is_symlink():
        raise ValueError('RUNNER_PROXY_RUNNER_DIRECTORY_UNSAFE')
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError('RUNNER_PROXY_RUNNER_ENV_UNSAFE')
    if info.st_uid != parent.st_uid or info.st_mode & 0o022:
        raise ValueError('RUNNER_PROXY_RUNNER_ENV_UNSAFE')
    if info.st_size < 0 or info.st_size > MAX_ENV_BYTES:
        raise ValueError('RUNNER_PROXY_RUNNER_ENV_UNSAFE')
    return info


def _render_update(path: Path, managed_environment: str | None) -> tuple[os.stat_result, bytes, bytes]:
    info = _validate_runner_env(path)
    try:
        raw = path.read_bytes()
        current = raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise ValueError('RUNNER_PROXY_RUNNER_ENV_UNREADABLE') from exc
    updated = reconcile_environment(current, managed_environment)
    encoded = updated.encode('utf-8')
    if len(encoded) > MAX_ENV_BYTES:
        raise ValueError('RUNNER_PROXY_RUNNER_ENV_TOO_LARGE')
    return info, raw, encoded


def check_runner_environment(path: Path, managed_environment: str) -> None:
    _render_update(path, managed_environment)


def update_runner_environment(path: Path, managed_environment: str | None) -> None:
    info, raw, encoded = _render_update(path, managed_environment)
    if encoded == raw:
        return

    fd, temporary = tempfile.mkstemp(prefix='.env.mobile-proxy-', dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        os.fchown(fd, info.st_uid, info.st_gid)
        with os.fdopen(fd, 'wb', closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fd = -1
        os.replace(temporary, path)
        temporary = ''
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _gateway() -> str:
    result = subprocess.run(
        ['/usr/sbin/ip', '-j', '-4', 'route', 'show', 'default'],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_UNAVAILABLE')
    try:
        routes = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_INVALID') from None
    return select_gateway(routes)


def main() -> int:
    if os.geteuid() != 0:
        print('RUNNER_PROXY_PREPARATION_REQUIRES_ROOT')
        return 1
    if len(os.sys.argv) != 2 or os.sys.argv[1] not in {'--apply', '--remove', '--check'}:
        print('RUNNER_PROXY_PREPARATION_ARGUMENT_INVALID')
        return 1
    try:
        path = runner_environment_path()
        if os.sys.argv[1] == '--apply':
            update_runner_environment(path, render_environment(_gateway()))
            print('runner_proxy_env=applied')
        elif os.sys.argv[1] == '--remove':
            update_runner_environment(path, None)
            print('runner_proxy_env=removed')
        else:
            check_runner_environment(path, render_environment(_gateway()))
            print('runner_proxy_env=ready')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Details can contain local host/network state. Publish one bounded category only.
        print('RUNNER_PROXY_ENVIRONMENT_PREPARATION_FAILED')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
