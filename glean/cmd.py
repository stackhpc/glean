#!/usr/bin/python
# Copyright (c) 2015 Monty Taylor
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

import argparse
import contextlib
import copy
import errno
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time

from collections import defaultdict
from glean._vendor import distro
from glean import install
from glean import systemlock
from glean import utils

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

log = logging.getLogger("glean")


# Type value for permanent mac addrs as defined by the linux kernel.
PERMANENT_ADDR_TYPE = '0'

# Global flag for selinux restore.
SELINUX_RESTORECON = '/usr/sbin/restorecon'
HAVE_SELINUX = os.path.exists(SELINUX_RESTORECON)


# Wrap open calls in this to make sure that any created or modified
# files retain their selinux context.
@contextlib.contextmanager
def safe_open(*args, **kwargs):
    f = open(*args, **kwargs)
    yield f
    f.flush()
    os.fsync(f.fileno())
    f.close()
    path = os.path.abspath(f.name)
    if HAVE_SELINUX:
        logging.debug("Restoring selinux context for %s" % path)
        subprocess.call([SELINUX_RESTORECON, path])


def _exists_rh_interface(name, distro):
    file_to_check = _network_files(distro)['ifcfg'] + '-{name}'.format(
        name=name)
    return os.path.exists(file_to_check)


def _exists_rh_keyfile_interface(name):
    file_to_check = \
        '/etc/NetworkManager/system-connections/{name}.nmconnection'.format(
            name=name)
    return os.path.exists(file_to_check)


# Since RHEL10/CentOS 10 keyfiles format configs are madatory
def is_keyfile_format():
    if "el10" in distro.os_release_info().get("platform_id", ""):
        keyfiles = True
    else:
        keyfiles = False
    return keyfiles


def _is_suse(distro):
    # 'distro could be any of suse, opensuse,
    # opensuse-leap, opensuse-tumbleweed, sles
    return 'suse' in distro or 'sles' in distro


def _network_files(distro):
    network_files = {}
    if _is_suse(distro):
        network_files = {
            "ifcfg": "/etc/sysconfig/network/ifcfg",
            "route": "/etc/sysconfig/network/ifroute",
            "route6": "/etc/sysconfig/network/ifroute6",
        }
    else:
        network_files = {
            "ifcfg": "/etc/sysconfig/network-scripts/ifcfg",
            "route": "/etc/sysconfig/network-scripts/route",
            "route6": "/etc/sysconfig/network-scripts/route6",
        }

    return network_files


def _network_config(args):
    distro = args.distro

    if _is_suse(distro):
        preamble = textwrap.dedent("""\
        # Automatically generated, do not edit
        BOOTPROTO={bootproto}
        LLADDR={hwaddr}
        STARTMODE=auto
        """)
    elif is_keyfile_format():
        preamble = textwrap.dedent("""\
        # Automatically generated, do not edit
        [connection]
        id={id}
        uuid=
        type=802-3-ethernet

        [ipv4]
        method={method}

        [802-3-ethernet]
        mac_address={mac_address}
        """)
    else:
        preamble = textwrap.dedent("""\
        # Automatically generated, do not edit
        DEVICE={name}
        BOOTPROTO={bootproto}
        HWADDR={hwaddr}
        ONBOOT=yes
        NM_CONTROLLED=%s
        TYPE=Ethernet
        """ % ("yes" if args.use_nm else "no"))

    return preamble


def _set_rh_bonding(name, interface, distro, results):
    if not any(bond in ['bond_slaves', 'bond_master'] for bond in interface):
        return results

    # Careful, we are operating on the live 'results' variable
    # so we need to always append our data
    if _is_suse(distro):
        # SUSE configures the slave interfaces on the master ifcfg file.
        # The master interface contains a 'bond_slaves' key containing a list
        # of the slave interfaces
        if 'bond_slaves' in interface:
            results += "BONDING_MASTER=yes\n"
            slave_cnt = 0
            for slave in interface['bond_slaves']:
                results += "BONDING_SLAVE_{id}={name}\n".format(
                    id=slave_cnt, name=slave)
                slave_cnt += 1
        else:
            # Slave interfaces do not know they are part of a bonded
            # interface. All we need to do is to set the STARTMODE
            # to hotplug
            results = results.replace("=auto", "=hotplug")

    elif is_keyfile_format():
        if 'bond_slaves' in interface:
            results += "type=bond\n"

        else:
            results += "master={0}\n".format(interface['bond_master'])
            results += "slave-type=bond\n"

    else:
        # RedHat does not add any specific configuration to the master
        # interface. All configuration is done in the slave ifcfg files.
        if 'bond_slaves' in interface:
            return results

        results += "SLAVE=yes\n"
        results += "MASTER={0}\n".format(interface['bond_master'])

    return results


def _set_rh_vlan(name, interface, distro):
    results = ""

    if 'vlan_id' not in interface:
        return results

    if _is_suse(distro):
        results += "VLAN_ID={vlan_id}\n".format(vlan_id=interface['vlan_id'])
        results += "ETHERDEVICE={etherdevice}\n".format(
            etherdevice=name.split('.')[0])
    elif is_keyfile_format():
        results += "\n[vlan]\n"
        results += "id={vlan_id}\n".format(vlan_id=interface['vlan_id'])
        results += "parent={parent}\n".format(parent=interface['vlan_link'])
    else:
        results += "VLAN=yes\n"

    return results


def _write_rh_v6_interface(name, interface, args, files):
    config_file = _network_files(args.distro)["ifcfg"] + '-%s' % name
    # We should already have this config file key key, we need to
    # append to it
    assert (config_file in files)
    config_data = files[config_file]
    config_data += 'IPV6INIT=yes\n'
    config_data += 'IPV6_PRIVACY=no\n'
    if interface['type'] == 'ipv6_slaac':
        config_data += 'IPV6_AUTOCONF=yes\n'
    else:
        config_data += 'IPV6_AUTOCONF=no\n'
        config_data += 'IPV6ADDR=%s/%d\n' % (
            interface['ip_address'],
            utils.ipv6_netmask_length(interface['netmask']))

    routes = ''
    for route in interface.get('routes', ()):
        netmask = utils.ipv6_netmask_length(route['netmask'])
        if route['network'] == '::':
            # The default gateway should be specified in the
            # ifcfg-<interface> file.  Putting default routes in
            # routes<6>-<interface> definitely doesn't work with
            # centos7.
            config_data += 'IPV6_DEFAULTGW={0}{1}\n'.format(
                route['gateway'],
                ("/%d" % netmask) if netmask else '')
        else:
            # Otherwise use "policy routing" mode.  The IPv4 mode is
            # called "Netmask Directives" (ADDRESS0/GATEWAY0/NETMASK0)
            # and it's not clear these support ipv6.
            netmask = utils.ipv6_netmask_length(route['netmask'])
            routes += '{0}{1} via {2}\n'.format(
                route['network'],
                ("/%d" % netmask) if netmask else '',
                route['gateway']
            )
    # Only write the routes file if we have extra routes
    route6_file = _network_files(args.distro)['route6'] + '-%s' % name
    if routes:
        files[route6_file] = routes

    files[config_file] = config_data
    return files


def _write_rh_v6_keyfile_interface(name, interface, args, files):
    filename = \
        '/etc/NetworkManager/system-connections/{name}.nmconnection'.format(
            name=name)
    if interface['type'] == 'ipv6_slaac' and not interface['ip_address']:
        results = ""
        results += "[ipv6]\n"
        results += "method=auto\n"
    else:
        results = ""
        results += "[ipv6]\n"
        results += "method=manual\n"
        try:
            netmask = utils.ipv6_netmask_length(interface['netmask'])
        except Exception:
            logging.error("Invalid netmask format %s." % interface['netmask'])
        results += "address1={address}/{netmask}\n".format(
            address=interface['ip_address'], netmask=netmask)

    if interface.get('routes', ()):
        routes = []
        route_counter = 0
        for route in interface.get('routes', ()):
            route_counter += 1
            ipv6_netmask = utils.ipv6_netmask_length(route['netmask'])
            full_route = "route" + str(route_counter) + "=" \
                + route['network'] + "/" + str(ipv6_netmask) \
                + ',' + route['gateway']

            routes.append(full_route)
        if routes:
            route_match = re.search(r"address.*/[0-9][0-9]\n", results)
            if not route_match:
                route_match = re.search(r"method=*\n", results)
            str_routes = ""
            for i in range(len(routes)):
                str_routes += routes[i] + "\n"
            results = results[:route_match.end()] + str_routes + \
                results[route_match.end():]

    # append ipv6 config at the end
    if results:
        files[filename] = files[filename] + "\n" + results

    return files


def _write_rh_interface(name, interface, args):
    distro = args.distro
    files_to_write = dict()
    results = _network_config(args).format(
        bootproto="static",
        name=name,
        hwaddr=interface['mac_address'],
    )

    results += 'IPADDR=%s\n' % interface['ip_address']
    results += 'NETMASK=%s\n' % interface['netmask']

    results += _set_rh_vlan(name, interface, distro)
    # set_rh_bonding takes results as argument so we need to assign
    # the return value, not append it
    results = _set_rh_bonding(name, interface, distro, results)
    routes = []
    for route in interface.get('routes', ()):
        if route['network'] == '0.0.0.0' and route['netmask'] == '0.0.0.0':
            if not _is_suse(distro):
                results += "DEFROUTE=yes\n"
                results += "GATEWAY={gw}\n".format(gw=route['gateway'])
            else:
                # Special notation for default route on SUSE/wicked
                routes.append(dict(
                    net='default', mask='', gw=route['gateway']))
        else:
            routes.append(dict(
                net=route['network'], mask=route['netmask'],
                gw=route['gateway']))

    if routes:
        route_content = ""
        for x in range(0, len(routes)):
            if not _is_suse(distro):
                route_content += "ADDRESS{x}={net}\n".format(x=x, **routes[x])
                route_content += "NETMASK{x}={mask}\n".format(x=x, **routes[x])
                route_content += "GATEWAY{x}={gw}\n".format(x=x, **routes[x])
            else:
                # Avoid the extra trailing whitespace for the default route
                # because mask is empty in that case.
                route_content += "{net} {gw} {mask}\n".format(
                    **routes[x]).replace(' \n', '\n')
        files_to_write[_network_files(distro)["route"] + '-{name}'
                       .format(name=name)] = route_content
    files_to_write[_network_files(distro)["ifcfg"] + '-{name}'.format(
        name=name)] = results

    return files_to_write


def _write_rh_dhcp(name, interface, args):
    distro = args.distro
    filename = _network_files(distro)["ifcfg"] + '-{name}'.format(name=name)
    results = _network_config(args).format(
        bootproto="dhcp", name=name, hwaddr=interface['mac_address'])
    results += _set_rh_vlan(name, interface, distro)
    # set_rh_bonding takes results as argument so we need to assign
    # the return value, not append it
    results = _set_rh_bonding(name, interface, distro, results)

    return {filename: results}


def _write_rh_keyfile_dhcp(name, interface, args):
    distro = args.distro
    filename = \
        '/etc/NetworkManager/system-connections/{name}.nmconnection'.format(
            name=name)
    results = _network_config(args).format(
        method="auto", id=name, mac_address=interface['mac_address'])
    results += _set_rh_vlan(name, interface, distro)
    # set_rh_bonding takes results as argument so we need to assign
    # the return value, not append it
    results = _set_rh_bonding(name, interface, distro, results)

    return {filename: results}


def _write_rh_manual(name, interface, args):
    distro = args.distro
    if is_keyfile_format():
        filename = \
            '/etc/NetworkManager/system-connections/{name}.nmconnection' \
            .format(name=name)
        results = _network_config(args).format(
            method="manual", id=name, mac_address=interface['mac_address'])
    else:
        filename = _network_files(distro)["ifcfg"] + '-{name}'.format(
            name=name)
        results = _network_config(args).format(
            bootproto="none", name=name, hwaddr=interface['mac_address'])
    results += _set_rh_vlan(name, interface, distro)
    # set_rh_bonding takes results as argument so we need to assign
    # the return value, not append it
    results = _set_rh_bonding(name, interface, distro, results)

    return {filename: results}


def _write_rh_keyfile_interface(_name, interface, args):
    # NOTE(mnasiadka): Importing here to maintain py2.7 compatibility
    import ipaddress

    distro = args.distro
    files_to_write = dict()
    filename = \
        '/etc/NetworkManager/system-connections/{name}.nmconnection'.format(
            name=_name)
    results = _network_config(args).format(
        method="manual", id=_name, mac_address=interface['mac_address'])

    # insert value after specific option using slicing
    address = interface['ip_address'] + "/" + interface['netmask']
    cidr_address = 'address1=%s' % ipaddress.ip_interface(
        address).with_prefixlen
    match = re.search(r'manual\n', results)
    results = results[:match.end()] + cidr_address + \
        "\n" + results[match.end():]
    results += _set_rh_vlan(_name, interface, distro)

    results = _set_rh_bonding(_name, interface, distro, results)

    # format:comma separated list of routes in [ipv4]
    routes = []
    route_counter = 0
    for route in interface.get('routes', ()):
        route_counter += 1
        route_cidr_address = ipaddress.ip_interface(
            route['network'] + "/" + route['netmask']).with_prefixlen
        full_route = "route" + str(route_counter) + "=" + \
            route_cidr_address + ',' + route['gateway']
        routes.append(full_route)

    if routes:
        route_match = re.search(r"address.*/[0-9][0-9]\n", results)
        str_routes = ""
        for i in range(len(routes)):
            str_routes += routes[i] + "\n"
        results = results[:route_match.end()] + \
            str_routes + results[route_match.end():]

    files_to_write[filename] = results
    return files_to_write


def write_redhat_interfaces(interfaces, sys_interfaces, args):

    files_to_write = dict()
    # Strip out ignored interfaces
    _interfaces = {}
    for iname, interface in interfaces.items():
        # sys_interfaces is pruned by --interface; if one of the
        # raw_macs (or, *the* MAC for single interfaces) does not
        # match as one of the interfaces we want configured, skip
        raw_macs = interface.get('raw_macs', [interface['mac_address']])
        if not set(sys_interfaces).intersection(set(raw_macs)):
            continue

        if 'bond_links' in interface:
            # We need to keep track of the slave interfaces because
            # SUSE configures the slaves on the master ifcfg file
            bond_slaves = []
            for phy in interface['raw_macs']:
                bond_slaves.append(sys_interfaces[phy])
            interface['bond_slaves'] = bond_slaves
            # Remove the 'bond_links' key
            interface.pop('bond_links')

        _interfaces[iname] = interface
    interfaces = _interfaces
    # Sort the interfaces by id so that we'll have consistent output order
    _interfaces_by_sys_name = defaultdict(list)
    for iname, interface in sorted(
            interfaces.items(), key=lambda x: x[1]['id']):

        if 'vlan_id' in interface:
            # raw_macs will have a single entry if the vlan device is a
            # phsical device and >1 when it is a bond device.
            raw_macs = interface.get('raw_macs', [interface['mac_address']])
            if len(raw_macs) == 1:
                vlan_raw_device = sys_interfaces.get(raw_macs[0])
            else:
                vlan_raw_device = interface['vlan_link']
            interface_name = "{0}.{1}".format(
                vlan_raw_device, interface['vlan_id'])

        elif 'bond_mode' in interface:
            # It is possible our interface does not have a link, so fall back
            # to iname which is the link id.
            interface_name = interface.get('link', iname)
        else:
            interface_name = sys_interfaces[interface['mac_address']]

        # This is a dict that lists the networks associated with a
        # device; so like:
        #  _interfaces_by_sys_name['eth0'] = [
        #     <ipv4_interface>, <ipv6_interface>
        #  ]
        _interfaces_by_sys_name[interface_name].append(interface)

    for _name, _interfaces in _interfaces_by_sys_name.items():

        # Sort the interfaces by type.  ipv4 should come before ipv6.
        # We need to process ipv4 first; it builds the base config
        # file (because when written we only dealt with ipv4).
        # _write_rh_v6_interface() then adds ipv6 info, which is why
        # it gets passed "files_to_write".
        #
        # NOTE(ianw) : 2022-06-01 It would definitely be better to
        # refactor this in a way that does more like a generic
        # template that each possible interface type fills in, rather
        # than relying on ordering like this.  The term "interfaces"
        # is a bit of a misnomer, and it's really a network being
        # setup on what we'd think of as an interface (eth<X>, etc.).
        # This could also be cleaned up to be clearer.
        for interface in sorted(_interfaces, key=lambda x: x['type']):
            logging.debug("Processing interface %s/%s" %
                          (_name, interface['type']))
            if interface['type'] == 'ipv4':
                if is_keyfile_format():
                    files_to_write.update(
                        _write_rh_keyfile_interface(_name, interface, args))
                else:
                    files_to_write.update(
                        _write_rh_interface(_name, interface, args))
            elif interface['type'] == 'ipv4_dhcp':
                if is_keyfile_format():
                    files_to_write.update(
                        _write_rh_keyfile_dhcp(_name, interface, args))
                else:
                    files_to_write.update(
                        _write_rh_dhcp(_name, interface, args))
            elif interface['type'] == 'manual':
                files_to_write.update(
                    _write_rh_manual(_name, interface, args))
            elif interface['type'] in ('ipv6', 'ipv6_slaac'):
                if is_keyfile_format():
                    files_to_write.update(
                        _write_rh_v6_keyfile_interface(_name,
                                                       interface, args,
                                                       files_to_write))
                else:
                    files_to_write.update(
                        _write_rh_v6_interface(_name,
                                               interface, args,
                                               files_to_write))
            else:
                logging.error(
                    "Unhandled interface %s/%s" % (_name, interface['type']))

    if args.no_dhcp_fallback:
        log.debug('DHCP fallback disabled')
        return files_to_write

    # Strip out anything that has now been configured, the rest falls
    # back to simple DHCP
    for mac, iname in sorted(
            sys_interfaces.items(), key=lambda x: x[1]):
        if is_keyfile_format:
            if _exists_rh_keyfile_interface(iname):
                log.debug("%s already has config file, skipping" % iname)
                continue
        else:
            if _exists_rh_interface(iname, args.distro):
                # This interface already has a config file, move on
                log.debug("%s already has config file, skipping" % iname)
                continue
        inter_macs = [intf['mac_address'] for intf in interfaces.values()]
        link_macs = [intf.get('link_mac') for intf in interfaces.values()
                     if 'vlan_id' in intf]
        if mac in inter_macs or mac in link_macs:
            # We have a config drive config, move on
            log.debug("%s configured via config-drive" % mac)
            continue
        if is_keyfile_format():
            files_to_write.update(
                _write_rh_keyfile_dhcp(iname, {'mac_address': mac}, args))
        else:
            files_to_write.update(
                _write_rh_dhcp(iname, {'mac_address': mac}, args))

    return files_to_write


def _write_networkd_interface(name, interfaces, args, files_struct=dict()):
    vlans = []
    for interface in interfaces:
        iname = name
        # if vlan set interface name to vlan format
        if 'vlan_id' in interface:
            iname = name + '-vlan' + str(interface['vlan_id'])
            vlans.append(iname)
        network_file = '/etc/systemd/network/{name}.network'.format(name=iname)
        if network_file not in files_struct:
            files_struct[network_file] = dict()
        if '[Match]' not in files_struct[network_file]:
            files_struct[network_file]['[Match]'] = list()
        files_struct[network_file]['[Match]'].append(
            'MACAddress={mac_address}'.format(
                mac_address=interface['mac_address']
            )
        )
        files_struct[network_file]['[Match]'].append(
            'Name={name}'.format(name=iname)
        )
        # define network if needed (basically always)
        if ((interface['type'] in ['ipv4_dhcp', 'ipv6_slaac',
                                   'ipv6_dhcpv6_stateful', 'manual',
                                   'ipv4', 'ipv6'])
                or ('vlan_id' in interface)
                or ('bond_mode' in interface)):
            if '[Network]' not in files_struct[network_file]:
                files_struct[network_file]['[Network]'] = list()
            if 'services' in interface:
                for service in interface['services']:
                    if service['type'] == 'dns':
                        if not args.skip_dns:
                            files_struct[network_file]['[Network]'].append(
                                'DNS={address}'.format(
                                    address=service['address']
                                )
                            )
        # dhcp network, set to yes if both dhcp6 and dhcp4 are set
        if interface['type'] == 'ipv4_dhcp':
            if 'DHCP=ipv6' in files_struct[network_file]['[Network]']:
                files_struct[network_file]['[Network]'].append('DHCP=yes')
            else:
                files_struct[network_file]['[Network]'].append('DHCP=ipv4')
        if interface['type'] == 'ipv6_dhcpv6_stateful':
            if 'DHCP=ipv4' in files_struct[network_file]['[Network]']:
                files_struct[network_file]['[Network]'].append('DHCP=yes')
            else:
                files_struct[network_file]['[Network]'].append('DHCP=ipv6')
        # slaac can start dhcp6 if the associated RA option is sent to server
        if (interface['type'] == 'ipv6_slaac'
                or interface['type'] == 'ipv6_dhcpv6-stateless'):
            # we are accepting slaac now, remove the disabling of slaac
            if 'IPv6AcceptRA=no' in files_struct[network_file]['[Network]']:
                files_struct[network_file]['[Network]'].remove(
                    'IPv6AcceptRA=no'
                )
            files_struct[network_file]['[Network]'].append('IPv6AcceptRA=yes')
        else:
            # only disbale slaac if slac is not already enabled
            if 'IPv6AcceptRA=yes' not in \
                    files_struct[network_file]['[Network]']:
                files_struct[network_file]['[Network]'].append(
                    'IPv6AcceptRA=no'
                )
        # vlan network

        # static network
        if interface['type'] in ['ipv4', 'ipv6']:
            if 'addresses' not in files_struct[network_file]:
                files_struct[network_file]['addresses'] = list()
        if interface['type'] == 'ipv4':
            files_struct[network_file]['addresses'].append(
                'Address={address}/{cidr}'.format(
                    address=interface['ip_address'],
                    cidr=utils.ipv4_netmask_length(interface['netmask'])
                )
            )
        if interface['type'] == 'ipv6':
            files_struct[network_file]['addresses'].append(
                'Address={address}/{cidr}'.format(
                    address=interface['ip_address'],
                    cidr=utils.ipv6_netmask_length(interface['netmask'])
                )
            )
        # routes
        if 'routes' in interface:
            if 'routes' not in files_struct[network_file]:
                files_struct[network_file]['routes'] = list()
            for route in interface['routes']:
                route_destination = None
                route_gateway = None
                if 'network' in route:
                    if 'v6' in interface['type']:
                        cidr = utils.ipv6_netmask_length(route['netmask'])
                    else:
                        cidr = utils.ipv4_netmask_length(route['netmask'])
                    route_destination = 'Destination={network}/{cidr}'.format(
                        network=route['network'], cidr=cidr
                    )
                if 'gateway' in route:
                    route_gateway = 'Gateway={gateway}'.format(
                        gateway=route['gateway']
                    )
                # add route as a dictionary to the routes list
                files_struct[network_file]['routes'].append({
                    'route': route_destination,
                    'gw': route_gateway
                })

        # create netdev files
        if 'bond_mode' or 'vlan_id' in interface:
            netdev_file = \
                '/etc/systemd/network/{name}.netdev'.format(name=iname)
            if netdev_file not in files_struct:
                files_struct[netdev_file] = dict()
            if '[NetDev]' not in files_struct[netdev_file]:
                files_struct[netdev_file]['[NetDev]'] = list()
            files_struct[netdev_file]['[NetDev]'].append(
                'Name={name}'.format(name=iname)
            )
            if 'mac_address' in interface:
                files_struct[netdev_file]['[NetDev]'].append(
                    'MACAddress={mac_address}'.format(
                        mac_address=interface['mac_address']
                    )
                )
            if 'vlan_id' in interface:
                files_struct[netdev_file]['[NetDev]'].append('Kind=vlan')
                files_struct[netdev_file]['[VLAN]'] = list()
                files_struct[netdev_file]['[VLAN]'].append(
                    'Id={id}'.format(id=interface['vlan_id'])
                )
            if 'bond_mode' in interface:
                files_struct[netdev_file]['[NetDev]'].append('Kind=bond')
                files_struct[netdev_file]['[Bond]'] = list()
                files_struct[netdev_file]['[Bond]'].append(
                    'Mode={bond_mode}'.format(bond_mode=interface['bond_mode'])
                )
                files_struct[netdev_file]['[Bond]'].append(
                    'LACPTransmitRate=fast'
                )
                if 'slaves' in interface:
                    for slave in interface['slaves']:
                        slave_net_file = \
                            '/etc/systemd/network/{name}.network'.format(
                                name=slave
                            )
                        if slave_net_file not in files_struct:
                            files_struct[slave_net_file] = dict()
                        if '[Network]' not in files_struct[slave_net_file]:
                            files_struct[slave_net_file]['[Network]'] = list()
                        files_struct[slave_net_file]['[Network]'].append(
                            'Bond={name}'.format(name=iname)
                        )
                if 'bond_xmit_hash_policy' in interface:
                    files_struct[netdev_file]['[Bond]'].append(
                        'TransmitHashPolicy={bond_xmit_hash_policy}'.format(
                            bond_xmit_hash_policy=interface[
                                'bond_xmit_hash_policy'
                            ]
                        )
                    )
                if 'bond_miimon' in interface:
                    files_struct[netdev_file]['[Bond]'].append(
                        'MIIMonitorSec={milliseconds}'.format(
                            milliseconds=interface['bond_miimon']
                        )
                    )

    # vlan mapping sucks (forward and reverse)
    if vlans:
        netdev = vlans[0].split('-')[0]
        vlan_master_file = \
            '/etc/systemd/network/{name}.network'.format(name=netdev)
        if vlan_master_file not in files_struct:
            files_struct[vlan_master_file] = dict()
        if '[Network]' not in files_struct[vlan_master_file]:
            files_struct[vlan_master_file]['[Network]'] = list()
        for vlan in vlans:
            files_struct[vlan_master_file]['[Network]'].append('VLAN=' + vlan)
            vlan_file = '/etc/systemd/network/{name}.network'.format(name=vlan)
            if vlan_file not in files_struct:
                files_struct[vlan_file] = dict()
            if '[Network]' not in files_struct[vlan_file]:
                files_struct[vlan_file]['[Network]'] = list()
            files_struct[vlan_file]['[Network]'].append('VLAN=' + vlan)

    return files_struct


def write_networkd_interfaces(interfaces, sys_interfaces, args):
    files_to_write = dict()
    gen_intfs = {}
    files_struct = dict()
    # Sort the interfaces by id so that we'll have consistent output order
    for iname, interface in sorted(
            interfaces.items(), key=lambda x: x[1]['id']):
        # sys_interfaces is pruned by --interface; if one of the
        # raw_macs (or, *the* MAC for single interfaces) does not
        # match as one of the interfaces we want configured, skip
        raw_macs = interface.get('raw_macs', [interface['mac_address']])
        if not set(sys_interfaces).intersection(set(raw_macs)):
            continue

        if 'bond_mode' in interface:
            interface['slaves'] = [
                sys_interfaces[mac] for mac in interface['raw_macs']]

        if 'raw_macs' in interface:
            key = tuple(interface['raw_macs'])
            if key not in gen_intfs:
                gen_intfs[key] = []
            gen_intfs[key].append(interface)
        else:
            key = (interface['mac_address'],)
            if key not in gen_intfs:
                gen_intfs[key] = []
            gen_intfs[key].append(interface)

    for raw_macs, interfs in gen_intfs.items():
        if len(raw_macs) == 1:
            interface_name = sys_interfaces[raw_macs[0]]
        else:
            # It is possible our interface does not have a link, so
            # fall back to interface id.
            interface_name = next(
                intf.get('link', intf['id']) for intf in interfs
                if 'bond_mode' in intf)
        files_struct = _write_networkd_interface(
            interface_name, interfs, args, files_struct)

    for mac, iname in sorted(
            sys_interfaces.items(), key=lambda x: x[1]):
        if _exists_networkd_interface(iname):
            # This interface already has a config file, move on
            log.debug("%s already has config file, skipping" % iname)
            continue
        if (mac,) in gen_intfs:
            # We have a config drive config, move on
            log.debug("%s configured via config-drive" % mac)
            continue
        if args.no_dhcp_fallback:
            log.debug('DHCP fallback disabled')
            continue
        interface = {'type': 'ipv4_dhcp', 'mac_address': mac}
        files_struct = _write_networkd_interface(
            iname, [interface], args, files_struct)

    for networkd_file in files_struct:
        file_contents = '# Automatically generated, do not edit\n'
        if '[Match]' in files_struct[networkd_file]:
            file_contents += '[Match]\n'
            for line in sorted(set(files_struct[networkd_file]['[Match]'])):
                file_contents += line
                file_contents += '\n'
            file_contents += '\n'
        if '[Network]' in files_struct[networkd_file]:
            file_contents += '[Network]\n'
            for line in sorted(set(files_struct[networkd_file]['[Network]'])):
                file_contents += line
                file_contents += '\n'
            file_contents += '\n'
        if 'addresses' in files_struct[networkd_file]:
            for address in files_struct[networkd_file]['addresses']:
                file_contents += '[Address]\n'
                file_contents += address + '\n\n'
        if 'routes' in files_struct[networkd_file]:
            for route in files_struct[networkd_file]['routes']:
                file_contents += '[Route]\n'
                if route['route'] is not None:
                    file_contents += route['route'] + '\n'
                if route['gw'] is not None:
                    file_contents += route['gw'] + '\n'
                file_contents += '\n'
        if '[NetDev]' in files_struct[networkd_file]:
            file_contents += '[NetDev]\n'
            for line in sorted(set(files_struct[networkd_file]['[NetDev]'])):
                file_contents += line
                file_contents += '\n'
            file_contents += '\n'
        if '[VLAN]' in files_struct[networkd_file]:
            file_contents += '[VLAN]\n'
            for line in sorted(set(files_struct[networkd_file]['[VLAN]'])):
                file_contents += line
                file_contents += '\n'
            file_contents += '\n'
        if '[Bond]' in files_struct[networkd_file]:
            file_contents += '[Bond]\n'
            for line in sorted(set(files_struct[networkd_file]['[Bond]'])):
                file_contents += line
                file_contents += '\n'
            file_contents += '\n'
        files_to_write['{path}'.format(path=networkd_file)] = file_contents
    return files_to_write


def _exists_networkd_interface(name):
    network_file = '/etc/systemd/network/{name}.network'.format(name=name)
    netdev_file = '/etc/systemd/network/{name}.netdev'.format(name=name)
    return (os.path.exists(network_file) or os.path.exists(netdev_file))


def _exists_gentoo_interface(name):
    file_to_check = '/etc/conf.d/net.{name}'.format(name=name)
    return os.path.exists(file_to_check)


def _enable_gentoo_interface(name):
    log.debug('rc-update add {name} default'.format(name=name))
    subprocess.call(['rc-update', 'add',
                     'net.{name}'.format(name=name), 'default'])


def _write_gentoo_interface(name, interfaces):
    files_to_write = dict()
    results = ""
    vlans = []
    for interface in interfaces:
        iname = name
        if 'vlan_id' in interface:
            vlans.append(interface['vlan_id'])
            iname = "%s_%s" % (iname, interface['vlan_id'])
        if interface['type'] == 'ipv4':
            results += """config_{name}="{ip_address} netmask {netmask}"
mac_{name}="{hwaddr}\"\n""".format(
                name=iname,
                ip_address=interface['ip_address'],
                netmask=interface['netmask'],
                hwaddr=interface['mac_address']
            )
            routes = list()
            for route in interface.get('routes', ()):
                if (route['network'] == '0.0.0.0'
                        and route['netmask'] == '0.0.0.0'):
                    # add default route if it exists
                    routes.append('default via {gw}'.format(
                        gw=route['gateway']
                    ))
                else:
                    # add remaining static routes
                    routes.append('{net} netmask {mask} via {gw}'.format(
                        net=route['network'],
                        mask=route['netmask'],
                        gw=route['gateway']
                    ))
            if routes:
                routes_string = '\n'.join(route for route in routes)
                results += 'routes_{name}="{routes}"'.format(
                    name=name,
                    routes=routes_string
                    # routes='\n'.join(str(route) for route in routes)
                )
                results += '\n'
        elif interface['type'] == 'manual':
            results += """config_{name}="null"
mac_{name}="{hwaddr}"
""".format(name=iname, hwaddr=interface['mac_address'])
            _enable_gentoo_interface(iname)
        else:
            results += """config_{name}="dhcp"
mac_{name}="{hwaddr}"
""".format(name=iname, hwaddr=interface['mac_address'])
            _enable_gentoo_interface(iname)
        if 'bond_mode' in interface:
            slaves = ' '.join(interface['slaves'])
            results += """slaves_{name}="{slaves}"
mode_{name}="{mode}"
""".format(name=iname, slaves=slaves, mode=interface['bond_mode'])

    full_results = "# Automatically generated, do not edit\n"
    if vlans:
        full_results += 'vlans_{name}="{vlans}"\n'.format(
            name=name,
            vlans=' '.join(str(vlan) for vlan in vlans))
    full_results += results

    files_to_write['/etc/conf.d/net.{name}'.format(name=name)] = full_results
    return files_to_write


def _setup_gentoo_network_init(sys_interface, interfaces):
    for interface in interfaces:
        interface_name = '{name}'.format(name=sys_interface)
        if 'vlan_id' in interface:
            interface_name += ".{vlan}".format(
                vlan=interface['vlan_id'])
            log.debug('vlan {vlan} found, interface named {name}'.
                      format(vlan=interface['vlan_id'], name=interface_name))
        if 'bond_master' in interface:
            continue
        _create_gentoo_net_symlink_and_enable(interface_name)
    if not interfaces:
        _create_gentoo_net_symlink_and_enable(sys_interface)


def _create_gentoo_net_symlink_and_enable(interface_name):
    file_path = '/etc/init.d/net.{name}'.format(name=interface_name)
    if not os.path.islink(file_path):
        log.debug('ln -s /etc/init.d/net.lo {file_path}'.
                  format(file_path=file_path))
        os.symlink('/etc/init.d/net.lo',
                   '{file_path}'.format(file_path=file_path))
        _enable_gentoo_interface(interface_name)


def write_gentoo_interfaces(interfaces, sys_interfaces, args):
    files_to_write = dict()
    gen_intfs = {}
    # Sort the interfaces by id so that we'll have consistent output order
    for iname, interface in sorted(
            interfaces.items(), key=lambda x: x[1]['id']):
        if interface['type'] == 'ipv6':
            continue
        # sys_interfaces is pruned by --interface; if one of the
        # raw_macs (or, *the* MAC for single interfaces) does not
        # match as one of the interfaces we want configured, skip
        raw_macs = interface.get('raw_macs', [interface['mac_address']])
        if not set(sys_interfaces).intersection(set(raw_macs)):
            continue

        if 'bond_mode' in interface:
            interface['slaves'] = [
                sys_interfaces[mac] for mac in interface['raw_macs']]

        if 'raw_macs' in interface:
            key = tuple(interface['raw_macs'])
            if key not in gen_intfs:
                gen_intfs[key] = []
            gen_intfs[key].append(interface)
        else:
            key = (interface['mac_address'],)
            if key not in gen_intfs:
                gen_intfs[key] = []
            gen_intfs[key].append(interface)

    for raw_macs, interfs in gen_intfs.items():
        if len(raw_macs) == 1:
            interface_name = sys_interfaces[raw_macs[0]]
        else:
            # It is possible our interface does not have a link, so
            # fall back to interface id.
            interface_name = next(
                intf.get('link', intf['id']) for intf in interfs
                if 'bond_mode' in intf)
        files_to_write.update(
            _write_gentoo_interface(interface_name, interfs))
        _setup_gentoo_network_init(interface_name, interfs)

    if args.no_dhcp_fallback:
        log.debug('DHCP fallback disabled')
        return files_to_write

    for mac, iname in sorted(
            sys_interfaces.items(), key=lambda x: x[1]):
        if _exists_gentoo_interface(iname):
            # This interface already has a config file, move on
            log.debug("%s already has config file, skipping" % iname)
            continue
        if (mac,) in gen_intfs:
            # We have a config drive config, move on
            log.debug("%s configured via config-drive" % mac)
            continue
        interface = {'type': 'ipv4_dhcp', 'mac_address': mac}
        files_to_write.update(_write_gentoo_interface(iname, [interface]))
        _setup_gentoo_network_init(iname, [])
    return files_to_write


def _write_debian_bond_conf(interface_name, interface, sys_interfaces):
    result = ""
    if interface['mac_address']:
        result += "    hwaddress {0}\n".format(
            interface['mac_address'])
    result += "    bond-mode {0}\n".format(interface['bond_mode'])
    result += "    bond-miimon {0}\n".format(
        interface.get('bond_miimon', 0))
    result += "    bond-lacp-rate {0}\n".format(
        interface.get('bond_lacp_rate', 'slow'))
    result += "    bond-xmit_hash_policy {0}\n".format(
        interface.get('bond_xmit_hash_policy', 'layer2'))
    slave_devices = [sys_interfaces[mac]
                     for mac in interface['raw_macs']]
    slaves = ' '.join(slave_devices)
    result += "    bond-slaves none\n"
    result += "    post-up ifenslave {0} {1}\n".format(interface_name, slaves)
    result += "    pre-down ifenslave -d {0} {1}\n".format(
        interface_name, slaves)
    return result


def write_debian_interfaces(interfaces, sys_interfaces, args):
    eni_path = '/etc/network/interfaces'
    eni_d_path = eni_path + '.d'
    files_to_write = dict()
    files_to_write[eni_path] = "auto lo\niface lo inet loopback\n"
    files_to_write[eni_path] += "source /etc/network/interfaces.d/*.cfg\n"
    # Sort the interfaces by id so that we'll have consistent output order
    for iname, interface in sorted(
            interfaces.items(), key=lambda x: x[1]['id']):
        # sys_interfaces is pruned by --interface; if one of the
        # raw_macs (or, *the* MAC for single interfaces) does not
        # match as one of the interfaces we want configured, skip
        raw_macs = interface.get('raw_macs', [interface['mac_address']])
        if not set(sys_interfaces).intersection(set(raw_macs)):
            continue

        # Determine the debian interface name and skip configuration for
        # this interface if config already exists for it.
        vlan_raw_device = None
        if 'vlan_id' in interface:
            # raw_macs will have a single entry if the vlan device is a
            # phsical device and >1 when it is a bond device.
            if len(raw_macs) == 1:
                vlan_raw_device = sys_interfaces.get(raw_macs[0])
            else:
                vlan_raw_device = interface['vlan_link']
            interface_name = "{0}.{1}".format(vlan_raw_device,
                                              interface['vlan_id'])
        elif 'bond_mode' in interface:
            # It is possible our interface does not have a link, so fall back
            # to iname which is the link id.
            interface_name = interface.get('link', iname)
        else:
            interface_name = sys_interfaces[interface['mac_address']]

        iface_path = os.path.join(eni_d_path, '%s.cfg' % interface_name)
        if os.path.exists(iface_path):
            continue

        if iface_path not in files_to_write:
            # Write the header if this is the first time editing
            # this interface.
            result = "auto {0}\n".format(interface_name)
        else:
            # Append to existing config
            result = files_to_write[iface_path]

        if interface['type'] == 'ipv4_dhcp':
            result += "iface {0} inet dhcp\n".format(interface_name)
            if vlan_raw_device is not None:
                result += "    vlan-raw-device {0}\n".format(vlan_raw_device)
                result += "    hw-mac-address {0}\n".format(
                    interface['mac_address'])
        elif interface['type'] == 'manual':
            result += "iface {0} inet manual\n".format(interface_name)
        else:
            # Static ipv4 and ipv6
            if interface['type'] == 'ipv6':
                link_type = "inet6"
            elif interface['type'] == 'ipv4':
                link_type = "inet"
            else:
                # We do not know this type of entry
                continue

            result += "iface {name} {link_type} static\n".format(
                name=interface_name, link_type=link_type)
            if vlan_raw_device:
                result += "    vlan-raw-device {0}\n".format(vlan_raw_device)
            result += "    address {0}\n".format(interface['ip_address'])

            if interface['type'] == 'ipv4':
                result += "    netmask {0}\n".format(interface['netmask'])
            else:
                result += "    netmask {0}\n".format(
                    utils.ipv6_netmask_length(interface['netmask']))

            for route in interface.get('routes', ()):
                if ((route['network'] == '0.0.0.0'
                     and route['netmask'] == '0.0.0.0')
                    or (route['network'] == '::'
                        and route['netmask'] == '::')):
                    result += "    gateway {0}\n".format(route['gateway'])
                else:
                    if interface['type'] == 'ipv4':
                        route_add = ("    up route add -net {net} netmask "
                                     "{mask} gw {gw} || true\n")
                        route_del = ("    down route del -net {net} netmask "
                                     "{mask} gw {gw} || true\n")
                        _netmask = route['netmask']
                    else:
                        route_add = ("    up ip -6 route add {net}/{mask} "
                                     "via {gw} dev {interface} || true\n")
                        route_del = ("    down ip -6 route del {net}/{mask} "
                                     "via {gw} dev {interface} || true\n")
                        _netmask = utils.ipv6_netmask_length(route['netmask'])

                    result += route_add.format(
                        net=route['network'], mask=_netmask,
                        gw=route['gateway'], interface=interface_name)
                    result += route_del.format(
                        net=route['network'], mask=_netmask,
                        gw=route['gateway'], interface=interface_name)
        if 'bond_master' in interface:
            result += "    bond-master {0}\n".format(
                interface['bond_master'])
        if 'bond_mode' in interface:
            result += _write_debian_bond_conf(interface_name,
                                              interface,
                                              sys_interfaces)
        files_to_write[iface_path] = result

    if args.no_dhcp_fallback:
        log.debug('DHCP fallback disabled')
        return files_to_write

    # Configure any interfaces not mentioned in the config drive data for DHCP.
    for mac, iname in sorted(
            sys_interfaces.items(), key=lambda x: x[1]):
        iface_path = os.path.join(eni_d_path, '%s.cfg' % iname)
        if os.path.exists(iface_path):
            # This interface already has a config file, move on
            continue
        inter_macs = [intf['mac_address'] for intf in interfaces.values()]
        link_macs = [intf.get('link_mac') for intf in interfaces.values()
                     if 'vlan_id' in intf]
        if mac in inter_macs or mac in link_macs:
            # We have a config drive config, move on
            continue
        result = "auto {0}\n".format(iname)
        result += "iface {0} inet dhcp\n".format(iname)
        files_to_write[iface_path] = result
    return files_to_write


def write_dns_info(dns_servers, args):
    # exit early if there are no DNS servers
    if not dns_servers:
        return {}
    # will fail on non-systemd systems (what we want)
    # will exit 1 if not enabled (what we want)
    # will exit 0 if enabled (or indirectly enabled)
    resolved_enabled = os.system('systemctl is-enabled systemd-resolved')
    resolve_confs = {}
    # write resolv.conf if the file can be written to (if symlink is pointing
    # to a non-existant file, writing will fail), will return false if the
    # pointer is incomplete
    if resolved_enabled != 0:
        log.debug("resolved not in use, writing to /etc/resolv.conf")
        resolv_nameservers = ""
        for server in dns_servers:
            resolv_nameservers += "nameserver {0}\n".format(server)
        resolve_confs['/etc/resolv.conf'] = resolv_nameservers
    # set up resolved if enabled
    if resolved_enabled == 0:
        log.debug("resolved in use, writing to /etc/systemd/resolved.conf")
        # read the existing config  so we only overwrite what's needed
        resolved_conf = configparser.ConfigParser()
        resolved_conf.optionxform = str
        resolved_conf.read('/etc/systemd/resolved.conf')
        # create config section if not created
        if not resolved_conf.has_section('Resolve'):
            resolved_conf.add_section('Resolve')
        # write space separated dns servers
        resolved_conf.set('Resolve', 'DNS', " ".join(dns_servers))
        # use stringio to output the resulting config to string
        # configparser only outputs to file descriptors
        resolved_conf_fd = StringIO("")
        resolved_conf.write(resolved_conf_fd)
        resolved_conf_output = resolved_conf_fd.getvalue()
        resolved_conf_fd.close()
        # add the config to files to be written
        resolve_confs['/etc/systemd/resolved.conf'] = resolved_conf_output
    # make sure NetworkManager does not override our configuration
    if args.use_nm:
        install.install(
            'nm-no-resolv-handling.conf',
            '/etc/NetworkManager/conf.d/nm-no-resolv-handling.conf',
            mode='0644')
    return resolve_confs


def get_config_drive_interfaces(net):
    interfaces = {}

    if 'networks' not in net or 'links' not in net:
        log.debug("No config-drive interfaces defined")
        return interfaces

    networks = {}
    for network in net['networks']:
        networks[network['link']] = network

    vlans = {}
    phys = {}
    bonds = {}
    for link in net['links']:
        if link['type'] == 'vlan':
            vlans[link['id']] = link
        elif link['type'] == 'bond':
            bonds[link['id']] = link
        else:
            phys[link['id']] = link

    for link in vlans.values():
        if link['vlan_link'] in phys:
            vlan_link = phys[link['vlan_link']]
            link['raw_macs'] = [vlan_link['ethernet_mac_address'].lower()]
        elif link['vlan_link'] in bonds:
            vlan_link = bonds[link['vlan_link']]
            link['raw_macs'] = []
            for phy in vlan_link['bond_links']:
                link['raw_macs'].append(
                    phys[phy]['ethernet_mac_address'].lower())
        else:
            log.warning('vlan_link=%s not matching any '
                        'NIC', link['vlan_link'])
            continue

        link['mac_address'] = link.pop(
            'vlan_mac_address', vlan_link['ethernet_mac_address']).lower()

    for link in bonds.values():
        phy_macs = []
        for phy in link['bond_links']:
            phy_link = phys[phy]
            phy_link['bond_master'] = link['id']
            if phy in phys:
                phy_macs.append(phy_link['ethernet_mac_address'].lower())
        link['raw_macs'] = phy_macs
        link['mac_address'] = link.pop('ethernet_mac_address').lower()
        if link['id'] not in networks:
            link['type'] = 'manual'
            interfaces[link['id']] = link

    for link in phys.values():
        link['mac_address'] = link.pop('ethernet_mac_address').lower()
        if link['id'] not in networks:
            link['type'] = 'manual'
            interfaces[link['id']] = link

    for network in net['networks']:
        link = vlans.get(
            network['link'],
            phys.get(network['link'], bonds.get(network['link'])))
        if not link:
            continue

        link.update(network)
        # NOTE(pabelanger): Make sure we index by the existing network id,
        # rather then creating out own.
        interfaces[network['id']] = copy.deepcopy(link)

    return interfaces


def get_dns_from_config_drive(net):
    if 'services' not in net:
        log.debug("No DNS info available from config-drive")
        return []
    return [
        f['address'] for f in net['services'] if f['type'] == 'dns'
    ]


def write_static_network_info(
        interfaces, sys_interfaces, files_to_write, args):

    if args.distro in ('debian', 'ubuntu'):
        files_to_write.update(
            write_debian_interfaces(interfaces, sys_interfaces, args))
    elif args.distro in ('redhat', 'centos', 'fedora', 'rocky') or \
            _is_suse(args.distro):
        files_to_write.update(
            write_redhat_interfaces(interfaces, sys_interfaces, args))
    elif args.distro in 'gentoo':
        files_to_write.update(
            write_gentoo_interfaces(interfaces, sys_interfaces, args)
        )
    elif args.distro in 'networkd':
        files_to_write.update(
            write_networkd_interfaces(interfaces, sys_interfaces, args)
        )
    else:
        return False

    finish_files(files_to_write, args)


def finish_files(files_to_write, args):
    files = sorted(files_to_write.keys())
    log.debug("Writing output files")
    for k in files:
        if not files_to_write[k]:
            # Don't write empty files
            log.debug("%s is blank, skipped" % k)
            continue

        if args.noop:
            sys.stdout.write("### Write {0}\n{1}".format(k, files_to_write[k]))
            continue

        retries = 0
        while True:
            try:
                log.debug("Writing output file : %s" % k)
                with safe_open(k, 'w') as outfile:
                    outfile.write(files_to_write[k])
                log.debug(" ... done")
                break
            except IOError as e:
                # if we got ELOOP the file was a dangling or bad
                # symlink.  We're taking ownership of this, so
                # overwrite it.
                if e.errno == errno.ELOOP and retries < 1:
                    log.debug("Dangling symlink <%s>; "
                              "unlinking and trying again" % k)
                    os.unlink(k)
                    retries = 1
                    continue
                elif e.errno == errno.EACCES:
                    log.debug(" ... is read only, skipped")
                    break
                else:
                    raise


def is_interface_live(interface, sys_root):
    try:
        if open('{root}/{iface}/carrier'.format(
                root=sys_root, iface=interface)).read().strip() == '1':
            return True
    except IOError as e:
        # We get this error if the link is not up
        if e.errno != 22:
            raise
    return False


def interface_live(iface, sys_root, args):
    log.debug("Checking status of interface %s" % iface)
    if is_interface_live(iface, sys_root):
        log.debug("%s has active carrier, including", iface)
        return True
    else:
        log.debug("%s does not have active carrier", iface)

    if args.noop:
        return False

    subprocess.check_call(['ip', 'link', 'set', 'dev', iface, 'up'])

    return True


def is_interface_vlan(iface, distro):
    if distro in ('debian', 'ubuntu'):
        file_name = '/etc/network/interfaces.d/%s.cfg' % iface
        if os.path.exists(file_name):
            return 'vlan-raw-device' in open(file_name).read()
    elif distro in ('redhat', 'centos', 'fedora', 'rocky'):
        file_name = '/etc/sysconfig/network-scripts/ifcfg-%s' % iface
        if os.path.exists(file_name):
            return 'VLAN=YES' in open(file_name).read()
    elif _is_suse(distro):
        file_name = '/etc/sysconfig/network/ifcfg-%s' % iface
        if os.path.exists(file_name):
            return 'ETHERDEVICE' in open(file_name).read()
    elif distro in ('gentoo'):
        file_name = '/etc/conf.d/net.%s' % iface
        if os.path.exists(file_name):
            return 'vlan_id' in open(file_name).read()

    return False


def is_interface_bridge(iface, distro):
    if distro in ('debian', 'ubuntu'):
        file_name = '/etc/network/interfaces.d/%s.cfg' % iface
        if os.path.exists(file_name):
            return 'bridge_ports' in open(file_name).read().lower()
    elif distro in ('redhat', 'centos', 'fedora', 'rocky'):
        file_name = '/etc/sysconfig/network-scripts/ifcfg-%s' % iface
        if os.path.exists(file_name):
            return 'type=bridge' in open(file_name).read().lower()
    elif _is_suse(distro):
        file_name = '/etc/sysconfig/network/ifcfg-%s' % iface
        if os.path.exists(file_name):
            return 'bridge=yes' in open(file_name).read().lower()
    elif distro in ('gentoo'):
        file_name = '/etc/conf.d/net.%s' % iface
        if os.path.exists(file_name):
            return 'bridge' in open(file_name).read().lower()

    return False


def get_sys_interfaces(interface, args):
    log.debug("Probing system interfaces")
    sys_root = os.path.join(args.root, 'sys/class/net')

    ignored_interfaces = ('sit', 'tunl', 'bonding_master', 'teql', 'wg',
                          'ip6gre', 'ip6_vti', 'ip6tnl', 'bond', 'lo')
    sys_interfaces = {}

    called_from_udev = False
    if interface is not None:
        log.debug("Only considering interface %s from arguments" % interface)
        interfaces = [interface]
        # see notes below...
        called_from_udev = True
    else:
        interfaces = [f for f in os.listdir(sys_root)
                      if not f.startswith(ignored_interfaces)]
    # build interface dict. so we can enumerate through later
    if_dict = {}
    for iface in interfaces:
        # if interface is for an already configured vlan, skip it
        if is_interface_vlan(iface, args.distro):
            log.debug("Skipping vlan %s" % iface)
            continue

        # if interface is for an already configured bridge, skip it
        if is_interface_bridge(iface, args.distro):
            log.debug("Skipping bridge %s" % iface)
            continue

        mac_addr_type = open(
            '%s/%s/addr_assign_type' % (sys_root, iface), 'r').read().strip()
        # Interfaces without a permanent address are likely created by some
        # other system on the host like a running neutron agent. In these cases
        # that system should be responsible for configuring the interface not
        # glean.
        if mac_addr_type != PERMANENT_ADDR_TYPE:
            continue

        mac = open('%s/%s/address' % (sys_root, iface), 'r').read().strip()

        # Hack alert!  If we have been given a single interface
        # argument (hence called_from_udev is true), that means we
        # have been called for just one nic by udev in response to the
        # "net" "add" action matching.  We are going to assume that if
        # we made it this far (i.e. past the filters above) this
        # interface should be configured.
        #
        # It is unclear, as at 2019-10, if there are active jobs
        # relying on the "probe" path below.  The only way to get into
        # this path is being called from init scripts on a pre-systemd
        # platform that does not use udev activiation; this would mean
        # (as at this writing) Trusty (CentOS 6 being long gone).
        #
        # In short, it tries to bring up *all* the interfaces, and if
        # they don't come up, it figures they're not valid and
        # excludes them.  This introduces a very tricky race -- by
        # bringing the interface up it can start accepting RA
        # broadcasts and possibly have the kernel configure it with an
        # ipv6 addresses.  Then, network-manager will see the
        # interface is already configured, and out of an abdundance of
        # caution, refuse to re-configure it.  You end up with broken
        # networking.
        #
        # This is racy; you might get lucky and the RA timeout is long
        # enough that network-manager starts before this happens.  So
        # it is not exactly correct to say that the probe path is
        # completely broken; it is possible users have just not
        # noticed or are tacitly relying on it.
        #
        # While we consider this, assuming that if we are called from
        # udev that the interface is to be configured here, and not
        # bringing it "up", avoids this issue.
        if called_from_udev:
            log.debug("Interface matched: %s (%s)", iface, mac)
            sys_interfaces[mac] = iface
            return sys_interfaces

        # check if interface is up if not try and bring it up
        if interface_live(iface, sys_root, args):
            if_dict[iface] = mac

    # wait up to 9 seconds all interfaces to reach up
    log.debug("Waiting for interfaces to become active.")
    if_up_list = []
    for x in range(0, 90):
        for iface in if_dict:
            mac = if_dict[iface]
            if iface in if_up_list:
                continue
            log.debug("Checking liveness of %s", mac)
            if is_interface_live(iface, sys_root):
                # Add system interface
                sys_interfaces[mac] = iface
                log.debug("Added system interface %s (%s)" % (iface, mac))
                if_up_list.append(iface)

        if sorted(if_up_list) == sorted(if_dict.keys()):
            # all interfaces are up no need to continue looping
            break
        time.sleep(.1)

    if sorted(if_up_list) != sorted(if_dict.keys()):
        # not all interfaces became active with in the time limit
        for iface in if_dict:
            if iface in if_up_list:
                continue
            msg = "Skipping system interface %s (%s)" % (iface,
                                                         if_dict[iface])
            log.warning(msg)

    log.debug("WARNING: interfaces have been brought 'up' during the probing"
              "process.  This may cause problems if IPv6 RA have"
              "been accepted")
    return sys_interfaces


def get_network_info(args):
    """Retrieves network info from config-drive.

    If there is no meta_data.json in config-drive, it means that there
    is no config drive mounted- which means we know nothing.
    """
    config_drive = os.path.join(args.root, 'mnt/config')
    network_info_file = '%s/openstack/latest/network_info.json' % config_drive
    network_data_file = '%s/openstack/latest/network_data.json' % config_drive
    vendor_data_file = '%s/openstack/latest/vendor_data.json' % config_drive
    network_info = {}
    if os.path.exists(network_info_file):
        log.debug("Found network_info file %s" % network_info_file)
        network_info = json.load(open(network_info_file))
    # network_data.json is the file written by nova that should be there.
    # Other cloud deployments may use the above network_info.json or
    # vendor_data.json but the canonical location is this one.
    if os.path.exists(network_data_file):
        log.debug("Found network_info file %s" % network_data_file)
        network_info = json.load(open(network_data_file))
    elif os.path.exists(vendor_data_file):
        log.debug("Found vendor_data_file file %s" % vendor_data_file)
        vendor_data = json.load(open(vendor_data_file))
        if 'network_info' in vendor_data:
            log.debug("Found network_info in vendor_data_file")
            network_info = vendor_data['network_info']
    else:
        log.debug("Did not find vendor_data or network_info in config-drive")

    if not network_info:
        log.debug("Found no network_info in config-drive! "
                  "Assuming DHCP interfaces")

    return network_info


def write_network_info_from_config_drive(args, meta_data):
    """Write network info from config-drive.

    If there is no meta_data.json in config-drive, it means that there
    is no config drive mounted- which means we know nothing.

    Returns False on any issue, which will cause the writing of
    DHCP network files.
    """
    network_info = get_network_info(args)

    dns = {}
    if not args.skip_dns:
        dns = write_dns_info(get_dns_from_config_drive(network_info), args)
    interfaces = get_config_drive_interfaces(network_info)
    sys_interfaces = get_sys_interfaces(args.interface, args)

    write_static_network_info(interfaces, sys_interfaces, dns, args)


def write_ssh_keys(args, meta_data):
    """Write ssh-keys from config-drive.

    If there is no 'public_keys' key in meta_data.json in
    config-drive, it means that there is no config drive mounted-
    which means we do nothing.

    """
    ssh_path = os.path.join(args.root, 'root/.ssh')
    if 'public_keys' not in meta_data:
        return 0

    keys_to_write = []

    # if we have keys already there, we want to preserve them
    if os.path.exists('/root/.ssh/authorized_keys'):
        with open('/root/.ssh/authorized_keys', 'r') as fk:
            for line in fk:
                keys_to_write.append(line.strip())
    for (name, key) in meta_data['public_keys'].items():
        key_title = "# Injected key {name} by keypair extension".format(
            name=name)
        if key_title not in keys_to_write:
            keys_to_write.append(key_title)

        if key.rstrip() not in keys_to_write:
            keys_to_write.append(key)

    files_to_write = {
        '/root/.ssh/authorized_keys': '\n'.join(keys_to_write) + '\n',
    }
    try:
        os.mkdir(ssh_path, 0o700)
    except OSError as e:
        if e.errno != 17:  # not File Exists
            raise
    finish_files(files_to_write, args)


def set_hostname_from_config_drive(args, meta_data):
    if args.noop:
        return

    if 'name' not in meta_data:
        return

    hostname = meta_data['name']
    log.debug("Got hostname from meta_data.json : %s" % hostname)
    # underscore is not a valid hostname, but it's easy to name your
    # host with that on the command-line.  be helpful...
    if '_' in hostname:
        hostname = hostname.replace('_', '-')
        log.debug("Fixed up hostname to %s" % hostname)

    ret = subprocess.call(['hostname', hostname])
    if ret != 0:
        raise RuntimeError('Error setting hostname')
    else:
        # gentoo's hostname file is in a different location
        if args.distro == 'gentoo':
            with open('/etc/conf.d/hostname', 'w') as fh:
                fh.write("hostname=\"{host}\"\n".format(host=hostname))
        else:
            with safe_open('/etc/hostname', 'w') as fh:
                fh.write(hostname)
                fh.write('\n')

        # generate the lists of hosts and ips
        hosts_to_add = {'localhost': '127.0.0.1'}

        # get information on the network
        hostname_ip = '127.0.1.1'
        network_info = get_network_info(args)
        if network_info:
            interfaces = get_config_drive_interfaces(network_info)
            keys = sorted(interfaces.keys())

            for key in keys:
                interface = interfaces[key]
                if interface and 'ip_address' in interface:
                    hostname_ip = interface['ip_address']
                    break

        # check short hostname and generate list for hosts
        hosts_to_add[hostname] = hostname_ip
        short_hostname = hostname.split('.')[0]
        if short_hostname != hostname:
            hosts_to_add[short_hostname] = hostname_ip

        for host in hosts_to_add:
            host_value = hosts_to_add[host]
            # See if we already have a hosts entry for hostname
            if os.path.isfile('/etc/hosts'):
                with safe_open('/etc/hosts', 'r+') as fh:
                    for line in fh:
                        if line.startswith('%s %s' % (host_value, host)):
                            break
                    else:
                        fh.write(u'%s %s\n' % (host_value, host))
            else:
                with safe_open('/etc/hosts', 'a+') as fh:
                    fh.write(u'%s %s\n' % (host_value, host))


def main(argv=None):

    if argv is None:
        args = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Static network config")
    parser.add_argument(
        '-n', '--noop', action='store_true', help='Do not write files')
    _distro = distro.linux_distribution(
        full_distribution_name=False)[0].lower()
    parser.add_argument(
        '--distro', dest='distro', default=_distro,
        help='Override distro (detected "%s")' % _distro)
    parser.add_argument(
        '--root', dest='root', default='/',
        help='Mounted root for config drive info, defaults to /')
    parser.add_argument(
        '-i', '--interface', dest='interface',
        default=None, help="Interface to process")
    parser.add_argument(
        '--ssh', dest='ssh', action='store_true', help="Write ssh key")
    parser.add_argument(
        '--hostname', dest='hostname', action='store_true',
        help="Set the hostname if name is available in config drive.")
    parser.add_argument(
        '--skip-network', dest='skip', action='store_true',
        help="Do not write network info")
    parser.add_argument(
        '--use-nm', dest='use_nm', action='store_true',
        help=('Use NetworkManager instead of legacy'
              'configuration scripts to manage interfaces'))
    parser.add_argument(
        '--skip-dns', dest='skip_dns', action='store_true',
        help='Do not write dns info')
    parser.add_argument(
        '--debug', dest='debug', action='store_true',
        help="Enable debugging output")
    parser.add_argument(
        "--no-dhcp-fallback", action="store_true",
        help="Do not fall back to DHCP")
    args = parser.parse_args(argv)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    log.debug("Starting glean")
    log.debug("Detected distro : %s" % args.distro)
    log.debug("Configuring %s NetworkManager" %
              ("with" if args.use_nm else "without"))

    with systemlock.Lock('/tmp/glean.lock'):
        config_drive = os.path.join(args.root, 'mnt/config')
        meta_data_path = '%s/openstack/latest/meta_data.json' % config_drive
        if not os.path.exists(meta_data_path):
            log.debug("No meta_data.json found")
            meta_data = {}
        else:
            meta_data = json.load(open(meta_data_path))
            log.debug("metadata loaded from: %s" % meta_data_path)

        if args.ssh:
            write_ssh_keys(args, meta_data)
        if args.hostname:
            set_hostname_from_config_drive(args, meta_data)
        if args.interface != 'lo' and not args.skip:
            write_network_info_from_config_drive(args, meta_data)
    log.debug("Done!")
    return 0


if __name__ == '__main__':
    sys.exit(main())
