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
        routes = [{'dst':'default','gateway':'172.22.0.1','metric':200}, {'dst':'default','gateway':'172.25.0.1','metric':100}]
        self.assertEqual(proxy.select_gateway(routes),'172.25.0.1')
        routes[1]['gateway']='172.26.0.1'
        self.assertEqual(proxy.select_gateway(routes),'172.26.0.1')

    def test_missing_ambiguous_or_injectable_route_fails_closed(self):
        invalid = [None, [], [{'dst':'default'}], [{'dst':'default','gateway':'8.8.8.8'}], [{'dst':'default','gateway':'127.0.0.1'}], [{'dst':'default','gateway':'0.0.0.0'}], [{'dst':'default','gateway':'172.22.0.1\nHTTPS_PROXY=bad'}], [{'dst':'default','gateway':'172.22.0.1','metric':True}], [{'dst':'default','gateway':'172.22.0.1'}, {'dst':'default','gateway':'172.23.0.1'}]]
        for routes in invalid:
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                proxy.select_gateway(routes)

    def test_all_clients_share_endpoint_and_local_bypass(self):
        env=dict(line.split('=',1) for line in proxy.render_environment('172.22.0.1').splitlines())
        self.assertEqual(len(env),8)
        self.assertEqual(env['https_proxy'],env['HTTPS_PROXY'])
        self.assertEqual(env['http_proxy'],env['https_proxy'])
        self.assertEqual(env['all_proxy'],'socks5h://172.22.0.1:17890')
        self.assertEqual(env['NO_PROXY'],'localhost,127.0.0.1,::1')
        self.assertNotIn('GIT_SSL_NO_VERIFY',env)

    def test_atomic_private_replacement_and_gateway_change(self):
        with tempfile.TemporaryDirectory() as raw:
            directory=Path(raw)/'private'
            proxy.write_environment(directory,proxy.render_environment('172.22.0.1'))
            proxy.write_environment(directory,proxy.render_environment('172.23.0.1'))
            target=directory/'environment'
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
            self.assertIn('172.23.0.1',target.read_text())
            self.assertNotIn('172.22.0.1',target.read_text())
            self.assertEqual([p.name for p in directory.iterdir()],['environment'])

    def test_symlink_or_public_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            base=Path(raw); private=base/'private'; private.mkdir(mode=0o700)
            other=base/'other'; other.write_text('keep')
            (private/'environment').symlink_to(other)
            with self.assertRaises(ValueError): proxy.write_environment(private,'bad')
            self.assertEqual(other.read_text(),'keep')
            link=base/'link'; link.symlink_to(private,target_is_directory=True)
            with self.assertRaises(ValueError): proxy.write_environment(link,'bad')
            public=base/'public'; public.mkdir(mode=0o755)
            with self.assertRaises(ValueError): proxy.write_environment(public,'bad')

    def test_preparation_is_a_required_prior_unit_not_same_activation_execstartpre(self):
        dropin=(HOST/'mobile-proxy-phone-runner-proxy.conf').read_text()
        prepare=(HOST/'mobile-proxy-runner-proxy-prepare.service').read_text()
        self.assertIn('Requires=mobile-proxy-runner-proxy-prepare.service',dropin)
        self.assertIn('After=mobile-proxy-runner-proxy-prepare.service',dropin)
        self.assertIn('EnvironmentFile=/run/mobile-proxy-runner-proxy/environment',dropin)
        self.assertNotIn('EnvironmentFile=-',dropin)
        self.assertNotIn('ExecStartPre=',dropin)
        self.assertNotIn('ExecStart=',dropin)
        self.assertIn('Before=mobile-proxy-phone-runner.service',prepare)
        self.assertIn('Type=oneshot',prepare)
        self.assertIn('User=root',prepare)
        self.assertIn('Group=root',prepare)
        self.assertIn('UMask=0077',prepare)
        self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/lib/mobile-proxy-runner-proxy/prepare-runner-proxy-environment.py',prepare)
        self.assertNotIn('RemainAfterExit=',prepare)

    def test_installation_does_not_restart_or_change_git_phone_network(self):
        installer=(HOST/'install-runner-proxy.sh').read_text()
        helper=(HOST/'prepare-runner-proxy-environment.py').read_text()
        dropin=(HOST/'mobile-proxy-phone-runner-proxy.conf').read_text()
        prepare=(HOST/'mobile-proxy-runner-proxy-prepare.service').read_text()
        combined=installer+helper+dropin+prepare
        for forbidden in ('systemctl restart','systemctl stop','systemctl start','adb ','usbipd','git config','config.sh','iptables','netsh'):
            self.assertNotIn(forbidden,combined)
        self.assertIn('cmp -s "$SOURCE_DROPIN" "$DROPIN"',installer)
        self.assertIn('cmp -s "$SOURCE_PREPARE_UNIT" "$INSTALLED_PREPARE_UNIT"',installer)
        self.assertIn('install -o root -g root -m 0644 "$SOURCE_PREPARE_UNIT" "$INSTALLED_PREPARE_UNIT"',installer)
        self.assertIn('Path("/etc/systemd/system/mobile-proxy-runner-proxy-prepare.service").unlink(missing_ok=True)',installer)


if __name__=='__main__': unittest.main()
