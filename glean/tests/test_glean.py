# Copyright (c) 2015 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import functools
import json
import os
try:
    from unittest import mock
except ImportError:
    import mock # noqa H216

from oslotest import base
from testscenarios import load_tests_apply_scenarios as load_tests  # noqa

from glean._vendor import distro as vendor_distro  # noqa
from glean import cmd


sample_data_path = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'fixtures')

distros = ['Ubuntu', 'Debian', 'Fedora', 'RedHat', 'CentOS', 'CentOS-10',
           'Gentoo', 'openSUSE', 'networkd', 'Rocky']
styles = ['hp', 'rax', 'rax-iad', 'liberty', 'nokey', 'vexxhost']

ips = {'hp': '127.0.1.1',
       'rax': '23.253.229.154',
       'rax-iad': '146.20.110.113',
       'liberty': '23.253.229.154',
       'nokey': '127.0.1.1',
       'vexxhost': '2604:e100:1:0:f816:3eff:fed1:1fb2'}

interfaces = {
    'hp': 'eth0',
    'rax': 'eth0',
    'rax-iad': 'eth0',
    'liberty': 'eth0',
    'nokey': 'eth0',
    'vexxhost': 'ens3',
}

built_scenarios = []
for distro in distros:
    for style in styles:
        built_scenarios.append(
            ('%s-%s' % (distro, style),
             dict(distro=distro, style=style)))

# save these for wrapping
real_path_exists = os.path.exists
real_listdir = os.listdir


class TestGlean(base.BaseTestCase):

    scenarios = built_scenarios

    def setUp(self):
        super(TestGlean, self).setUp()
        self._resolv_unlinked = False
        self.file_handle_mocks = {}

    def open_side_effect(self, sample_prefix, *args, **kwargs):
        # incoming file
        path = args[0]

        # we redirect some files to files in here
        sample_path = os.path.join(sample_data_path, sample_prefix)

        # Test broken symlink handling -- we want the code to unlink
        # this file and write a new one.  Simulate a /etc/resolv.conf
        # that at first returns ELOOP (i.e. broken symlink) when
        # opened, but on the second call opens as usual.  We check
        # that there was an os.unlink() performed
        if path.startswith('/etc/resolv.conf'):
            if not self._resolv_unlinked:
                self._resolv_unlinked = True
                raise IOError(errno.ELOOP,
                              os.strerror(errno.ELOOP), path)

        # mock any files in these paths as blank files.  Keep track of
        # them in file_handle_mocks{} so we can assert they were
        # called later
        mock_dirs = ('/etc/network', '/etc/sysconfig/network-scripts',
                     '/etc/conf.d', '/etc/init.d', '/etc/sysconfig/network',
                     '/etc/systemd/network',
                     '/etc/NetworkManager/system-connections')
        mock_files = ('/etc/resolv.conf', '/etc/hostname', '/etc/hosts',
                      '/etc/systemd/resolved.conf', '/bin/systemctl')
        if (path.startswith(mock_dirs) or path in mock_files):
            try:
                mock_handle = self.file_handle_mocks[path]
            except KeyError:
                # note; don't use spec=file here ... it's not py3
                # compatible.  It really just limits the allowed
                # mocked functions.
                mock_handle = mock.MagicMock()
                mock_handle.__enter__ = mock.Mock()
                mock_handle.__exit__ = mock.Mock()
                mock_handle.name = path
                # This is a trick to handle open used as a context
                # manager (i.e. with open('foo') as f).  It's the
                # returned object that gets called, so we point it
                # back at the underlying mock (see mock.mock_open())
                mock_handle.__enter__.return_value = mock_handle
                mock_handle.read.return_value = ''
                self.file_handle_mocks[path] = mock_handle
            return mock_handle

        # redirect these files to our samples
        elif path.startswith(('/sys/class/net',
                              '/mnt/config')):
            new_args = list(args)
            new_args[0] = os.path.join(sample_path, path[1:])
            return open(*new_args, **kwargs)

        # otherwise just pass it through
        else:
            return open(*args, **kwargs)

    def os_listdir_side_effect(self, sample_prefix, path):
        if path.startswith('/'):
            path = path[1:]
        return real_listdir(os.path.join(
            sample_data_path, sample_prefix, path))

    def os_path_exists_side_effect(self, sample_prefix, path):
        if path.startswith('/mnt/config'):
            path = os.path.join(sample_data_path, sample_prefix, path[1:])
        if path in ('/etc/sysconfig/network-scripts/ifcfg-eth2',
                    '/etc/network/interfaces.d/eth2.cfg',
                    '/etc/conf.d/net.eth2',
                    '/etc/sysconfig/network/ifcfg-eth2',
                    '/etc/systemd/network/eth2.network'):
            # Pretend this file exists, we need to test skipping
            # pre-existing config files
            return True
        elif (path.startswith(('/etc/sysconfig/network-scripts/',
                               '/etc/sysconfig/network/',
                               '/etc/network/interfaces.d/',
                               '/etc/conf.d/',
                               '/etc/systemd/network/'))):
            # Don't check the host os's network config
            return False
        return real_path_exists(path)

    def os_system_side_effect(self, distro, command):
        if distro.lower() != 'networkd' and \
                command == 'systemctl is-enabled systemd-resolved':
            return 3
        else:
            return 0

    @mock.patch('subprocess.call', return_value=0, new_callable=mock.Mock)
    @mock.patch('os.fsync', return_value=0, new_callable=mock.Mock)
    @mock.patch('os.unlink', return_value=0, new_callable=mock.Mock)
    @mock.patch('os.symlink', return_value=0, new_callable=mock.Mock)
    @mock.patch('os.path.exists', new_callable=mock.Mock)
    @mock.patch('os.listdir', new_callable=mock.Mock)
    @mock.patch('os.system', return_value=0, new_callable=mock.Mock)
    @mock.patch('glean.cmd.open', new_callable=mock.Mock)
    @mock.patch('glean._vendor.distro.linux_distribution',
                new_callable=mock.Mock)
    @mock.patch('glean.cmd.is_keyfile_format', return_value=False,
                new_callable=mock.Mock)
    def _assert_distro_provider(self, distro, provider, interface,
                                mock_is_keyfile,
                                mock_vendor_linux_distribution,
                                mock_open,
                                mock_os_system,
                                mock_os_listdir,
                                mock_os_path_exists,
                                mock_os_symlink,
                                mock_os_unlink,
                                mock_os_fsync,
                                mock_call,
                                skip_dns=False,
                                use_nm=False):
        """Main test function

        :param distro: distro to return from "distro.linux_distribution()"
        :param provider: we will look in fixtures/provider for mocked
                         out files
        :param interface: --interface argument; None for no argument
        :param skip_dns: --skip-dns argument; False for no argument
        :param use_nm: --use-nm argument; False for no argument
        """

        # These functions are watching the path and faking results
        # based on various things
        # XXX : There are several virtual file-systems available, we
        # might like to look into them and just point ourselves at
        # testing file-systems in the future if this becomes more
        # complex.
        mock_os_path_exists.side_effect = functools.partial(
            self.os_path_exists_side_effect, provider)
        mock_os_listdir.side_effect = functools.partial(
            self.os_listdir_side_effect, provider)
        mock_open.side_effect = functools.partial(
            self.open_side_effect, provider)
        # we want os.system to return False for specific commands if
        # running networkd
        mock_os_system.side_effect = functools.partial(
            self.os_system_side_effect, distro)

        # distro versions handling
        if distro.__contains__("-"):
            version = distro.rsplit("-", 1)[1]
            distro = distro.rsplit("-", 1)[0]
            mock_vendor_linux_distribution.return_value = [distro, version]
            if distro in ['CentOS', 'Rocky', 'RHEL'] and version == "10":
                mock_is_keyfile.return_value = True
                use_nm = True
        else:
            version = None
            mock_vendor_linux_distribution.return_value = [distro]

        # default args
        argv = ['--hostname']

        argv.append('--distro=%s' % distro.lower())

        if interface:
            argv.append('--interface=%s' % interface)
        if skip_dns:
            argv.append('--skip-dns')
        if use_nm:
            argv.append('--use-nm')

        cmd.main(argv)

        if version:
            output_filename = '%s.%s-%s.network.out' % (provider,
                                                        distro.lower(),
                                                        version)
        else:
            output_filename = '%s.%s.network.out' % (provider, distro.lower())

        output_path = os.path.join(sample_data_path, 'test', output_filename)
        if not skip_dns:
            if os.path.exists(output_path + '.dns'):
                output_path = output_path + '.dns'

        # Generate a list of (dest, content) into write_blocks to assert
        write_blocks = []
        lines = []
        with open(output_path) as f:
            for line in f:
                if line == '%NM_CONTROLLED%\n':
                    line = 'NM_CONTROLLED=%s\n' % ("yes" if use_nm else "no")
                lines.append(line)
        write_dest = None
        write_content = None
        for line in lines:
            if line.startswith('### Write '):
                if write_dest is not None:
                    write_blocks.append((write_dest, write_content))
                write_dest = line[len('### Write '):-1]
                write_content = ''
            else:
                write_content += line
        if write_dest is not None:
            write_blocks.append((write_dest, write_content))

        for dest, content in write_blocks:
            if interface and interface not in dest:
                continue
            self.assertNotIn("eth2", dest)
            # Skip check
            if skip_dns and '/etc/resolv.conf' in dest:
                self.assertNotIn(dest, self.file_handle_mocks)
                continue
            if skip_dns and '/etc/systemd/resolved.conf' in dest:
                self.assertNotIn(dest, self.file_handle_mocks)
                continue
            self.assertIn(dest, self.file_handle_mocks)
            write_handle = self.file_handle_mocks[dest].write
            write_handle.assert_called_once_with(content)

        if self._resolv_unlinked:
            mock_os_unlink.assert_called_once_with('/etc/resolv.conf')

        # Check hostname
        meta_data_path = 'mnt/config/openstack/latest/meta_data.json'
        hostname = None
        with open(os.path.join(sample_data_path, provider,
                               meta_data_path)) as fh:
            meta_data = json.load(fh)
            hostname = meta_data['name']

        mock_call.assert_has_calls([mock.call(['hostname', hostname])])
        if distro.lower() == 'gentoo':
            (self.file_handle_mocks['/etc/conf.d/hostname'].write.
                assert_has_calls([mock.call('hostname="%s"\n' % hostname)]))
        else:
            self.file_handle_mocks['/etc/hostname'].write.assert_has_calls(
                [mock.call(hostname), mock.call('\n')])

        # Check hosts entry
        hostname_ip = ips[provider]
        calls = [mock.call('%s %s\n' % (hostname_ip, hostname)), ]
        short_hostname = hostname.split('.')[0]
        if hostname != short_hostname:
            calls.append(mock.call('%s %s\n' % (hostname_ip, short_hostname)))

        self.file_handle_mocks['/etc/hosts'].write.assert_has_calls(
            calls, any_order=True)

    def test_glean(self):
        with mock.patch('glean.systemlock.Lock'):
            self._assert_distro_provider(self.distro, self.style, None)

    # In the systemd case, we are a templated unit file
    # (glean@.service) so we get called once for each interface that
    # comes up with "--interface".  This simulates that.
    def test_glean_systemd(self):
        with mock.patch('glean.systemlock.Lock'):
            self._assert_distro_provider(self.distro, self.style,
                                         interfaces[self.style], skip_dns=True)

    def test_glean_systemd_resolved(self):
        with mock.patch('glean.systemlock.Lock'):
            self._assert_distro_provider(self.distro, self.style,
                                         interfaces[self.style])

    def test_glean_skip_dns(self):
        with mock.patch('glean.systemlock.Lock'):
            self._assert_distro_provider(
                self.distro, self.style, None, skip_dns=True)

    def test_glean_nm(self):
        with mock.patch('glean.systemlock.Lock'):
            self._assert_distro_provider(
                self.distro, self.style, interfaces[self.style], use_nm=True)
