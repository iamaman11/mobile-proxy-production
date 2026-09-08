#!/usr/bin/python3
"""Prepare the existing runner's proxy environment from its current WSL gateway.

No network probe, configuration change, recovery or phone access is performed.
The systemd manager reads the generated file before starting the listener.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

ENVIRONMENT_DIR = Path('/run/mobile-proxy-runner-proxy')
ENVIRONMENT_NAME = 'environment'
PROXY_PORT = 17890


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
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
            raise ValueError('RUNNER_PROXY_GATEWAY_NOT_PRIVATE_UNICAST')
        candidates.append((metric, str(address)))
    best = min(metric for metric, _ in candidates)
    gateways = {gateway for metric, gateway in candidates if metric == best}
    if len(gateways) != 1:
        raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_AMBIGUOUS')
    return gateways.pop()


def render_environment(gateway: str) -> str:
    # Validate again so this function cannot introduce EnvironmentFile syntax.
    gateway = select_gateway([{'dst': 'default', 'gateway': gateway}])
    http = f'http://{gateway}:{PROXY_PORT}'
    socks = f'socks5h://{gateway}:{PROXY_PORT}'
    values = {
        'http_proxy': http, 'https_proxy': http, 'HTTP_PROXY': http, 'HTTPS_PROXY': http,
        'all_proxy': socks, 'ALL_PROXY': socks,
        'no_proxy': 'localhost,127.0.0.1,::1', 'NO_PROXY': 'localhost,127.0.0.1,::1',
    }
    return ''.join(f'{key}={value}\n' for key, value in values.items())


def write_environment(directory: Path, content: str) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('RUNNER_PROXY_ENVIRONMENT_DIRECTORY_UNSAFE')
    target = directory / ENVIRONMENT_NAME
    if target.is_symlink():
        raise ValueError('RUNNER_PROXY_ENVIRONMENT_SYMLINK_REFUSED')
    fd, temporary = tempfile.mkstemp(prefix='.environment-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='ascii') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    if os.geteuid() != 0:
        print('RUNNER_PROXY_PREPARATION_REQUIRES_ROOT')
        return 1
    try:
        result = subprocess.run(
            ['/usr/sbin/ip', '-j', '-4', 'route', 'show', 'default'],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            raise ValueError('RUNNER_PROXY_DEFAULT_ROUTE_UNAVAILABLE')
        gateway = select_gateway(json.loads(result.stdout))
        write_environment(ENVIRONMENT_DIR, render_environment(gateway))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Exception details may contain host/network state; publish a category only.
        print('RUNNER_PROXY_ENVIRONMENT_PREPARATION_FAILED')
        return 1
    print('runner_proxy_environment=prepared')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
