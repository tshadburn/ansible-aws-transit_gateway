#!/usr/bin/python
#
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type


DOCUMENTATION = r'''
---
module: ec2_transit_gateway_route_table
version_added: 1.0.0
short_description: Manage route tables for AWS transit gateways
description:
    - Manage route tables for AWS transit gateways
author:
- Todd Shadburn (@tshadburn)
- Robert Estelle (@erydo)
- Rob White (@wimnat)
- Will Thames (@willthames)
options:
  lookup:
    description: Look up route table by either tags or by route table ID. Non-unique tag lookup will fail.
      If no tags are specified then no lookup for an existing route table is performed and a new
      route table will be created. To change tags of a route table you must look up by id.
    default: tag
    choices: [ 'tag', 'id' ]
    type: str
  purge_tags:
    description: Purge existing tags that are not found in route table.
    type: bool
    default: 'no'
  route_table_id:
    description:
    - The ID of the route table to update or delete.
    - Required when I(lookup=id).
    type: str
  associations:
    description: List of attachment associations in the route table.
        Associations are specified as a list containing attachment IDs
    type: list
    elements: str
  routes:
    description: List of routes in the route table.
        Routes are specified as dicts containing the keys 'dest_cidr' and 'tgw_attachment_id'
    type: list
    elements: dict
  state:
    description: Create or destroy the VPC route table.
    default: present
    choices: [ 'present', 'absent' ]
    type: str
  tags:
    description: >
      A dictionary of resource tags of the form: C({ tag1: value1, tag2: value2 }). Tags are
      used to uniquely identify route tables within a VPC when the route_table_id is not supplied.
    aliases: [ "resource_tags" ]
    type: dict
  tgw_id:
    description:
    - ID of the transit gateway in which to create the route table.
    - Required when I(state=present) or I(lookup=tag).
    type: str
extends_documentation_fragment:
- amazon.aws.aws
- amazon.aws.ec2

'''

EXAMPLES = r'''
# Note: These examples do not set authentication details, see the AWS Guide for details.

# Basic creation example:
- name: Set up route table
  community.aws.ec2_transit_gateway_route_table:
    tgw_id: tgw-1245678
    region: us-west-1
    tags:
      Name: Public
    associations:
        - tgw-attach-123456789
        - tgw-attach-234567890
    routes:
        - dest_cidr: 10.2.0.0/16
          tgw_attachment_id: tgw-attach-123456789
        - dest_cidr: 10.3.0.0/16
          tgw_attachment_id: tgw-attach-234567890
  register: tgw_route_table

- name: delete route table
  community.aws.ec2_transit_gateway_route_table:
    tgw_id: tgw-1245678
    route_table_id: "{{ tgw_route_table.id }}"
    region: us-west-1
    state: absent
'''

RETURN = r'''
route_table:
  description: Route Table result
  returned: always
  type: complex
  contains:
    associations:
      description: List of subnets associated with the route table
      returned: always
      type: complex
      contains:
        main:
          description: Whether this is the main route table
          returned: always
          type: bool
          sample: false
        route_table_association_id:
          description: ID of association between route table and subnet
          returned: always
          type: str
          sample: rtbassoc-ab47cfc3
        route_table_id:
          description: ID of the route table
          returned: always
          type: str
          sample: rtb-bf779ed7
        subnet_id:
          description: ID of the subnet
          returned: always
          type: str
          sample: subnet-82055af9
    id:
      description: ID of the route table (same as route_table_id for backwards compatibility)
      returned: always
      type: str
      sample: rtb-bf779ed7
    propagating_vgws:
      description: List of Virtual Private Gateways propagating routes
      returned: always
      type: list
      sample: []
    route_table_id:
      description: ID of the route table
      returned: always
      type: str
      sample: rtb-bf779ed7
    routes:
      description: List of routes in the route table
      returned: always
      type: complex
      contains:
        destination_cidr_block:
          description: CIDR block of destination
          returned: always
          type: str
          sample: 10.228.228.0/22
        gateway_id:
          description: ID of the gateway
          returned: when gateway is local or internet gateway
          type: str
          sample: local
        instance_id:
          description: ID of a NAT instance
          returned: when the route is via an EC2 instance
          type: str
          sample: i-abcd123456789
        instance_owner_id:
          description: AWS account owning the NAT instance
          returned: when the route is via an EC2 instance
          type: str
          sample: 123456789012
        nat_gateway_id:
          description: ID of the NAT gateway
          returned: when the route is via a NAT gateway
          type: str
          sample: local
        origin:
          description: mechanism through which the route is in the table
          returned: always
          type: str
          sample: CreateRouteTable
        state:
          description: state of the route
          returned: always
          type: str
          sample: active
    tags:
      description: Tags applied to the route table
      returned: always
      type: dict
      sample:
        Name: Public route table
        Public: 'true'
    vpc_id:
      description: ID for the VPC in which the route lives
      returned: always
      type: str
      sample: vpc-6e2d2407
'''

import re
from time import sleep
from ansible_collections.amazon.aws.plugins.module_utils.core import AnsibleAWSModule
from ansible_collections.amazon.aws.plugins.module_utils.waiters import get_waiter
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import ansible_dict_to_boto3_filter_list
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import camel_dict_to_snake_dict, snake_dict_to_camel_dict
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import ansible_dict_to_boto3_tag_list, boto3_tag_list_to_ansible_dict
from ansible_collections.amazon.aws.plugins.module_utils.ec2 import compare_aws_tags, AWSRetry


try:
    import botocore
except ImportError:
    pass  # caught by AnsibleAWSModule


@AWSRetry.exponential_backoff()
def describe_tags_with_backoff(connection, resource_id):
    filters = ansible_dict_to_boto3_filter_list({'resource-id': resource_id})
    paginator = connection.get_paginator('describe_tags')
    tags = paginator.paginate(Filters=filters).build_full_result()['Tags']
    return boto3_tag_list_to_ansible_dict(tags)


def tags_match(match_tags, candidate_tags):
    return all((k in candidate_tags and candidate_tags[k] == v
                for k, v in match_tags.items()))


def ensure_tags(connection=None, module=None, resource_id=None, tags=None, purge_tags=None, check_mode=None):
    try:
        cur_tags = describe_tags_with_backoff(connection, resource_id)
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg='Unable to list tags for VPC')

    to_add, to_delete = compare_aws_tags(cur_tags, tags, purge_tags)

    if not to_add and not to_delete:
        return {'changed': False, 'tags': cur_tags}
    if check_mode:
        if not purge_tags:
            tags = cur_tags.update(tags)
        return {'changed': True, 'tags': tags}

    if to_delete:
        try:
            connection.delete_tags(Resources=[resource_id], Tags=[{'Key': k} for k in to_delete])
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
            module.fail_json_aws(e, msg="Couldn't delete tags")
    if to_add:
        try:
            connection.create_tags(Resources=[resource_id], Tags=ansible_dict_to_boto3_tag_list(to_add))
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
            module.fail_json_aws(e, msg="Couldn't create tags")

    try:
        latest_tags = describe_tags_with_backoff(connection, resource_id)
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg='Unable to list tags for VPC')
    return {'changed': True, 'tags': latest_tags}


@AWSRetry.exponential_backoff()
def describe_route_tables_with_backoff(connection, **params):
    try:
        return connection.describe_transit_gateway_route_tables(**params)['TransitGatewayRouteTables']
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'InvalidRouteTableID.NotFound':
            return None
        else:
            raise


def get_route_table_by_id(connection, module, route_table_id):

    route_table = None
    try:
        route_tables = describe_route_tables_with_backoff(connection, TransitGatewayRouteTableIds=[route_table_id])
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg="Couldn't get route table")
    if route_tables:
        route_table = route_tables[0]

    return route_table


def get_route_table_by_tags(connection, module, tgw_id, tags):
    count = 0
    route_table = None
    filters = ansible_dict_to_boto3_filter_list({'transit-gateway-id': tgw_id})
    try:
        route_tables = describe_route_tables_with_backoff(connection, Filters=filters)
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg="Couldn't get route table")
    for table in route_tables:
        this_tags = describe_tags_with_backoff(connection, table['TransitGatewayRouteTableId'])
        if tags_match(tags, this_tags):
            route_table = table
            count += 1

    if count > 1:
        module.fail_json(msg="Tags provided do not identify a unique route table")
    else:
        return route_table


def rename_key(d, old_key, new_key):
    d[new_key] = d.pop(old_key)



def ensure_route_table_absent(connection, module):

    lookup = module.params.get('lookup')
    route_table_id = module.params.get('route_table_id')
    tags = module.params.get('tags')
    tgw_id = module.params.get('tgw_id')

    if lookup == 'tag':
        if tags is not None:
            route_table = get_route_table_by_tags(connection, module, tgw_id, tags)
        else:
            route_table = None
    elif lookup == 'id':
        route_table = get_route_table_by_id(connection, module, route_table_id)

    if route_table is None:
        return {'changed': False}


    # disassociate subnets before deleting route table
    if not module.check_mode:
        ## TODO disassociate attachments before deleting route table

        #ensure_subnet_associations(connection=connection, module=module, route_table=route_table,
        #                           check_mode=False)

        try:
            connection.delete_transit_gateway_route_table(TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'])
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
            module.fail_json_aws(e, msg="Error deleting route table")

    return {'changed': True}


def get_route_table_info(connection, module, route_table):
    result = get_route_table_by_id(connection, module, route_table['TransitGatewayRouteTableId'])
    try:
        result['Tags'] = describe_tags_with_backoff(connection, route_table['TransitGatewayRouteTableId'])
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg="Couldn't get tags for route table")
    result = camel_dict_to_snake_dict(result, ignore_list=['Tags'])
    # backwards compatibility
    result['id'] = result['transit_gateway_route_table_id']
    return result


def create_route_spec(connection, module, tgw_id):
    routes = module.params.get('routes')

    for route_spec in routes:
        rename_key(route_spec, 'dest', 'destination_cidr_block')

        if route_spec.get('gateway_id') and route_spec['gateway_id'].lower() == 'igw':
            igw = find_igw(connection, module, tgw_id)
            route_spec['gateway_id'] = igw
        if route_spec.get('gateway_id') and route_spec['gateway_id'].startswith('nat-'):
            rename_key(route_spec, 'gateway_id', 'nat_gateway_id')

    return snake_dict_to_camel_dict(routes, capitalize_first=True)


def ensure_associations(connection, module, route_table, associations=[]):
    changed = False

    check_mode=module.check_mode

    try:
        assoc_list = connection.get_transit_gateway_route_table_associations(TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'])['Associations']
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg='No associations found for {0}'.format(route_table['TransitGatewayRouteTableId']))

    # create simple attachment list for comparisons below
    existing_attachments = []
    for assoc in assoc_list:
        existing_attachments.append(assoc['TransitGatewayAttachmentId'])

    # check for missing associations
    for assoc in associations:
        if not assoc in existing_attachments:
            # Create the association
            changed = True
            if not module.check_mode:
                try:
                    result = connection.associate_transit_gateway_route_table(
                                 TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'],
                                 TransitGatewayAttachmentId=assoc)
                except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                    module.fail_json_aws(e, msg='Failed to create asociation for {0} on {1}'.format(assoc, route_table['TransitGatewayRouteTableId']))

    # check for errant associations
    for assoc in existing_attachments:
        if not assoc in associations:
            # Delete the errant association
            changed = True
            if not module.check_mode:
                try:
                    result = connection.disassociate_transit_gateway_route_table(
                                 TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'],
                                 TransitGatewayAttachmentId=assoc)
                except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                    module.fail_json_aws(e, msg='Failed to delete asociation for {0} on {1}'.format(assoc, route_table['TransitGatewayRouteTableId']))

    if changed:
        sleep(15) # TODO make sure associations are 'associated' before continuing (need a waiter)
    return {'changed': changed}


def ensure_routes(connection=None, module=None, route_table=None, routes=[]):
    changed = False

    check_mode=module.check_mode

    filters = ansible_dict_to_boto3_filter_list({'type': 'active'})

    try:
        route_list = connection.search_transit_gateway_routes(TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'], Filters=filters)['Routes']
    except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
        module.fail_json_aws(e, msg='No routes found for {0}'.format(route_table['TransitGatewayRouteTableId']))

    # create attachment/cidr index for comparisons below
    existing_attachments = {}
    existing_cidrs = {}
    for route in route_list:
        for attach in route['TransitGatewayAttachments']:
            existing_attachments[attach['TransitGatewayAttachmentId']] = route['DestinationCidrBlock']
            existing_cidrs[route['DestinationCidrBlock']] = attach['TransitGatewayAttachmentId']

    # check for missing routes
    for route in routes:
        if not route['dest_cidr'] in existing_cidrs:
            # add route entry
            changed = True
            if not module.check_mode:
                try:
                    result = connection.create_transit_gateway_route(
                                 TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'],
                                 TransitGatewayAttachmentId=route['tgw_attachment_id'],
                                 DestinationCidrBlock=route['dest_cidr'])
                except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                    module.fail_json_aws(e, msg='Failed to create route for {0} on {1}'.format(route['tgw_attachment_id'], route_table['TransitGatewayRouteTableId']))

    # check for errant routes
    for cidr in existing_cidrs.keys():
        found = 0
        for route in routes:
            if route['dest_cidr'] == cidr:
                found = 1
        if found == 0:
            # delete the route
            changed = True
            if not module.check_mode:
                try:
                    result = connection.delete_transit_gateway_route(
                                 TransitGatewayRouteTableId=route_table['TransitGatewayRouteTableId'],
                                 DestinationCidrBlock=route['dest_cidr'])
                except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                    module.fail_json_aws(e, msg='Failed to delete route for {0} on {1}'.format(route['dest_cidr'], route_table['TransitGatewayRouteTableId']))

    if changed:
        sleep(15) # TODO make sure routes are 'available' before continuing (need a waiter)
    return {'changed': changed}


def ensure_route_table_present(connection, module):

    lookup = module.params.get('lookup')
    purge_tags = module.params.get('purge_tags')
    route_table_id = module.params.get('route_table_id')
    tags = module.params.get('tags')
    tgw_id = module.params.get('tgw_id')
    #routes = create_route_spec(connection, module, tgw_id)
    associations = module.params.get('associations', None)
    routes = module.params.get('routes', None)

    changed = False
    tags_valid = False

    if lookup == 'tag':
        if tags is not None:
            try:
                route_table = get_route_table_by_tags(connection, module, tgw_id, tags)
            except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                module.fail_json_aws(e, msg="Error finding route table with lookup 'tag'")
        else:
            route_table = None
    elif lookup == 'id':
        try:
            route_table = get_route_table_by_id(connection, module, route_table_id)
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
            module.fail_json_aws(e, msg="Error finding route table with lookup 'id'")

    # If no route table returned then create new route table
    if route_table is None:
        changed = True
        if not module.check_mode:
            try:
                route_table = connection.create_transit_gateway_route_table(TransitGatewayId=tgw_id)['TransitGatewayRouteTable']
                sleep(30) # TODO need a waiter
            except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as e:
                module.fail_json_aws(e, msg="Error creating route table")
        else:
            route_table = {"id": "rtb-xxxxxxxx", "route_table_id": "rtb-xxxxxxxx", "tgw_id": tgw_id}
            module.exit_json(changed=changed, route_table=route_table)


    if not tags_valid and tags is not None:
        result = ensure_tags(connection=connection, module=module, resource_id=route_table['TransitGatewayRouteTableId'],
                             tags=tags, purge_tags=purge_tags, check_mode=module.check_mode)
        route_table['Tags'] = result['tags']
        changed = changed or result['changed']


    if associations is not None:
        result = ensure_associations(connection=connection, module=module, route_table=route_table,
                                     associations=associations)
        changed = changed or result['changed']

    if routes is not None:
        result = ensure_routes(connection=connection, module=module, route_table=route_table,
                                     routes=routes)
        changed = changed or result['changed']

    if changed:
        # pause to allow route table routes/subnets/associations to be updated before exiting with final state
        sleep(5) # TODO need a waiter
    module.exit_json(changed=changed, route_table=get_route_table_info(connection, module, route_table))


def main():
    argument_spec = dict(
        lookup=dict(default='tag', choices=['tag', 'id']),
        purge_tags=dict(default=False, type='bool'),
        route_table_id=dict(),
        state=dict(default='present', choices=['present', 'absent']),
        tags=dict(type='dict', aliases=['resource_tags']),
        tgw_id=dict(),
        associations=dict(type='list', elements='str'),
        routes=dict(type='list', elements='dict'),
    )

    module = AnsibleAWSModule(argument_spec=argument_spec,
                              required_if=[['lookup', 'id', ['route_table_id']],
                                           ['lookup', 'tag', ['tgw_id']],
                                           ['state', 'present', ['tgw_id']]],
                              supports_check_mode=True)

    connection = module.client('ec2')

    state = module.params.get('state')

    if state == 'present':
        result = ensure_route_table_present(connection, module)
    elif state == 'absent':
        result = ensure_route_table_absent(connection, module)

    module.exit_json(**result)


if __name__ == '__main__':
    main()

