from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
HOST = ROOT / 'infra/runner-host/wsl'
spec = importlib.util.spec_from_file_location('runner_proxy_env', HOST / 'prepare-runner-proxy-environment.py')
assert spec is not None and spec.loader is not None
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class RunnerProxyEnvironmentTests(unittest.TestCase):
    def test_lowest_metric_gateway_is_recomputed(self):
        routes = [
            {'dst': 'default', 'gateway': '172.22.0.1', 'metric': 200},
            {'dst': 'default', 'gateway': '172.25.0.1', 'metric': 100},
        ]
        self.assertEqual(proxy.select_gateway(routes), '172.25.0.1')
        routes[1]['gateway'] = '172.26.0.1'
        self.assertEqual(proxy.select_gateway(routes), '172.26.0.1')

    def test_missing_ambiguous_or_injectable_route_fails_closed(self):
        invalid = [
            None,
            [],
            [{'dst': 'default'}],
            [{'dst': 'default', 'gateway': '8.8.8.8'}],
            [{'dst': 'default', 'gateway': '127.0.0.1'}],
            [{'dst': 'default', 'gateway': '0.0.0.0'}],
            [{'dst': 'default', 'gateway': '172.22.0.1\nHTTPS_PROXY=bad'}],
            [{'dst': 'default', 'gateway': '172.22.0.1', 'metric': True}],
            [
                {'dst': 'default', 'gateway': '172.22.0.1'},
                {'dst': 'default', 'gateway': '172.23.0.1'},
            ],
        ]
        for routes in invalid:
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                proxy.select_gateway(routes)

    def test_all_clients_share_endpoint_and_local_bypass(self):
        env = dict(line.split('=', 1) for line in proxy.render_environment('172.22.0.1').splitlines())
        self.assertEqual(list(env), list(proxy.MANAGED_KEYS))
        self.assertEqual(len(env), 8)
        self.assertEqual(env['https_proxy'], env['HTTPS_PROXY'])
        self.assertEqual(env['http_proxy'], env['https_proxy'])
        self.assertEqual(env['all_proxy'], 'socks5h://172.22.0.1:17890')
        self.assertEqual(env['NO_PROXY'], 'localhost,127.0.0.1,::1')
        self.assertNotIn('GIT_SSL_NO_VERIFY', env)

    def test_managed_block_preserves_unrelated_runner_env_and_updates_gateway(self):
        original = 'LANG=C.UTF-8\nJAVA_HOME=/opt/java\n'
        first = proxy.reconcile_environment(original, proxy.render_environment('172.22.0.1'))
        self.assertTrue(first.startswith(original))
        self.assertEqual(first.count(proxy.MANAGED_BEGIN), 1)
        self.assertEqual(first.count(proxy.MANAGED_END), 1)
        second = proxy.reconcile_environment(first, proxy.render_environment('172.23.0.1'))
        self.assertIn('172.23.0.1', second)
        self.assertNotIn('172.22.0.1', second)
        self.assertTrue(second.startswith(original))
        removed = proxy.reconcile_environment(second, None)
        self.assertEqual(removed, original)

    def test_unmanaged_proxy_or_malformed_owned_block_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'RUNNER_PROXY_EXISTING_ENV_CONFLICT'):
            proxy.reconcile_environment('LANG=C\nhttps_proxy=http://existing\n', proxy.render_environment('172.22.0.1'))
        with self.assertRaisesRegex(ValueError, 'RUNNER_PROXY_MANAGED_BLOCK_INVALID'):
            proxy.reconcile_environment(proxy.MANAGED_BEGIN + '\nhttp_proxy=x\n', proxy.render_environment('172.22.0.1'))
        malformed = (
            proxy.MANAGED_BEGIN + '\n'
            + 'http_proxy=x\n'
            + proxy.MANAGED_END + '\n'
        )
        with self.assertRaisesRegex(ValueError, 'RUNNER_PROXY_MANAGED_BLOCK_INVALID'):
            proxy.reconcile_environment(malformed, None)

    def test_atomic_runner_env_replacement_preserves_owner_mode_and_unrelated_lines(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'runner'
            root.mkdir(mode=0o700)
            target = root / '.env'
            target.write_text('LANG=C.UTF-8\n', encoding='utf-8')
            target.chmod(0o600)
            before = target.stat()
            proxy.update_runner_environment(target, proxy.render_environment('172.22.0.1'))
            after = target.stat()
            self.assertEqual(after.st_uid, before.st_uid)
            self.assertEqual(after.st_gid, before.st_gid)
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            value = target.read_text(encoding='utf-8')
            self.assertTrue(value.startswith('LANG=C.UTF-8\n'))
            self.assertEqual(value.count(proxy.MANAGED_BEGIN), 1)
            proxy.update_runner_environment(target, None)
            self.assertEqual(target.read_text(encoding='utf-8'), 'LANG=C.UTF-8\n')

    def test_symlink_or_writable_runner_env_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'runner'
            root.mkdir(mode=0o700)
            real = root / 'real'
            real.write_text('LANG=C\n')
            link = root / '.env'
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                proxy.update_runner_environment(link, proxy.render_environment('172.22.0.1'))
            link.unlink()
            link.write_text('LANG=C\n')
            link.chmod(0o666)
            with self.assertRaises(ValueError):
                proxy.update_runner_environment(link, proxy.render_environment('172.22.0.1'))

    def test_dropin_uses_runner_supported_env_not_systemd_environmentfile(self):
        dropin = (HOST / 'mobile-proxy-phone-runner-proxy.conf').read_text()
        self.assertIn('ExecStartPre=+/usr/bin/python3 -I -B /usr/local/lib/mobile-proxy-runner-proxy/prepare-runner-proxy-environment.py --apply', dropin)
        self.assertNotIn('EnvironmentFile=', dropin)
        self.assertNotIn('Requires=', dropin)
        self.assertNotIn('mobile-proxy-runner-proxy-prepare.service', dropin)
        self.assertFalse((HOST / 'mobile-proxy-runner-proxy-prepare.service').exists())

    def test_installation_has_exact_rollback_and_no_restart_or_network_mutation(self):
        installer = (HOST / 'install-runner-proxy.sh').read_text()
        helper = (HOST / 'prepare-runner-proxy-environment.py').read_text()
        dropin = (HOST / 'mobile-proxy-phone-runner-proxy.conf').read_text()
        combined = installer + helper + dropin
        for forbidden in (
            'systemctl restart', 'systemctl stop', 'systemctl start', 'adb ', 'usbipd',
            'git config', 'config.sh', 'iptables', 'netsh',
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn('RUNNER_PROXY_LEGACY_PREPARE_UNIT_PRESENT', installer)
        self.assertIn('cmp -s "$SOURCE_DROPIN" "$DROPIN"', installer)
        self.assertIn('"${LIB_DIR}/${HELPER}" --remove', installer)
        self.assertIn('runner_restart_performed=false', installer)


if __name__ == '__main__':
    unittest.main()
