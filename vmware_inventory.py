#!/usr/bin/env python

# TODO:
#   * unit tests
#   * docstrings in all functions
#   * more jq examples
#   * optional folder heirarchy 
#   * find tag attributes and make groupby example

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
import datetime
import getpass
import jinja2
import os
import six
import ssl
from time import time
import uuid

from collections import defaultdict
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from six.moves import configparser
from time import time


try:
    import json
except ImportError:
    import simplejson as json

hasvcr = False
try:
    import vcr
    hasvcr = True
except ImportError:
    pass


class VMWareInventory(object):

    __name__ = 'VMWareInventory'

    debug = False
    load_dumpfile = None
    write_dumpfile = None
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

    bad_types = ['Array']
    safe_types = [int, long, bool, str, float, None]
    iter_types = [dict, list]
    skip_keys = ['dynamicproperty', 'dynamictype', 'managedby', 'childtype']


    def _empty_inventory(self):
        return {"_meta" : {"hostvars" : {}}}


    def __init__(self, load=True):
        self.inventory = self._empty_inventory()

        if load:
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

    def debugl(self, text):
        if self.args.debug:
            print(text)

    def show(self):
        # Data to print
        data_to_print = None
        if self.args.host:
            data_to_print = self.get_host_info(self.args.host)
        elif self.args.list:
            # Display list of instances for inventory
            data_to_print = self.inventory
        return json.dumps(data_to_print, indent=2)


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
	self.instances = instances
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

        scriptbasename = os.path.realpath(__file__)
        scriptbasename = os.path.basename(scriptbasename)
        scriptbasename = scriptbasename.replace('.py', '')

	defaults = {'vmware': {
			'server': '',
			'port': 443,
			'username': '',
			'password': '',
			'ini_path': os.path.join(os.path.dirname(os.path.realpath(__file__)), '%s.ini' % scriptbasename),
			'cache_name': 'ansible-vmware',
			'cache_path': '~/.ansible/tmp',
			'cache_max_age': 3600,
                        'max_object_level': 0,
                        'alias_pattern': '{{ config.name + "_" + config.uuid }}',
                        'host_pattern': '{{ guest.ipaddress }}',
                        'host_filters': '{{ guest.gueststate == "running" }}',
                        'groupby_patterns': '{{ guest.guestid }},{{ "templates" if config.template else "guests"}}',
                        'lower_var_keys': True }
		   }

        if six.PY3:
            config = configparser.ConfigParser()
        else:
            config = configparser.SafeConfigParser()

        # where is the config?
        vmware_ini_path = os.environ.get('VMWARE_INI_PATH', defaults['vmware']['ini_path'])
        vmware_ini_path = os.path.expanduser(os.path.expandvars(vmware_ini_path))
        config.read(vmware_ini_path)

	# apply defaults
	for k,v in defaults['vmware'].iteritems():
	    if not config.has_option('vmware', k):
                config.set('vmware', k, str(v))

        # where is the cache?
        self.cache_dir = os.path.expanduser(config.get('vmware', 'cache_path'))
        if self.cache_dir and not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

        # set the cache filename and max age
	cache_name = config.get('vmware', 'cache_name')
        self.cache_path_cache = self.cache_dir + "/%s.cache" % cache_name
        self.cache_max_age = int(config.getint('vmware', 'cache_max_age'))

	# mark the connection info 
        self.server =  os.environ.get('VMWARE_SERVER', config.get('vmware', 'server'))
        self.port = int(os.environ.get('VMWARE_PORT', config.get('vmware', 'port')))
        self.username = os.environ.get('VMWARE_USERNAME', config.get('vmware', 'username'))
        self.password = os.environ.get('VMWARE_PASSWORD', config.get('vmware', 'password'))

	# behavior control
	self.maxlevel = int(config.get('vmware', 'max_object_level'))
    	self.lowerkeys = config.get('vmware', 'lower_var_keys')
        if type(self.lowerkeys) != bool:
            if str(self.lowerkeys).lower() in ['yes', 'true', '1']:
                self.lowerkeys = True
            else:    
                self.lowerkeys = False

        self.host_filters = list(config.get('vmware', 'host_filters').split(','))
        self.groupby_patterns = list(config.get('vmware', 'groupby_patterns').split(','))

        # save the config
        self.config = config    


    def parse_cli_args(self):

        ''' Command line argument processing '''

        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on PyVmomi')
        parser.add_argument('--debug', action='store_true', default=False,
                           help='show debug info')
        parser.add_argument('--usevcr', action='store_true', default=None,
                           help='use python-vcr to store pysphere data to yaml (for troubleshooting)')
        parser.add_argument('--list', action='store_true', default=True,
                           help='List instances (default: True)')
        parser.add_argument('--host', action='store',
                           help='Get all the variables about a specific instance')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                           help='Force refresh of cache by making API requests to VSphere (default: False - use cache files)')
        self.args = parser.parse_args()


    def get_instances(self):

        ''' Get a list of vm instances with pyvmomi '''

        instances = []        

	kwargs = {'host': self.server,
                  'user': self.username,
                  'pwd': self.password,
                  'port': int(self.port) }

	if hasattr(ssl, 'SSLContext'):
		# older ssl libs do not have an SSLContext method:
    		# 	context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
		# 	AttributeError: 'module' object has no attribute 'SSLContext'
        	context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
        	context.verify_mode = ssl.CERT_NONE
		kwargs['sslContext'] = context

	if self.args.usevcr and not os.path.isdir('fixtures'):
	    os.makedirs('fixtures')

	if self.args.usevcr and hasvcr and os.path.isfile('fixtures/get_instances.yaml'):
	    self.debugl("### RUNNING IN VCR PLAY MODE")
            instances = self._get_instances_with_vcr_play(kwargs)
	elif self.args.usevcr and hasvcr and not os.path.isfile('fixtures/get_instances.yaml'):
	    self.debugl("### RUNNING IN VCR RECORD MODE")
            instances = self._get_instances_with_vcr_record(kwargs)
	else:
	    self.debugl("### RUNNING WITHOUT VCR")
            instances = self._get_instances(kwargs)

	return instances


    def _get_instances(self, inkwargs):

	instances = []

        si = SmartConnect(**inkwargs)

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


    @vcr.use_cassette('get_instances.yaml',
                      cassette_library_dir='fixtures',
                      record_mode='once')
    def _get_instances_with_vcr_record(self, kwargs):

	instances = []

        si = SmartConnect(**kwargs)

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

	# in record mode, we need to make all possible api calls
	for instance in instances:
            self.facts_from_vobj(instance)

        return instances


    @vcr.use_cassette('get_instances.yaml',
                      cassette_library_dir='fixtures',
                      record_mode='never')
    def _get_instances_with_vcr_play(self, kwargs):

	instances = []

        si = SmartConnect(**kwargs)

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

            # Get all known info about this instance
            idata = {}
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

        # pyvmomi objects are not yet serializable, but may be one day ...
        # https://github.com/vmware/pyvmomi/issues/21

        rdata = {}

	# Do not serialize self
        if hasattr(vobj, '__name__'):
            if vobj.__name__ == 'VMWareInventory':
                return rdata

	# Exit early if maxlevel is reached
        if level > self.maxlevel:
            return rdata

        # Objects usually have a dict property
        if hasattr(vobj, '__dict__') and not level == 0:

            for k,v in vobj.__dict__.iteritems():

                # Skip private methods
                if k.startswith('_'):
                    continue

                if k.lower() in self.skip_keys:
                    continue

                if self.lowerkeys:
                    k = k.lower()

		rdata[k] = self._process_object_types(v, level=level)

        else:    

            methods = dir(vobj)
            methods = [str(x) for x in methods if not x.startswith('_')]
            methods = [x for x in methods if not x in self.bad_types]

            for method in methods:

                if method in rdata:
                    continue

                # Attempt to get the method, skip on fail
                try:
                    methodToCall = getattr(vobj, method)
                except Exception as e:
                    continue

                # Skip callable methods
                if callable(methodToCall):
                    continue

                if self.lowerkeys:
                    method = method.lower()

		rdata[method] = self._process_object_types(methodToCall, level=level)

        return rdata


    def _process_object_types(self, vobj, level=0):

	#import pprint; pprint.pprint(vobj)

        rdata = {}

	vstr = None
	if type(vobj) not in self.safe_types:
	    try:
		vstr = str(methodToCall)
	    except Exception as e:
		pass

        if type(vobj) in self.safe_types:
            try:
                rdata = vobj
            except Exception as e:
		    print(e)
		    import epdb; epdb.st()

        elif hasattr(vobj, 'append'):
            rdata = []
            for vi in vobj:
                if type(vi) in self.safe_types:
                    rdata.append(vi)
                else:
		    if (level+1 <= self.maxlevel):
			vid = self.facts_from_vobj(vi, level=(level+1))
			if vid:
			    rdata.append(vid)

        elif hasattr(vobj, '__dict__'):
	    if (level+1 <= self.maxlevel):
		md = None
		md = self.facts_from_vobj(vobj, level=(level+1))
		if md:
		    rdata = md
        elif not vobj or type(vobj) in self.safe_types:
            rdata = vobj
        elif type(vobj) == datetime.datetime:
            rdata = str(vobj)
        else:
            print("WHAT!")
            import epdb; epdb.st()
        #import epdb; epdb.st()    
	if not rdata:
		rdata = None
        return rdata


    def get_host_info(self, host):
        
        ''' Return hostvars for a single host '''

        return self.inventory['_meta']['hostvars'][host]

if __name__ == "__main__":
    # Run the script
    print(VMWareInventory().show())


