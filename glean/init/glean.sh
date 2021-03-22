#!/bin/bash
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
#
# See the License for the specific language governing permissions and
# limitations under the License.

# dib-lint: disable=dibdebugtrace
set -eu
set -o pipefail

PATH=/usr/local/bin:/bin:/sbin:/usr/bin:/usr/sbin

# python-glean is installed alongside us and runs glean (the python
# tool that actually does stuff).
_GLEAN_PATH=$(dirname "$0")

# NOTE(mnaser): Depending on the cloud, it may have `vfat` config drive which
#               comes with a capitalized label rather than all lowercase.
CONFIG_DRIVE_LABEL=""

if blkid -t LABEL="config-2" ; then
    CONFIG_DRIVE_LABEL="config-2"
elif blkid -t LABEL="CONFIG-2" ; then
    CONFIG_DRIVE_LABEL="CONFIG-2"
fi

# If the config drive exists we update the ssh keys, hostname and network
# interfaces. Otherwise we only update network interfaces with a dhcp
# fallback.
#
# Note we want to run as few glean processes as possible to cut down on
# runtime in resource constrained environments.
if [ -n "$CONFIG_DRIVE_LABEL" ]; then
    # Mount config drive
    mkdir -p /mnt/config
    BLOCKDEV="$(blkid -L ${CONFIG_DRIVE_LABEL})"
    TYPE="$(blkid -t LABEL=${CONFIG_DRIVE_LABEL} -s TYPE -o value)"
    if [[ "${TYPE}" == 'vfat' ]]; then
        mount -t vfat -o umask=0077 "${BLOCKDEV}" /mnt/config || true
    elif [[ "${TYPE}" == 'iso9660' ]]; then
        mount -t iso9660 -o ro,mode=0700 "${BLOCKDEV}" /mnt/config || true
    else
        mount -o mode=0700 "${BLOCKDEV}" /mnt/config || true
    fi
    $_GLEAN_PATH/python-glean --ssh --hostname $@
else
    $_GLEAN_PATH/python-glean $@
fi
