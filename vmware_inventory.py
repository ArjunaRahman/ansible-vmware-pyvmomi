#!/usr/bin/env python

# TODO:
#   * unit tests
#   * docstrings in all functions
#   * more jq examples
#   * optional folder heirarchy 

"""
$ jq '._meta.hostvars[].config' data.json | head
{
  "alternateguestname": "",
  "instanceuuid": "5035a5cd-b8e8-d717-e133-2d383eb0d675",
  "memoryhotaddenabled": false,
  "guestfullname": "Red Hat Enterprise Linux 7 (64-bit)",
  "changeversion": "2016-05-16T18:43:14.977925Z",
  "uuid": "4235fc97-5ddb-7a17-193b-9a3ac97dc7b4",
  "cpuhotremoveenabled": false,
  "vpmcenabled": false,
  "firmware": "bios",
"""

from __future__ import print_function

import argparse
import atexit
import getpass
import jinja2
import os
import six
import ssl
from time import time
import uuid

from collections import defaultdict
from pyVim.connect import SmartConnect, Disconnect
from six.moves import configparser
from time import time

try:
    import json
except ImportError:
    import simplejson as json


class VMWareInventory(object):

    __name__ = 'VMWareInventory'

    maxlevel = 1
    lowerkeys = True
    config = None
    cache_max_age = None
    cache_path_cache = None
    cache_path_index = None
    server = None
    port = None
    username = None
    password = None
    host_filters = []
    groupby_patterns = []


    def _empty_inventory(self):
        return {"_meta" : {"hostvars" : {}}}


    def __init__(self):
        self.inventory = self._empty_inventory()

        # Read settings and parse CLI arguments
        self.parse_cli_args()
        self.read_settings()

        # Check the cache
        cache_valid = self.is_cache_valid()

        # Handle Cache
        if self.args.refresh_cache or not cache_valid:
            self.do_api_calls_update_cache()
        else:
            self.inventory = self.get_inventory_from_cache()

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info(self.args.host)
        elif self.args.list:
            # Display list of instances for inventory
            data_to_print = self.inventory
        print(json.dumps(data_to_print, indent=2))


    def is_cache_valid(self):

        ''' Determines if the cache files have expired, or if it is still valid '''

        valid = False

        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                valid = True

        return valid


    def do_api_calls_update_cache(self):

        ''' Get instances and cache the data '''

        instances = self.get_instances()
	self.inventory = self.instances_to_inventory(instances)
        self.write_to_cache(self.inventory, self.cache_path_cache)


    def write_to_cache(self, data, cache_path):

        ''' Dump inventory to json file '''

        with open(self.cache_path_cache, 'wb') as f:
            f.write(json.dumps(data))


    def get_inventory_from_cache(self):

        ''' Read in jsonified inventory '''

        jdata = None
        with open(self.cache_path_cache, 'rb') as f:
            jdata = f.read()
        return json.loads(jdata)


    def read_settings(self):
        ''' Reads the settings from the vmware_inventory.ini file '''

	defaults = {'vmware': {
			'server': '',
			'port': 443,
			'username': '',
			'password': '',
			'ini_path': os.path.join(os.path.dirname(os.path.realpath(__file__)), 'vmware_inventory.ini'),
			'cache_name': 'ansible-vmware',
			'cache_path': '~/.ansible/tmp',
			'cache_max_age': 300,
                        'max_object_level': 0,
                        'alias_pattern': '{{ config.name + "_" + config.uuid }}',
                        'host_pattern': '{{ guest.ipaddress }}',
                        'host_filters': '{{ guest.gueststate == "running" }}',
                        'groupby_patterns': '{{ guest.guestid }},{{ "template" if config.template else "not_template"}}',
                        'lower_var_keys': True }
		   }

        if six.PY3:
            config = configparser.ConfigParser()
        else:
            config = configparser.SafeConfigParser()

        vmware_ini_path = os.environ.get('VMWARE_INI_PATH', defaults['vmware']['ini_path'])
        vmware_ini_path = os.path.expanduser(os.path.expandvars(vmware_ini_path))
        config.read(vmware_ini_path)

	# apply defaults
	for k,v in defaults['vmware'].iteritems():
	    if not config.has_option('vmware', k):
                config.set('vmware', k, str(v))

        self.cache_dir = os.path.expanduser(config.get('vmware', 'cache_path'))
        if self.cache_dir and not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

	cache_name = config.get('vmware', 'cache_name')
        self.cache_path_cache = self.cache_dir + "/%s.cache" % cache_name
        self.cache_path_index = self.cache_dir + "/%s.index" % cache_name
        self.cache_max_age = int(config.getint('vmware', 'cache_max_age'))

	# mark the connection info 
        self.server =  os.environ.get('VMWARE_SERVER', config.get('vmware', 'server'))
        self.port = int(os.environ.get('VMWARE_PORT', config.get('vmware', 'port')))
        self.username = os.environ.get('VMWARE_USERNAME', config.get('vmware', 'username'))
        self.password = os.environ.get('VMWARE_PASSWORD', config.get('vmware', 'password'))

	# behavior control
	self.maxlevel = int(config.get('vmware', 'max_object_level'))
    	self.lowerkeys = bool(config.get('vmware', 'lower_var_keys'))
        self.host_filters = list(config.get('vmware', 'host_filters').split(','))
        self.groupby_patterns = list(config.get('vmware', 'groupby_patterns').split(','))

        self.config = config    


    def parse_cli_args(self):
        ''' Command line argument processing '''

        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on PyVmomi')
        parser.add_argument('--list', action='store_true', default=True,
                           help='List instances (default: True)')
        parser.add_argument('--host', action='store',
                           help='Get all the variables about a specific instance')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                           help='Force refresh of cache by making API requests to VSphere (default: False - use cache files)')
        self.args = parser.parse_args()


    def get_instances(self):
        instances = []        
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
        context.verify_mode = ssl.CERT_NONE
        si = SmartConnect(host=self.server,
                         user=self.username,
                         pwd=self.password,
                         port=int(self.port),
                         sslContext=context)
        if not si:
            print("Could not connect to the specified host using specified "
                "username and password")
            return -1

        atexit.register(Disconnect, si)

        content = si.RetrieveContent()
        for child in content.rootFolder.childEntity:
            if hasattr(child, 'vmFolder'):
                datacenter = child
                vmFolder = datacenter.vmFolder
                vmList = vmFolder.childEntity
                for vm in vmList:
                    if hasattr(vm, 'childEntity'):
                        vmList = vm.childEntity
                        for c in vmList:
                            instances.append(c)
                    else:
                        instances.append(vm)
        return instances


    def instances_to_inventory(self, instances):

        ''' Convert a list of vm objects into a json compliant inventory '''

	inventory = self._empty_inventory()
        inventory['all'] = {}
        inventory['all']['hosts'] = []
        last_idata = None
        for instance in instances:
    
            # make a unique id for this object to avoid vmware's
            # numerous uuid's which aren't all unique.
            thisid = str(uuid.uuid4())
            idata = {}

            # Get all known info about this instance
            idata = self.facts_from_vobj(instance)

            # Put it in the inventory
            inventory['all']['hosts'].append(thisid)
            inventory['_meta']['hostvars'][thisid] = idata.copy()
            inventory['_meta']['hostvars'][thisid]['ansible_uuid'] = thisid

        # Make a map of the uuid to the name the user wants
        name_mapping = self.create_template_mapping(inventory, 
                            self.config.get('vmware', 'alias_pattern'))

        host_mapping = self.create_template_mapping(inventory,
                            self.config.get('vmware', 'host_pattern'))

        # Reset the inventory keys
        for k,v in name_mapping.iteritems():

            # set ansible_host (2.x)
            inventory['_meta']['hostvars'][k]['ansible_host'] = host_mapping[k]
            # 1.9.x backwards compliance
            inventory['_meta']['hostvars'][k]['ansible_ssh_host'] = host_mapping[k]

            if k == v:
                continue

            # add new key
            inventory['all']['hosts'].append(v)
            inventory['_meta']['hostvars'][v] = inventory['_meta']['hostvars'][k]

            # cleanup old key
            inventory['all']['hosts'].remove(k)
            inventory['_meta']['hostvars'].pop(k, None)

        # Apply host filters
        for hf in self.host_filters:
            if not hf:
                continue
            filter_map = self.create_template_mapping(inventory, hf, dtype='boolean')
            for k,v in filter_map.iteritems():
                if not v:
                    # delete this host
                    inventory['all']['hosts'].remove(k)
                    inventory['_meta']['hostvars'].pop(k, None)

        # Create groups
        for gbp in self.groupby_patterns:
            print(gbp)
            groupby_map = self.create_template_mapping(inventory, gbp)
            for k,v in groupby_map.iteritems():
                if v not in inventory:
                    inventory[v] = {}
                    inventory[v]['hosts'] = []
                if k not in inventory[v]['hosts']:
                    inventory[v]['hosts'].append(k)    

	return inventory


    def create_template_mapping(self, inventory, pattern, dtype='string'):

        ''' Return a hash of uuid to templated string from pattern '''

        mapping = {}
        for k,v in inventory['_meta']['hostvars'].iteritems():
            t = jinja2.Template(pattern)
            newkey = t.render(v)
            newkey = newkey.strip()
            if dtype == 'integer':
                newkey = int(newkey)
            elif dtype == 'boolean':
                if newkey.lower() == 'false':
                    newkey = False
                elif newkey.lower() == 'true':
                    newkey = True    
            elif dtype == 'string':
                pass        
            mapping[k] = newkey
        return mapping


    def facts_from_vobj(self, vobj, level=0):

        ''' Traverse a VM object and return a json compliant data structure '''

        rdata = {}

        if level > self.maxlevel:
            return rdata

        bad_types = ['Array']
        safe_types = [int, bool, str, float]
        iter_types = [dict, list]

        methods = dir(vobj)
        methods = [str(x) for x in methods if not x.startswith('_')]
        methods = [x for x in methods if not x in bad_types]

        for method in methods:

            if method in rdata:
                continue

            # Attempt to get the method, skip on fail
            try:
                methodToCall = getattr(vobj, method)
            except Exception as e:
                continue

            if self.lowerkeys:
                method = method.lower()


            # Store if type is a primitive
            if type(methodToCall) in safe_types:
                try:
                    rdata[method] = methodToCall
                except Exception as e:
                    print(e)
                    import epdb; epdb.st()

            # Objects usually have a dict property
            elif hasattr(methodToCall, '__dict__'):

                # the dicts will have objects for values, get rid of those
                safe_dict = {}

                for k,v in methodToCall.__dict__.iteritems():

                    # Try not to recurse into self
                    if hasattr(v, '__name__'):
                        if v.__name__ == 'VMWareInventory':
                            continue                     

                    # Skip private methods
                    if k.startswith('_'):
                        continue

                    if self.lowerkeys:
                        k = k.lower()

                    if type(v) in safe_types:
                        safe_dict[k] = v    
                    elif not v:
                        pass    
                    elif type(v) in iter_types:
                        pass
                    else:

                        # Recurse through this method to get deeper data
                        if level < self.maxlevel:
                            vdata = None
                            vdata = self.facts_from_vobj(methodToCall, 
                                                        level=(level+1))

                            for vk, vv in vdata.iteritems():
                                if self.lowerkeys:
                                    vk = vk.lower()
                                safe_dict[vk] = vv
                if safe_dict:        
                    try:
                        rdata[method] = safe_dict
                    except Exception as e:
                        print(e)
                        import epdb; epdb.st()
                        pass

        return rdata

    def get_host_info(self, host):
        return self.inventory['_meta']['hostvars'][host]


# Run the script
VMWareInventory()


