# Copyright (C) 2016 EMC Corporation.
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

"""
Handles all requests relating to groups.
"""


import functools

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import timeutils

from cinder.common import constants
from cinder.db import base
from cinder import exception
from cinder.i18n import _, _LE, _LW
from cinder import objects
from cinder.objects import base as objects_base
from cinder.objects import fields as c_fields
import cinder.policy
from cinder import quota
from cinder.scheduler import rpcapi as scheduler_rpcapi
from cinder.volume import api as volume_api
from cinder.volume import rpcapi as volume_rpcapi
from cinder.volume import utils as vol_utils
from cinder.volume import volume_types


CONF = cfg.CONF

LOG = logging.getLogger(__name__)
GROUP_QUOTAS = quota.GROUP_QUOTAS
VALID_REMOVE_VOL_FROM_GROUP_STATUS = (
    'available',
    'in-use',
    'error',
    'error_deleting')
VALID_ADD_VOL_TO_GROUP_STATUS = (
    'available',
    'in-use')


def wrap_check_policy(func):
    """Check policy corresponding to the wrapped methods prior to execution.

    This decorator requires the first 3 args of the wrapped function
    to be (self, context, group)
    """
    @functools.wraps(func)
    def wrapped(self, context, target_obj, *args, **kwargs):
        check_policy(context, func.__name__, target_obj)
        return func(self, context, target_obj, *args, **kwargs)

    return wrapped


def check_policy(context, action, target_obj=None):
    target = {
        'project_id': context.project_id,
        'user_id': context.user_id,
    }

    if isinstance(target_obj, objects_base.CinderObject):
        # Turn object into dict so target.update can work
        target.update(
            target_obj.obj_to_primitive()['versioned_object.data'] or {})
    else:
        target.update(target_obj or {})

    _action = 'group:%s' % action
    cinder.policy.enforce(context, _action, target)


class API(base.Base):
    """API for interacting with the volume manager for groups."""

    def __init__(self, db_driver=None):
        self.scheduler_rpcapi = scheduler_rpcapi.SchedulerAPI()
        self.volume_rpcapi = volume_rpcapi.VolumeAPI()
        self.volume_api = volume_api.API()

        super(API, self).__init__(db_driver)

    def _extract_availability_zone(self, availability_zone):
        raw_zones = self.volume_api.list_availability_zones(enable_cache=True)
        availability_zones = set([az['name'] for az in raw_zones])
        if CONF.storage_availability_zone:
            availability_zones.add(CONF.storage_availability_zone)

        if availability_zone is None:
            if CONF.default_availability_zone:
                availability_zone = CONF.default_availability_zone
            else:
                # For backwards compatibility use the storage_availability_zone
                availability_zone = CONF.storage_availability_zone

        if availability_zone not in availability_zones:
            if CONF.allow_availability_zone_fallback:
                original_az = availability_zone
                availability_zone = (
                    CONF.default_availability_zone or
                    CONF.storage_availability_zone)
                LOG.warning(_LW("Availability zone '%(s_az)s' "
                                "not found, falling back to "
                                "'%(s_fallback_az)s'."),
                            {'s_az': original_az,
                             's_fallback_az': availability_zone})
            else:
                msg = _("Availability zone '%(s_az)s' is invalid.")
                msg = msg % {'s_az': availability_zone}
                raise exception.InvalidInput(reason=msg)

        return availability_zone

    def create(self, context, name, description, group_type,
               volume_types, availability_zone=None):
        check_policy(context, 'create')

        req_volume_types = []
        # NOTE: Admin context is required to get extra_specs of volume_types.
        req_volume_types = (self.db.volume_types_get_by_name_or_id(
            context.elevated(), volume_types))

        req_group_type = self.db.group_type_get(context, group_type)

        availability_zone = self._extract_availability_zone(availability_zone)
        kwargs = {'user_id': context.user_id,
                  'project_id': context.project_id,
                  'availability_zone': availability_zone,
                  'status': c_fields.GroupStatus.CREATING,
                  'name': name,
                  'description': description,
                  'volume_type_ids': volume_types,
                  'group_type_id': group_type}
        group = None
        try:
            group = objects.Group(context=context, **kwargs)
            group.create()
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Error occurred when creating group"
                              " %s."), name)

        request_spec_list = []
        filter_properties_list = []
        for req_volume_type in req_volume_types:
            request_spec = {'volume_type': req_volume_type.copy(),
                            'group_id': group.id}
            filter_properties = {}
            request_spec_list.append(request_spec)
            filter_properties_list.append(filter_properties)

        group_spec = {'group_type': req_group_type.copy(),
                      'group_id': group.id}
        group_filter_properties = {}

        # Update quota for groups
        self.update_quota(context, group, 1)

        self._cast_create_group(context, group,
                                group_spec,
                                request_spec_list,
                                group_filter_properties,
                                filter_properties_list)

        return group

    def _cast_create_group(self, context, group,
                           group_spec,
                           request_spec_list,
                           group_filter_properties,
                           filter_properties_list):

        try:
            for request_spec in request_spec_list:
                volume_type = request_spec.get('volume_type')
                volume_type_id = None
                if volume_type:
                    volume_type_id = volume_type.get('id')

                specs = {}
                if volume_type_id:
                    qos_specs = volume_types.get_volume_type_qos_specs(
                        volume_type_id)
                    specs = qos_specs['qos_specs']
                if not specs:
                    # to make sure we don't pass empty dict
                    specs = None

                volume_properties = {
                    'size': 0,  # Need to populate size for the scheduler
                    'user_id': context.user_id,
                    'project_id': context.project_id,
                    'status': 'creating',
                    'attach_status': 'detached',
                    'encryption_key_id': request_spec.get('encryption_key_id'),
                    'display_description': request_spec.get('description'),
                    'display_name': request_spec.get('name'),
                    'volume_type_id': volume_type_id,
                    'group_type_id': group.group_type_id,
                }

                request_spec['volume_properties'] = volume_properties
                request_spec['qos_specs'] = specs

            group_properties = {
                'size': 0,  # Need to populate size for the scheduler
                'user_id': context.user_id,
                'project_id': context.project_id,
                'status': 'creating',
                'display_description': group_spec.get('description'),
                'display_name': group_spec.get('name'),
                'group_type_id': group.group_type_id,
            }

            group_spec['volume_properties'] = group_properties
            group_spec['qos_specs'] = None

        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    group.destroy()
                finally:
                    LOG.error(_LE("Error occurred when building "
                                  "request spec list for group "
                                  "%s."), group.id)

        # Cast to the scheduler and let it handle whatever is needed
        # to select the target host for this group.
        self.scheduler_rpcapi.create_group(
            context,
            constants.VOLUME_TOPIC,
            group,
            group_spec=group_spec,
            request_spec_list=request_spec_list,
            group_filter_properties=group_filter_properties,
            filter_properties_list=filter_properties_list)

    def update_quota(self, context, group, num, project_id=None):
        reserve_opts = {'groups': num}
        try:
            reservations = GROUP_QUOTAS.reserve(context,
                                                project_id=project_id,
                                                **reserve_opts)
            if reservations:
                GROUP_QUOTAS.commit(context, reservations)
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    group.destroy()
                finally:
                    LOG.error(_LE("Failed to update quota for "
                                  "group %s."), group.id)

    @wrap_check_policy
    def delete(self, context, group, delete_volumes=False):
        if not group.host:
            self.update_quota(context, group, -1, group.project_id)

            LOG.debug("No host for group %s. Deleting from "
                      "the database.", group.id)
            group.destroy()

            return

        if not delete_volumes and group.status not in (
                [c_fields.GroupStatus.AVAILABLE,
                 c_fields.GroupStatus.ERROR]):
            msg = _("Group status must be available or error, "
                    "but current status is: %s") % group.status
            raise exception.InvalidGroup(reason=msg)

        volumes = self.db.volume_get_all_by_generic_group(context.elevated(),
                                                          group.id)
        if volumes and not delete_volumes:
            msg = (_("Group %s still contains volumes. "
                     "The delete-volumes flag is required to delete it.")
                   % group.id)
            LOG.error(msg)
            raise exception.InvalidGroup(reason=msg)

        volumes_model_update = []
        for volume in volumes:
            if volume['attach_status'] == "attached":
                msg = _("Volume in group %s is attached. "
                        "Need to detach first.") % group.id
                LOG.error(msg)
                raise exception.InvalidGroup(reason=msg)

            snapshots = objects.SnapshotList.get_all_for_volume(context,
                                                                volume['id'])
            if snapshots:
                msg = _("Volume in group still has "
                        "dependent snapshots.")
                LOG.error(msg)
                raise exception.InvalidGroup(reason=msg)

            volumes_model_update.append({'id': volume['id'],
                                         'status': 'deleting'})

        self.db.volumes_update(context, volumes_model_update)

        group.status = c_fields.GroupStatus.DELETING
        group.terminated_at = timeutils.utcnow()
        group.save()

        self.volume_rpcapi.delete_group(context, group)

    def update(self, context, group, name, description,
               add_volumes, remove_volumes):
        """Update group."""
        if group.status != c_fields.GroupStatus.AVAILABLE:
            msg = _("Group status must be available, "
                    "but current status is: %s.") % group.status
            raise exception.InvalidGroup(reason=msg)

        add_volumes_list = []
        remove_volumes_list = []
        if add_volumes:
            add_volumes = add_volumes.strip(',')
            add_volumes_list = add_volumes.split(',')
        if remove_volumes:
            remove_volumes = remove_volumes.strip(',')
            remove_volumes_list = remove_volumes.split(',')

        invalid_uuids = []
        for uuid in add_volumes_list:
            if uuid in remove_volumes_list:
                invalid_uuids.append(uuid)
        if invalid_uuids:
            msg = _("UUIDs %s are in both add and remove volume "
                    "list.") % invalid_uuids
            raise exception.InvalidVolume(reason=msg)

        volumes = self.db.volume_get_all_by_generic_group(context, group.id)

        # Validate name.
        if name == group.name:
            name = None

        # Validate description.
        if description == group.description:
            description = None

        # Validate volumes in add_volumes and remove_volumes.
        add_volumes_new = ""
        remove_volumes_new = ""
        if add_volumes_list:
            add_volumes_new = self._validate_add_volumes(
                context, volumes, add_volumes_list, group)
        if remove_volumes_list:
            remove_volumes_new = self._validate_remove_volumes(
                volumes, remove_volumes_list, group)

        if (name is None and description is None and not add_volumes_new and
                not remove_volumes_new):
            msg = (_("Cannot update group %(group_id)s "
                     "because no valid name, description, add_volumes, "
                     "or remove_volumes were provided.") %
                   {'group_id': group.id})
            raise exception.InvalidGroup(reason=msg)

        fields = {'updated_at': timeutils.utcnow()}

        # Update name and description in db now. No need to
        # to send them over through an RPC call.
        if name is not None:
            fields['name'] = name
        if description is not None:
            fields['description'] = description
        if not add_volumes_new and not remove_volumes_new:
            # Only update name or description. Set status to available.
            fields['status'] = 'available'
        else:
            fields['status'] = 'updating'

        group.update(fields)
        group.save()

        # Do an RPC call only if the update request includes
        # adding/removing volumes. add_volumes_new and remove_volumes_new
        # are strings of volume UUIDs separated by commas with no spaces
        # in between.
        if add_volumes_new or remove_volumes_new:
            self.volume_rpcapi.update_group(
                context, group,
                add_volumes=add_volumes_new,
                remove_volumes=remove_volumes_new)

    def _validate_remove_volumes(self, volumes, remove_volumes_list, group):
        # Validate volumes in remove_volumes.
        remove_volumes_new = ""
        for volume in volumes:
            if volume['id'] in remove_volumes_list:
                if volume['status'] not in VALID_REMOVE_VOL_FROM_GROUP_STATUS:
                    msg = (_("Cannot remove volume %(volume_id)s from "
                             "group %(group_id)s because volume "
                             "is in an invalid state: %(status)s. Valid "
                             "states are: %(valid)s.") %
                           {'volume_id': volume['id'],
                            'group_id': group.id,
                            'status': volume['status'],
                            'valid': VALID_REMOVE_VOL_FROM_GROUP_STATUS})
                    raise exception.InvalidVolume(reason=msg)
                # Volume currently in group. It will be removed from group.
                if remove_volumes_new:
                    remove_volumes_new += ","
                remove_volumes_new += volume['id']

        for rem_vol in remove_volumes_list:
            if rem_vol not in remove_volumes_new:
                msg = (_("Cannot remove volume %(volume_id)s from "
                         "group %(group_id)s because it "
                         "is not in the group.") %
                       {'volume_id': rem_vol,
                        'group_id': group.id})
                raise exception.InvalidVolume(reason=msg)

        return remove_volumes_new

    def _validate_add_volumes(self, context, volumes, add_volumes_list, group):
        add_volumes_new = ""
        for volume in volumes:
            if volume['id'] in add_volumes_list:
                # Volume already in group. Remove from add_volumes.
                add_volumes_list.remove(volume['id'])

        for add_vol in add_volumes_list:
            try:
                add_vol_ref = self.db.volume_get(context, add_vol)
            except exception.VolumeNotFound:
                msg = (_("Cannot add volume %(volume_id)s to "
                         "group %(group_id)s because volume cannot be "
                         "found.") %
                       {'volume_id': add_vol,
                        'group_id': group.id})
                raise exception.InvalidVolume(reason=msg)
            orig_group = add_vol_ref.get('group_id', None)
            if orig_group:
                # If volume to be added is already in the group to be updated,
                # it should have been removed from the add_volumes_list in the
                # beginning of this function. If we are here, it means it is
                # in a different group.
                msg = (_("Cannot add volume %(volume_id)s to group "
                         "%(group_id)s because it is already in "
                         "group %(orig_group)s.") %
                       {'volume_id': add_vol_ref['id'],
                        'group_id': group.id,
                        'orig_group': orig_group})
                raise exception.InvalidVolume(reason=msg)
            if add_vol_ref:
                add_vol_type_id = add_vol_ref.get('volume_type_id', None)
                if not add_vol_type_id:
                    msg = (_("Cannot add volume %(volume_id)s to group "
                             "%(group_id)s because it has no volume "
                             "type.") %
                           {'volume_id': add_vol_ref['id'],
                            'group_id': group.id})
                    raise exception.InvalidVolume(reason=msg)
                vol_type_ids = [v_type.id for v_type in group.volume_types]
                if add_vol_type_id not in vol_type_ids:
                    msg = (_("Cannot add volume %(volume_id)s to group "
                             "%(group_id)s because volume type "
                             "%(volume_type)s is not supported by the "
                             "group.") %
                           {'volume_id': add_vol_ref['id'],
                            'group_id': group.id,
                            'volume_type': add_vol_type_id})
                    raise exception.InvalidVolume(reason=msg)
                if (add_vol_ref['status'] not in
                        VALID_ADD_VOL_TO_GROUP_STATUS):
                    msg = (_("Cannot add volume %(volume_id)s to group "
                             "%(group_id)s because volume is in an "
                             "invalid state: %(status)s. Valid states are: "
                             "%(valid)s.") %
                           {'volume_id': add_vol_ref['id'],
                            'group_id': group.id,
                            'status': add_vol_ref['status'],
                            'valid': VALID_ADD_VOL_TO_GROUP_STATUS})
                    raise exception.InvalidVolume(reason=msg)

                # group.host and add_vol_ref['host'] are in this format:
                # 'host@backend#pool'. Extract host (host@backend) before
                # doing comparison.
                vol_host = vol_utils.extract_host(add_vol_ref['host'])
                group_host = vol_utils.extract_host(group.host)
                if group_host != vol_host:
                    raise exception.InvalidVolume(
                        reason=_("Volume is not local to this node."))

                # Volume exists. It will be added to CG.
                if add_volumes_new:
                    add_volumes_new += ","
                add_volumes_new += add_vol_ref['id']

            else:
                msg = (_("Cannot add volume %(volume_id)s to group "
                         "%(group_id)s because volume does not exist.") %
                       {'volume_id': add_vol_ref['id'],
                        'group_id': group.id})
                raise exception.InvalidVolume(reason=msg)

        return add_volumes_new

    def get(self, context, group_id):
        group = objects.Group.get_by_id(context, group_id)
        check_policy(context, 'get', group)
        return group

    def get_all(self, context, filters=None, marker=None, limit=None,
                offset=None, sort_keys=None, sort_dirs=None):
        check_policy(context, 'get_all')
        if filters is None:
            filters = {}

        if filters:
            LOG.debug("Searching by: %s", filters)

        if (context.is_admin and 'all_tenants' in filters):
            del filters['all_tenants']
            groups = objects.GroupList.get_all(
                context, filters=filters, marker=marker, limit=limit,
                offset=offset, sort_keys=sort_keys, sort_dirs=sort_dirs)
        else:
            groups = objects.GroupList.get_all_by_project(
                context, context.project_id, filters=filters, marker=marker,
                limit=limit, offset=offset, sort_keys=sort_keys,
                sort_dirs=sort_dirs)
        return groups
