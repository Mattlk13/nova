# Copyright 2013 Intel Corporation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

""" Example of a PCI alias::

        | [pci]
        | alias = '{
        |   "name": "QuickAssist",
        |   "product_id": "0443",
        |   "vendor_id": "8086",
        |   "device_type": "type-PCI",
        |   "numa_policy": "legacy"
        |   }'

    Aliases with the same name, device_type and numa_policy are ORed::

        | [pci]
        | alias = '{
        |   "name": "QuickAssist",
        |   "product_id": "0442",
        |   "vendor_id": "8086",
        |   "device_type": "type-PCI",
        |   }'

    These two aliases define a device request meaning: vendor_id is "8086" and
    product_id is "0442" or "0443".
    """
import functools
import typing as ty

import jsonschema
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import uuidutils

import nova.conf
from nova import context as ctx
from nova import exception
from nova.i18n import _
from nova.network import model as network_model
from nova import objects
from nova.objects import fields as obj_fields
from nova.pci import utils
from oslo_utils import strutils

Alias = ty.Dict[str, ty.Tuple[str, ty.List[ty.Dict[str, str]]]]

PCI_NET_TAG = 'physical_network'
PCI_TRUSTED_TAG = 'trusted'
PCI_DEVICE_TYPE_TAG = 'dev_type'
PCI_REMOTE_MANAGED_TAG = 'remote_managed'

DEVICE_TYPE_FOR_VNIC_TYPE = {
    network_model.VNIC_TYPE_DIRECT_PHYSICAL: obj_fields.PciDeviceType.SRIOV_PF,
    network_model.VNIC_TYPE_VDPA: obj_fields.PciDeviceType.VDPA,
}

CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)

_ALIAS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
        },
        # TODO(stephenfin): This isn't used anywhere outside of tests and
        # should probably be removed.
        "capability_type": {
            "type": "string",
            "enum": ['pci'],
        },
        "product_id": {
            "type": "string",
            "pattern": utils.PCI_VENDOR_PATTERN,
        },
        "vendor_id": {
            "type": "string",
            "pattern": utils.PCI_VENDOR_PATTERN,
        },
        "device_type": {
            "type": "string",
            # NOTE(sean-k-mooney): vDPA devices cannot currently be used with
            # alias-based PCI passthrough so we exclude it here
            "enum": [
                obj_fields.PciDeviceType.STANDARD,
                obj_fields.PciDeviceType.SRIOV_PF,
                obj_fields.PciDeviceType.SRIOV_VF,
            ],
        },
        "numa_policy": {
            "type": "string",
            "enum": list(obj_fields.PCINUMAAffinityPolicy.ALL),
        },
        "resource_class": {
            "type": "string",
        },
        "traits": {
            "type": "string",
        },
        "live_migratable": {
            "type": "string",
        },
    },
    "required": ["name"],
}


def _validate_multispec(aliases):
    if CONF.filter_scheduler.pci_in_placement:
        alias_with_multiple_specs = [
            name for name, spec in aliases.items() if len(spec[1]) > 1]
        if alias_with_multiple_specs:
            raise exception.PciInvalidAlias(
                "The PCI alias(es) %s have multiple specs but "
                "[filter_scheduler]pci_in_placement is True. The PCI in "
                "Placement feature only supports one spec per alias. You can "
                "assign the same resource_class to multiple [pci]device_spec "
                "matchers to allow using different devices for the same alias."
                % ",".join(alias_with_multiple_specs))


def _validate_required_ids(aliases):
    if CONF.filter_scheduler.pci_in_placement:
        alias_without_ids_or_rc = set()
        for name, alias in aliases.items():
            for spec in alias[1]:
                ids = "vendor_id" in spec and "product_id" in spec
                rc = "resource_class" in spec
                if not ids and not rc:
                    alias_without_ids_or_rc.add(name)

        if alias_without_ids_or_rc:
            raise exception.PciInvalidAlias(
                "The PCI alias(es) %s does not have vendor_id and product_id "
                "fields set or resource_class field set."
                % ",".join(sorted(alias_without_ids_or_rc)))
    else:
        alias_without_ids = set()
        for name, alias in aliases.items():
            for spec in alias[1]:
                ids = "vendor_id" in spec and "product_id" in spec
                if not ids:
                    alias_without_ids.add(name)

        if alias_without_ids:
            raise exception.PciInvalidAlias(
                "The PCI alias(es) %s does not have vendor_id and product_id "
                "fields set."
                % ",".join(sorted(alias_without_ids)))


def _validate_aliases(aliases):
    """Checks the parsed aliases for common mistakes and raise easy to parse
    error messages
    """
    _validate_multispec(aliases)
    _validate_required_ids(aliases)


@functools.cache
def get_alias_from_config() -> Alias:
    """Parse and validate PCI aliases from the nova config.

    :returns: A dictionary where the keys are alias names and the values are
        tuples of form ``(numa_policy, specs)``. ``numa_policy`` describes the
        required NUMA affinity of the device(s), while ``specs`` is a list of
        PCI device specs.
    :raises: exception.PciInvalidAlias if two aliases with the same name have
        different device types or different NUMA policies.
    """
    jaliases = CONF.pci.alias
    # map alias name to alias spec list
    aliases: Alias = {}
    try:
        for jsonspecs in jaliases:
            spec = jsonutils.loads(jsonspecs)
            jsonschema.validate(spec, _ALIAS_SCHEMA)

            name = spec.pop('name').strip()
            numa_policy = spec.pop('numa_policy', None)
            if not numa_policy:
                numa_policy = obj_fields.PCINUMAAffinityPolicy.LEGACY

            dev_type = spec.pop('device_type', None)
            if dev_type:
                spec['dev_type'] = dev_type

            live_migratable = spec.pop('live_migratable', None)
            if live_migratable is not None:
                live_migratable = (
                    "true"
                    if strutils.bool_from_string(live_migratable, strict=True)
                    else "false"
                )
                spec['live_migratable'] = live_migratable

            if name not in aliases:
                aliases[name] = (numa_policy, [spec])
                continue

            if aliases[name][0] != numa_policy:
                reason = _("NUMA policy mismatch for alias '%s'") % name
                raise exception.PciInvalidAlias(reason=reason)

            if aliases[name][1][0]['dev_type'] != spec['dev_type']:
                reason = _("Device type mismatch for alias '%s'") % name
                raise exception.PciInvalidAlias(reason=reason)

            aliases[name][1].append(spec)
    except exception.PciInvalidAlias:
        raise
    except jsonschema.exceptions.ValidationError as exc:
        raise exception.PciInvalidAlias(reason=exc.message)
    except Exception as exc:
        raise exception.PciInvalidAlias(reason=str(exc))

    _validate_aliases(aliases)
    return aliases


def _translate_alias_to_requests(
    alias_spec: str, affinity_policy: ty.Optional[str] = None,
) -> ty.List['objects.InstancePCIRequest']:
    """Generate complete pci requests from pci aliases in extra_spec."""
    pci_aliases = get_alias_from_config()

    pci_requests: ty.List[objects.InstancePCIRequest] = []
    for name, count in [spec.split(':') for spec in alias_spec.split(',')]:
        name = name.strip()
        if name not in pci_aliases:
            raise exception.PciRequestAliasNotDefined(alias=name)

        numa_policy, spec = pci_aliases[name]
        policy = affinity_policy or numa_policy

        # NOTE(gibi): InstancePCIRequest has a requester_id field that could
        # be filled with the flavor.flavorid but currently there is no special
        # handling for InstancePCIRequests created from the flavor. So it is
        # left empty.
        pci_requests.append(objects.InstancePCIRequest(
            count=int(count),
            spec=spec,
            alias_name=name,
            numa_policy=policy,
            request_id=uuidutils.generate_uuid(),
        ))
    return pci_requests


def get_instance_pci_request_from_vif(
    context: ctx.RequestContext,
    instance: 'objects.Instance',
    vif: network_model.VIF,
) -> ty.Optional['objects.InstancePCIRequest']:
    """Given an Instance, return the PCI request associated
    to the PCI device related to the given VIF (if any) on the
    compute node the instance is currently running.

    In this method we assume a VIF is associated with a PCI device
    if 'pci_slot' attribute exists in the vif 'profile' dict.

    :param context: security context
    :param instance: instance object
    :param vif: network VIF model object
    :raises: raises PciRequestFromVIFNotFound if a pci device is requested
             but not found on current host
    :return: instance's PCIRequest object associated with the given VIF
             or None if no PCI device is requested
    """

    # Get PCI device address for VIF if exists
    vif_pci_dev_addr = vif['profile'].get('pci_slot') \
        if vif['profile'] else None

    if not vif_pci_dev_addr:
        return None

    try:
        cn_id = objects.ComputeNode.get_by_host_and_nodename(
            context,
            instance.host,
            instance.node).id
    except exception.NotFound:
        LOG.warning("expected to find compute node with host %s "
                    "and node %s when getting instance PCI request "
                    "from VIF", instance.host, instance.node)
        return None
    # Find PCIDevice associated with vif_pci_dev_addr on the compute node
    # the instance is running on.
    found_pci_dev = None
    for pci_dev in instance.pci_devices:
        if (pci_dev.compute_node_id == cn_id and
                pci_dev.address == vif_pci_dev_addr):
            found_pci_dev = pci_dev
            break
    if not found_pci_dev:
        return None
    # Find PCIRequest associated with the given PCIDevice in instance
    for pci_req in instance.pci_requests.requests:
        if pci_req.request_id == found_pci_dev.request_id:
            return pci_req

    raise exception.PciRequestFromVIFNotFound(
        pci_slot=vif_pci_dev_addr,
        node_id=cn_id)


def get_pci_requests_from_flavor(
    flavor: 'objects.Flavor', affinity_policy: ty.Optional[str] = None,
) -> 'objects.InstancePCIRequests':
    """Validate and return PCI requests.

    The ``pci_passthrough:alias`` extra spec describes the flavor's PCI
    requests. The extra spec's value is a comma-separated list of format
    ``alias_name_x:count, alias_name_y:count, ... ``, where ``alias_name`` is
    defined in ``pci.alias`` configurations.

    The flavor's requirement is translated into a PCI requests list. Each
    entry in the list is an instance of nova.objects.InstancePCIRequests with
    four keys/attributes.

    - 'spec' states the PCI device properties requirement
    - 'count' states the number of devices
    - 'alias_name' (optional) is the corresponding alias definition name
    - 'numa_policy' (optional) states the required NUMA affinity of the devices

    For example, assume alias configuration is::

        {
            'vendor_id':'8086',
            'device_id':'1502',
            'name':'alias_1'
        }

    While flavor extra specs includes::

        'pci_passthrough:alias': 'alias_1:2'

    The returned ``pci_requests`` are::

        [{
            'count':2,
            'specs': [{'vendor_id':'8086', 'device_id':'1502'}],
            'alias_name': 'alias_1'
        }]

    :param flavor: The flavor to be checked
    :param affinity_policy: pci numa affinity policy
    :returns: A list of PCI requests
    :rtype: nova.objects.InstancePCIRequests
    :raises: exception.PciRequestAliasNotDefined if an invalid PCI alias is
        provided
    :raises: exception.PciInvalidAlias if the configuration contains invalid
        aliases.
    """
    pci_requests: ty.List[objects.InstancePCIRequest] = []
    if ('extra_specs' in flavor and
            'pci_passthrough:alias' in flavor['extra_specs']):
        pci_requests = _translate_alias_to_requests(
            flavor['extra_specs']['pci_passthrough:alias'],
            affinity_policy=affinity_policy)

    return objects.InstancePCIRequests(requests=pci_requests)
