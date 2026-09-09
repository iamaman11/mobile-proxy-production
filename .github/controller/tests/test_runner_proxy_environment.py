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

    def test_runner_env_is_derived_from_service_working_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            self.assertEqual(proxy.runner_environment_path(root), root / '.env')
            linked = root.parent / (root.name + '-link')
            linked.symlink_to(root, target_is_directory=True)
            try:
                with self.assertRaisesRegex(ValueError, 'RUNNER_PROXY_WORKING_DIRECTORY_UNSAFE'):
                    proxy.runner_environment_path(linked)
            finally:
                linked.unlink()

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

    def test_check_is_read_only_and_validates_same_existing_file_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'runner'
            root.mkdir(mode=0o700)
            target = root / '.env'
            target.write_text('LANG=C.UTF-8\n', encoding='utf-8')
            target.chmod(0o600)
            before = target.read_bytes()
            proxy.check_runner_environment(target, proxy.render_environment('172.22.0.1'))
            self.assertEqual(target.read_bytes(), before)
            target.write_text('https_proxy=http://unmanaged\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'RUNNER_PROXY_EXISTING_ENV_CONFLICT'):
                proxy.check_runner_environment(target, proxy.render_environment('172.22.0.1'))

    def test_absent_runner_env_check_is_read_only_and_apply_creates_atomically(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'runner'
            root.mkdir(mode=0o700)
            target = root / '.env'
            parent = root.stat()
            managed = proxy.render_environment('172.22.0.1')

            self.assertFalse(target.exists())
            proxy.check_runner_environment(target, managed)
            self.assertFalse(target.exists())

            proxy.update_runner_environment(target, managed)
            self.assertTrue(target.is_file())
            info = target.stat()
            self.assertEqual(info.st_uid, parent.st_uid)
            self.assertEqual(info.st_gid, parent.st_gid)
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            value = target.read_text(encoding='utf-8')
            self.assertEqual(value.count(proxy.MANAGED_BEGIN), 1)
            self.assertEqual(value.count(proxy.MANAGED_END), 1)
            for key in proxy.MANAGED_KEYS:
                self.assertIn(f'{key}=', value)

            proxy.update_runner_environment(target, None)
            self.assertEqual(target.read_bytes(), b'')

    def test_absent_runner_env_under_writable_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'runner'
            root.mkdir(mode=0o700)
            root.chmod(0o777)
            target = root / '.env'
            with self.assertRaisesRegex(ValueError, 'RUNNER_PROXY_RUNNER_DIRECTORY_UNSAFE'):
                proxy.check_runner_environment(target, proxy.render_environment('172.22.0.1'))

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

    def test_installation_validates_then_applies_before_activation_without_restart(self):
        installer = (HOST / 'install-runner-proxy.sh').read_text()
        helper = (HOST / 'prepare-runner-proxy-environment.py').read_text()
        dropin = (HOST / 'mobile-proxy-phone-runner-proxy.conf').read_text()
        combined = installer + helper + dropin
        for forbidden in (
            'systemctl restart', 'systemctl stop', 'systemctl start', 'adb ', 'usbipd',
            'git config', 'config.sh', 'iptables', 'netsh',
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn('systemctl show --property=WorkingDirectory --value', installer)
        self.assertIn('RUNNER_PROXY_WORKING_DIRECTORY_INVALID', installer)
        self.assertIn('"${SOURCE_DIR}/${HELPER}" --check', installer)
        self.assertIn('RUNNER_PROXY_PREINSTALL_CHECK_FAILED', installer)
        self.assertIn('"${LIB_DIR}/${HELPER}" --apply', installer)
        self.assertIn('RUNNER_PROXY_ENV_APPLY_FAILED', installer)
        self.assertIn('RUNNER_PROXY_PARTIAL_GENERATION_PRESENT', installer)
        self.assertIn('rollback_fresh_install', installer)
        self.assertIn('RUNNER_PROXY_LEGACY_PREPARE_UNIT_PRESENT', installer)
        self.assertIn('cmp -s "$SOURCE_DROPIN" "$DROPIN"', installer)
        self.assertIn('"${LIB_DIR}/${HELPER}" --remove', installer)
        self.assertIn('runner_proxy_env=applied', installer)
        self.assertIn('runner_restart_performed=false', installer)
        self.assertLess(
            installer.index('"${SOURCE_DIR}/${HELPER}" --check'),
            installer.index('"${LIB_DIR}/${HELPER}" --apply'),
        )
        self.assertLess(
            installer.index('"${LIB_DIR}/${HELPER}" --apply'),
            installer.index('if ! systemctl daemon-reload; then'),
        )
        self.assertNotIn('/opt/mobile-proxy-production-runner', combined)


if __name__ == '__main__':
    unittest.main()
