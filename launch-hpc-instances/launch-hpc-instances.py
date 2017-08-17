#!/usr/bin/env python

import argparse
import datetime
import os
import re
import subprocess
import sys
import time
import traceback
import urllib
import uuid
import prettytable
from collections import namedtuple

from oslo_utils import importutils
from oslo_log import log as logging
from oslo_config import cfg
from oslo_utils import encodeutils

from glanceclient import client as glance_client
from keystoneclient.v2_0 import client as keystone_client
from novaclient import client as nova_client
from neutronclient.v2_0 import client as neutron_client

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
DOMAIN = "support"

TIMEOUT = 300

REGIONS = ['nz-hlz-1', 'nz-por-1', 'nz_wlg_2']

SERVER_GROUP_LIST = []

INSTANCE = namedtuple('instance', ['region_name', 'instance_id',
                                   'instance_name', 'networks'])


def prepare_log():
    logging.register_options(CONF)
    extra_log_level_defaults = [
        'dogpile=INFO',
        'routes=INFO'
        ]

    logging.set_defaults(
        default_log_levels=logging.get_default_log_levels() +
        extra_log_level_defaults)

    logging.setup(CONF, DOMAIN)


def arg(*args, **kwargs):
    def _decorator(func):
        func.__dict__.setdefault('arguments', []).insert(0, (args, kwargs))
        return func
    return _decorator


class CatalystCloudShell(object):

    # public network id
    NZ_HLZ_1_PUBLIC_NETWORK_ID = 'f10ad6de-a26d-4c29-8c64-2a7418d47f8f'
    NZ_POR_1_PUBLIC_NETWORK_ID = '849ab1e9-7ac5-4618-8801-e6176fbbcf30'
    NZ_WLG_2_PUBLIC_NETWORK_ID = 'e0ba6b88-5360-492c-9c3d-119948356fd3'

    def get_base_parser(self):
            parser = argparse.ArgumentParser(
                prog='launch-hpc-instances',
                description='Tool to create compute instances on the Catalyst '
                            'Cloud that have their placement optimised for '
                            'CPU performance.',
                add_help=False,
            )

            # Global arguments
            parser.add_argument('-h', '--help',
                                action='store_true',
                                help=argparse.SUPPRESS,
                                )

            parser.add_argument('-a', '--os-auth-url', metavar='OS_AUTH_URL',
                                type=str, required=False, dest='OS_AUTH_URL',
                                default=os.environ.get('OS_AUTH_URL', None),
                                help='Keystone Authentication URL')

            parser.add_argument('-u', '--os-username', metavar='OS_USERNAME',
                                type=str, required=False, dest='OS_USERNAME',
                                default=os.environ.get('OS_USERNAME', None),
                                help='Username for authentication')

            parser.add_argument('-p', '--os-password', metavar='OS_PASSWORD',
                                type=str, required=False, dest='OS_PASSWORD',
                                default=os.environ.get('OS_PASSWORD', None),
                                help='Password for authentication')

            parser.add_argument('-t', '--os-tenant-name',
                                metavar='OS_TENANT_NAME',
                                type=str, required=False,
                                dest='OS_TENANT_NAME',
                                default=os.environ.get('OS_TENANT_NAME', None),
                                help='Tenant name for authentication')

            parser.add_argument('-r', '--os-region-name',
                                metavar='OS_REGION_NAME',
                                type=str, required=False,
                                dest='OS_REGION_NAME',
                                default=os.environ.get('OS_REGION_NAME', None),
                                help='Region for authentication')

            parser.add_argument('-c', '--os-cacert', metavar='OS_CACERT',
                                dest='OS_CACERT',
                                default=os.environ.get('OS_CACERT'),
                                help='Path of CA TLS certificate(s) used to '
                                'verify the remote server\'s certificate. '
                                'Without this option glance looks for the '
                                'default system CA certificates.')

            parser.add_argument('-k', '--insecure',
                                default=False,
                                action='store_true', dest='OS_INSECURE',
                                help='Explicitly allow script to perform '
                                '\"insecure SSL\" (https) requests. '
                                'The server\'s certificate will not be '
                                'verified against any certificate authorities.'
                                ' This option should be used with caution.')

            return parser

    def get_subcommand_parser(self):
        parser = self.get_base_parser()
        self.subcommands = {}
        subparsers = parser.add_subparsers(metavar='<subcommand>')
        submodule = importutils.import_module('launch-hpc-instances')
        self._find_actions(subparsers, submodule)
        self._find_actions(subparsers, self)
        return parser

    def _find_actions(self, subparsers, actions_module):
        for attr in (a for a in dir(actions_module) if a.startswith('do_')):
            command = attr[3:].replace('_', '-')
            callback = getattr(actions_module, attr)
            desc = callback.__doc__ or ''
            help = desc.strip().split('\n')[0]
            arguments = getattr(callback, 'arguments', [])

            subparser = subparsers.add_parser(command,
                                              help=help,
                                              description=desc,
                                              add_help=False,
                                              formatter_class=HelpFormatter
                                              )
            subparser.add_argument('-h', '--help',
                                   action='help',
                                   help=argparse.SUPPRESS,
                                   )
            self.subcommands[command] = subparser
            for (args, kwargs) in arguments:
                subparser.add_argument(*args, **kwargs)
            subparser.set_defaults(func=callback)

    @arg('command', metavar='<subcommand>', nargs='?',
         help='Display help for <subcommand>.')
    def do_help(self, args):
        """Display help about this program or one of its subcommands."""
        if getattr(args, 'command', None):
            if args.command in self.subcommands:
                self.subcommands[args.command].print_help()
            else:
                raise Exception("'%s' is not a valid subcommand" %
                                args.command)
        else:
            self.parser.print_help()

    def init_client(self, args):
        if not args.OS_AUTH_URL:
            LOG.critical("Please specify auth information")
            sys.exit(1)
        try:
            from keystoneauth1.identity import generic
            from keystoneauth1 import session

            auth = generic.Password(auth_url=args.OS_AUTH_URL,
                                    username=args.OS_USERNAME,
                                    password=args.OS_PASSWORD,
                                    project_name=args.OS_TENANT_NAME,
                                    )
            sess = session.Session(auth=auth)

            keystone = keystone_client.Client(session=sess)
            self.keystone = keystone
        except Exception as e:
            raise e

        try:
            nova = nova_client.Client('2', session=sess,
                                      region_name=args.OS_REGION_NAME)
            self.nova = nova

            neutron = neutron_client.Client(session=sess,
                                            region_name=args.OS_REGION_NAME)
            self.neutron = neutron
            glance = glance_client.Client('1', session=sess,
                                          region_name=args.OS_REGION_NAME)
            self.glance = glance
        except Exception as e:
            raise e

    def main(self, argv):
        parser = self.get_base_parser()
        (options, args) = parser.parse_known_args(argv)

        subcommand_parser = self.get_subcommand_parser()
        self.parser = subcommand_parser

        if options.help or not argv:
            self.do_help(options)
            return 0

        args = subcommand_parser.parse_args(argv)
        if args.func == self.do_help:
            self.do_help(args)
            return 0

        try:
            args.func(self, args)
        except Exception:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_exception(exc_type, exc_value, exc_traceback,
                                      limit=2, file=sys.stdout)


class HelpFormatter(argparse.HelpFormatter):
    def start_section(self, heading):
        # Title-case the headings
        heading = '%s%s' % (heading[0].upper(), heading[1:])
        super(HelpFormatter, self).start_section(heading)


@arg('--instance-count', type=int, metavar='INSTANCE_COUNT',
     dest='INSTANCE_COUNT', default=5,
     help='How many instances will be created')
@arg('--assign-public-ip',
     dest='ASSIGN_PUBLIC_IP', action="store_true", default=False,
     help='If assign public ip for instances')
@arg('--path-cloud-init-script', type=str, metavar='PATH_CLOUD_INIT_SCRIPT',
     dest='PATH_CLOUD_INIT_SCRIPT',
     help='Path to cloud init script')
@arg('--name-prefix', type=str, metavar='NAME_PREFIX',
     dest='NAME_PREFIX', default="instance-",
     help='The name prefix for instances')
@arg('--image-name', type=str, metavar='IMAGE_NAME',
     dest='IMAGE_NAME', default="ubuntu-16.04-x86_64",
     help='Image name use to boot instances.')
@arg('--flavor-name', type=str, metavar='FLAVOR_NAME',
     dest='FLAVOR_NAME', default="c1.c1r1",
     help='Flavor name use to boot instances.')
@arg('--network-name', type=str, metavar='NETWORK_NAME',
     dest='NETWORK_NAME', default="private-net",
     help='Network name use to boot instances.')
@arg('--volume-size', type=int, metavar='VOLUME_SIZE',
     dest='VOLUME_SIZE', default=20,
     help='The size of the root volume of the instance.')
@arg('--keypair-name', type=str, metavar='KEYPAIR_NAME',
     dest='KEYPAIR_NAME',required=True,
     help='The name of keypair to be injected into instance')
def do_create(shell, args):
    """ Boot instances with anti-affinity policy
    """
    LOG.info("Launching %d instances across all regions" %
             args.INSTANCE_COUNT);
    instances = []
    for i in range(args.INSTANCE_COUNT):
        for region in REGIONS:
            group = _find_server_group(shell, region, args)
            if group["is_full"]:
                continue

            args.OS_REGION_NAME = region
            shell.init_client(args)
            capital_region = args.OS_REGION_NAME.replace('-', '_').upper()
            #shell.flavor_id = getattr(shell,  capital_region + '_FLAVOR_ID')
            #shell.network_id = getattr(shell, capital_region + '_NETWORK_ID')
            #shell.image_id = getattr(shell, capital_region + '_IMAGE_ID')
            shell.public_network_id = getattr(shell, capital_region + '_PUBLIC_NETWORK_ID')

            try:
                server = _create_server(shell,
                                        args.NAME_PREFIX + str(i),
                                        args.IMAGE_NAME,
                                        args.FLAVOR_NAME,
                                        args.NETWORK_NAME,
                                        args.KEYPAIR_NAME,
                                        args.VOLUME_SIZE,
                                        group["group"].id,
                                        path_cloud_init_script=args.PATH_CLOUD_INIT_SCRIPT)

                resp = _check_server_status(shell, server)
                if resp["active"]:
                    # If the server is created successfully, then try to
                    # create another one
                    LOG.info("Created %s successfully in %s" %
                             (server.name, region))
                    # Assign floating ip if it's active
                    if args.ASSIGN_PUBLIC_IP:
                        floating_ip = shell.neutron.create_floatingip(
                                    {"floatingip": {"floating_network_id":
                                                    shell.public_network_id}})
                        time.sleep(10)
                        server.add_floating_ip(floating_ip["floatingip"]["floating_ip_address"])
                        time.sleep(10)

                    # Get the latest status of server
                    server = shell.nova.servers.get(server.id)

                    inst = INSTANCE(region_name=region,
                                    instance_id=server.id,
                                    instance_name=server.name,
                                    networks=server.networks)
                    instances.append(inst)
                    break
                elif "No valid host" in resp["fault"]["message"]:
                    # If the server is failed then try to create it in
                    # another region
                    SERVER_GROUP_LIST[-1][region]["is_full"] = True
                    shell.nova.servers.delete(server.id)
                    continue
                else:
                    LOG.info("Failed to create server %s due to %s" %
                             (server.id, resp["fault"]))
            except Exception as e:
                LOG.error(e)

    LOG.info("Job finished. Instances have been created as below:")
    print_list(instances, ["region_name", "instance_id",
                         "instance_name", "networks"])


def _find_server_group(shell, region_name, args):
    # If there is no server group or all are full
    all_full = (len(SERVER_GROUP_LIST)> 0 and
                all([region["is_full"] for region in
                     SERVER_GROUP_LIST[-1].values()]))
    if (len(SERVER_GROUP_LIST) == 0 or all_full):
        # Would like to have same server group name for all regions
        group_name = "AF-" + str(uuid.uuid4())
        region_groups = {}
        for region in REGIONS:
            args.OS_REGION_NAME = region
            shell.init_client(args)

            # Clean old unused server groups
            old_groups = shell.nova.server_groups.list()
            for g in old_groups:
                try:
                    if g.name.startswith("AF-"):
                        shell.nova.server_groups.delete(g.id)
                except:
                    pass

            group = shell.nova.server_groups.create(group_name,
                                                    'anti-affinity')
            region_groups[region] = {"group": group, "is_full": False}

        LOG.info("Created anti-affinity group %s in all regions" % group_name)
        SERVER_GROUP_LIST.append(region_groups)

    return SERVER_GROUP_LIST[-1][region_name]


def _check_server_status(shell, server):
    def check():
        inst = shell.nova.servers.get(server.id)
        return inst.status == "ACTIVE"

    status = call_until_true(check, 60, 3)

    if status:
        return {"active": True, "fault": ""}
    else:
        return {"active": False, "fault": getattr(server, "fault", "")}


def _create_server(shell, name,
                   image_name,
                   flavor_name,
                   network_name,
                   keypair_name,
                   volume_size,
                   group_id,
                   path_cloud_init_script=None,
                   assign_public_ip=False):

    create_kwargs = {}

    if path_cloud_init_script:
        create_kwargs["userdata"] = open(path_cloud_init_script)

    try:
        flavors = shell.nova.flavors.list()
        for f in flavors:
            if f.name == flavor_name:
                shell.flavor_id = f.id
                break
        else:
            raise Exception("Can't find flavor %s " % flavor_name)
        images =  shell.glance.images.list()
        for i in images:
            if i.name == image_name:
                shell.image_id = i.id
                break
        else:
            raise Exception("Can't find image %s " % image_name)
        networks = shell.neutron.list_networks()
        for n in networks["networks"]:
            if n["name"] == network_name:
                shell.network_id = n["id"]
                break
        else:
            raise Exception("Can't find network %s " % network_name)

        dev_mapping_2 = {
             'device_name': None,
             'source_type': 'image',
             'destination_type': 'volume',
             'delete_on_termination': 'true',
             'uuid': shell.image_id,
             'volume_size': str(volume_size),
        }
        server = shell.nova.servers.create(name,
                                           shell.image_id,
                                           shell.flavor_id,
                                           block_device_mapping_v2=[dev_mapping_2,],
                                           nics=[{'net-id': shell.network_id}],
                                           key_name=keypair_name,
                                           scheduler_hints={"group": group_id},
                                           **create_kwargs)
    except Exception as e:
        raise e

    return server


def call_until_true(func, duration, sleep_for):
    now = time.time()
    timeout = now + duration
    while now < timeout:
        if func():
            return True
        time.sleep(sleep_for)
        now = time.time()
    return False


def print_list(objs, fields, formatters={}):
    pt = prettytable.PrettyTable([f for f in fields], caching=False)
    pt.align = 'l'

    for o in objs:
        row = []
        for field in fields:
            if field in formatters:
                row.append(formatters[field](o))
            else:
                field_name = field.lower().replace(' ', '_')
                if type(o) == dict and field in o:
                    data = o[field_name]
                else:
                    data = getattr(o, field_name, None) or ''
                row.append(data)
        pt.add_row(row)

    print(encodeutils.safe_encode(pt.get_string()))



if __name__ == '__main__':
    prepare_log()

    try:
        CatalystCloudShell().main(sys.argv[1:])
    except KeyboardInterrupt:
        print("Terminating...")
        sys.exit(1)
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback,
                                  limit=2, file=sys.stdout)
