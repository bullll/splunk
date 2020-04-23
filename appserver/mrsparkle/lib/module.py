from __future__ import absolute_import
from functools import cmp_to_key
from builtins import object
import logging
import os
import sys
import splunk.appserver.mrsparkle.controllers.module as controllersmodule
import splunk.appserver.mrsparkle.lib.apps as apps
import splunk.appserver.mrsparkle.lib.util as util
import splunk.util
import splunk.clilib.cli_common as comm


from threading import Lock


logger = logging.getLogger('splunk.appserver.lib.module')

MASTER_EXTENSION = '.js'
MANIFEST_EXTENSION = '.conf'

UNDEFINED_MODULE_ASSERTION_ERROR = "%s is not a defined module classname. This is probably either a typo in the first line of a module's js file, or an old out-of-date module file hanging around in the modules directory."

def do_lock(mutex):
    def wrap(f):
        def helper(*a, **kw):
            with mutex:
                return f(*a, **kw)
        return helper
    return wrap

def assert_locked(mutex):
    def wrap(f):
        def helper(*a, **kw):
            if not mutex.locked():
                raise Exception(_('moduleDefinitionsMutex not held!'))
            return f(*a, **kw)
        return helper
    return wrap

class ModuleMapperException(Exception):
    pass

class ModuleLookup(object):
    """
    A simple utility to centralize the lookup/load ordering of a module existing
    in 1-many paths.
    """
    def __init__(self):
        self.modules = {}

    def add(self, path, class_name):
        self.modules.setdefault(class_name, [])
        concat_paths = self.modules[class_name] + [path]
        self.modules[class_name] = sorted(concat_paths, key=cmp_to_key(self.sort_by_path))
        return self.modules[class_name]

    def is_primary(self, path, class_name):
        return self.get_primary(class_name)==path

    def get_primary(self, class_name):
        if class_name not in self.modules:
            return None
        return self.modules[class_name][0]

    def sort_by_path(self, x, y):
        if 'search_mrsparkle' in x and 'search_mrsparkle' in y:
            return 1 if x > y else -1
        if 'search_mrsparkle' in x:
            return -1
        if 'search_mrsparkle' in y:
            return 1
        return 1 if x > y else -1

class ModuleMapper(object):

    # Class-level lock that is equivalent to a member variable because this
    # class is used as a singleton
    moduleDefinitionsMutex = Lock()

    def __init__(self):
        self.installedModules = None
        self.installedModules = self.getInstalledModules()

    @assert_locked(moduleDefinitionsMutex)
    def getModuleList(self, root, use_lookup_heuristic=True):
        '''
        Generates a list of module detail by crawling the entire modules directory
        for JS files.
        '''
        moduleList = []
        moduleHash = {}
        module_lookup = ModuleLookup()
        _duplicateDefender = set()
        _seen_filenames = set()

        def scan_file(dirpath, name):
            if name.endswith(MANIFEST_EXTENSION) and os.path.exists(os.path.join(dirpath, name[:-len(MANIFEST_EXTENSION)] + MASTER_EXTENSION)):
                cfg_pathname = os.path.join(dirpath, name)
                if cfg_pathname in _seen_filenames:
                    return
                _seen_filenames.add(cfg_pathname)

                config = comm.readConfFile(cfg_pathname)

                def lower_keys(x):
                    if isinstance(x, dict):
                        return dict((k.lower(), lower_keys(v)) for k, v in x.items())
                    return x

                '''
                ConfigParser supports case-insensitive keys, since we've moved off it
                (because it's not BOM-aware) we must also, but the section names
                (e.g. 'param:myParam') must preserve case
                '''
                for k in config:
                    config[k] = lower_keys(config[k])



                # assert that conf file has base classes defined
                try:
                    className = config['module']['classname']
                    superClass = config['module']['superclass'] if 'superclass' in config['module'] else None
                except KeyError:
                    logger.warn("Manifest file %s does not contain a valid module section" % cfg_pathname)
                    return

                if className == superClass:
                    raise ModuleMapperException('%s defines className == superClass !!' % cfg_pathname)


                # assemble the parameter configuration info

                params = {}
                stickyParams = []
                persistableParams = []
                for section_name in config:
                    section = config[section_name]

                    if section_name.startswith('param:'):
                        pname = section_name[6:].strip()
                        if 'default' in section and 'required' in section and splunk.util.normalizeBoolean(section['required']):
                            raise ModuleMapperException(
                                'Cannot use required=True with a default value in Manifest file %s, parameter %s'
                                % (cfg_pathname, pname))

                        params[pname] = {
                            'required': splunk.util.normalizeBoolean(section['required']) if 'required' in section else False,
                            'default': section['default'] if 'default' in section else None,
                            'values' : [val.strip() for val in section['values'].split(',')] if 'values' in section else None,
                            'label': section['label'] if 'label' in section else None,
                            'translate': section['translate'] if 'translate' in section else None
                        }

                        # add params to persistence lists
                        if 'sticky' in section and splunk.util.normalizeBoolean(section['sticky']):
                            stickyParams.append(pname)
                        if 'persistable' in section and splunk.util.normalizeBoolean(section['persistable']):
                            persistableParams.append(pname)


                # module loading

                if use_lookup_heuristic:
                    module_lookup.add(dirpath, className)
                    if not module_lookup.is_primary(dirpath, className):
                        logger.error("module %s version in %s trumped by %s" % (className, dirpath, module_lookup.get_primary(className)))
                        return
                else:
                    if (className in _duplicateDefender):
                        logger.error("ASSERT - duplicate definition of %s in %s/%s" % (className, dirpath, name))
                        return
                    _duplicateDefender.add(className)


                # enable support for including other module configuration

                if 'include' in config['module']:
                    include = [ mod_name.strip() for mod_name in config['module']['include'].split(',') ]
                else:
                    include = []

                if 'description' in config['module']:
                    description = config['module']['description']
                else:
                    description = None


                # assemble final module definition dict

                mod = {
                    'class': className,
                    'appName': '',
                    'superClass': superClass,
                    'path': dirpath,
                    'filePrefix': name[:-len(MANIFEST_EXTENSION)],
                    'params': params,
                    'stickyParams': stickyParams,
                    'persistableParams': persistableParams,
                    'include': include,
                    'description': description
                }
                moduleList.append(mod)
                moduleHash[className] = mod


        # generate a dict of all the accessible Splunk.Modules
        for dirpath, subdirs, filenames in os.walk(root):
            for name in filenames:
                scan_file(dirpath, name)


        # Scan installed applications for modules that they might define
        for (app_name, module_name, module_path) in apps.local_apps.getAllModules():
            for filename in os.listdir(module_path):
                if filename.endswith('.py'):
                    # load module's python handlers
                    modname = filename[:-3]
                    if modname not in sys.modules:
                        sys.path.insert(0, module_path)
                        try:
                            mod = __import__(modname)
                        except ValueError as e:
                            logger.warning("Error importing module: %s. Exception: %s" % (modname, e))
                            continue

                        # find all the module classes that subclass module.ModuleHandler()
                        for c in util.get_module_classes(mod):
                            # push module into controllers.module namespace..
                            # not sure i really like this, but does keep it super easy for module writers
                            if issubclass(c, controllersmodule.ModuleHandler):
                                logger.debug('module loader - loading python module handler: %s' % c.__name__)
                                # XXX Normalize this asap!
                                try:
                                    setattr(controllersmodule, c.__name__, c)
                                except AttributeError:
                                    pass
                                setattr(controllersmodule, c.__name__, c)
                        del sys.path[0]
                else:
                    scan_file(module_path, filename)

        # modules inherit their parameters from their superClass
        processed = set()
        def update_params(mod):
            if mod['class'] in processed:
                return
            processed.add(mod['class'])
            if not (mod['superClass'] and mod['superClass'] in moduleHash):
                return
            supermod = moduleHash[mod['superClass']]
            if mod['superClass'] not in processed:
                update_params(supermod)
            for pname, param in supermod['params'].items():
                if pname not in mod['params']:
                    mod['params'][pname] = param.copy()
        for mod in moduleList:
            update_params(mod)

        if len(moduleList) == 0:
            logger.warn('getModuleList - did not find any modules to load from: %s' % root)

        return moduleList


    @assert_locked(moduleDefinitionsMutex)
    def sortModuleList(self, moduleList):
        '''
        Sorts a module list (in-place) according to inheritance order
        '''

        # create link list
        links = dict([(x['class'], x['superClass']) for x in moduleList])

        def getNumberOfGenerations(name, count=0):
            # check for simple but common configuration error, for instance where
            # an old version of a module file refers to a nonexistent class.
            if (name not in links):
                logger.warn(UNDEFINED_MODULE_ASSERTION_ERROR % name)
                return -1

            superClass = links[name]

            if superClass == None: return count
            return getNumberOfGenerations(superClass, count + 1)

        # count the number of parents until a class hits top
        numberOfGenerationsMap = {}

        for item in moduleList:
            numberOfGenerationsMap[item['class']] = getNumberOfGenerations(item['class'])

        # remove the ones that were flagged earlier as having -1 ancestral generations.
        startingLen = len(moduleList) - 1
        for i, x in enumerate(reversed(moduleList)):
            if numberOfGenerationsMap.get(x['class'], 0) < 0:
                moduleList.pop(startingLen - i)

        return sorted(moduleList, key = lambda e: numberOfGenerationsMap[e['class']])
        
    @assert_locked(moduleDefinitionsMutex)
    def updateModuleAssets(self, moduleList):
        '''
        Updates a module list (in-place) with asset flags.  Any JS file with
        matching files will result in a new key being added to the module list.
        Ex: if a matching HTML file is found, a 'html': True key will be added.
        '''

        for module in moduleList:
            for name in os.listdir(module['path']):
                if name.startswith(module['filePrefix'] + '.'):
                    parts = name.split('.')
                    if len(parts) > 1:
                        module[parts[-1]] = os.path.join(module['path'], module['filePrefix'] + '.' + parts[-1])

    @assert_locked(moduleDefinitionsMutex)
    def updateModuleAppName(self, moduleList):
        """
        Look at the modulelist's path attribute to get what application it came from.
        """
        homePath = os.environ["SPLUNK_HOME"]

        for module in moduleList:
            appnameShard = module['path'].split(os.sep).index('modules')-2
            appname = module['path'].split(os.sep)[appnameShard]
            if appname == 'splunk':
                module['appName'] = splunk.getDefault('namespace')
            else:
                module['appName'] = appname

            # hide the real home path behind a literal string
            module['path'] = module['path'].replace(homePath, '$SPLUNK_HOME')

    @assert_locked(moduleDefinitionsMutex)
    def updateModuleInheritance(self, moduleList):
        '''
        Updates a module list (in-place) with the chain of inheritance as the
        'inheritance' property
        '''

        # generate an ordered dict version of the list for easy access
        workingList = splunk.util.OrderedDict()
        for x in moduleList:
            workingList[x['class']] = x.copy()

        # flag all of the modules that are active in this view
        def mwalk(name):
            if name: workingList[name]['isActive'] = True
            else: return
            mwalk(workingList[name]['superClass'])

        # inspect each module and generate its inheritance chain
        for moduleName in workingList:

            # reset
            for x in workingList:
                workingList[x]['isActive'] = False

            mwalk(workingList[moduleName]['class'])

            workingList[moduleName]['inheritance'] = []
            for x in workingList:
                if workingList[x]['isActive']:
                    workingList[moduleName]['inheritance'].append(x)

            for x in moduleList:
                x['inheritance'] = workingList[x['class']].get('inheritance', [])


    def extractClassName(self, jsFilePath) :
        """
        Extracts from the first line of the js file, the classname and any
        parent class relationships.
        """

        className = None
        superClass = None
        handle = open(jsFilePath, 'r')
        try :
            for line in handle:
                firstLine = line.strip('/\r\n').split(" extends ")
                className   = firstLine[0].strip()
                if (len(firstLine) > 1) :
                    superClass = firstLine[1].strip()
                break
        except Exception as e:
            logger.exception(e)
        finally:
            if handle: handle.close()

        return className, superClass


    def resetInstalledModules(self):
        '''
        Clears the cache of installed modules
        '''
        apps.local_apps.refresh(True)
        self.getInstalledModules(True)
        logger.info("Modules are all refreshed")

    @do_lock(moduleDefinitionsMutex)
    def getInstalledModules(self, force=False):
        '''
        Returns a list of module information that will be used
        to construct the various view templates.
        Top-level keys are module names like "Splunk.Module.Topnav".
        and those top level values are each their own dictionary.

        Within those second level dictionares, the most important keys are
        'path', and 'fileprefix', giving clients the path to the files.
        -- and then individual keys like 'html','css','js', that are
        bools, each indicating whether the module specifies a file
        for that specific resource.

        Example:

        {
            'Splunk.Module.FlashTimeline': {
                'filePrefix': 'flash_timeline',
                'inheritance': [
                    'Splunk.Module',
                    'Splunk.Module.SearchModule',
                    'Splunk.Module.FlashWrapper',
                    'Splunk.Module.FlashTimeline'
                ],
                'js': '/Users/johnvey/build/current/share/splunk/search_mrsparkle/modules/flash_timeline/flash_timeline.js',
                'html': '/Users/johnvey/build/current/share/splunk/search_mrsparkle/modules/flash_timeline/flash_timeline.html',
                'superClass': 'Splunk.Module.FlashWrapper',
                'path': '/Users/johnvey/build/current/share/splunk/search_mrsparkle/modules/flash_timeline',
                'class': 'Splunk.Module.FlashTimeline',
                'css': '/Users/johnvey/build/current/share/splunk/search_mrsparkle/modules/flash_timeline/flash_timeline.css'
            }

            ...

        }
        '''

        if self.installedModules is not None and not force:
            return self.installedModules

        # TODO: pull out path into global config area
        # read in the system modules
        root = util.make_absolute("share/splunk/search_mrsparkle/modules")

        mods = self.getModuleList(root)
        mods = self.sortModuleList(mods)
        self.updateModuleAssets(mods)
        self.updateModuleInheritance(mods)
        self.updateModuleAppName(mods)

        # generate an ordered dict version of the list for easy access
        output = splunk.util.OrderedDict()
        for x in mods:
            output[x['class']] = x

        # memoize the list of installed modules
        self.installedModules = output

        return output

### make ModuleMapper a singleton
moduleMapper = ModuleMapper()

if __name__ == '__main__':

    import unittest

    class ModuleLookupTest(unittest.TestCase):

        def testSystemAndApp(self):
            lookups = [
                '/home/docyes/code/splunk/opt/ace/etc/apps/launcher/appserver/modules/mystuff',
                '/home/docyes/code/splunk/opt/ace/share/splunk/search_mrsparkle/modules/results',
                '/home/docyes/code/splunk/opt/ace/etc/apps/search/appserver/modules/crap'
            ]
            class_name = 'Splunk.Module.EventsViewer'
            module_lookup = ModuleLookup()
            module_lookup.add(lookups[0], class_name)
            self.assertTrue(module_lookup.is_primary(lookups[0], class_name))
            module_lookup.add(lookups[1], class_name)
            self.assertTrue(module_lookup.is_primary(lookups[1], class_name))
            module_lookup.add(lookups[2], class_name)
            self.assertFalse(module_lookup.is_primary(lookups[2], class_name))
            self.assertTrue(module_lookup.is_primary(lookups[1], class_name))

        def testAppOnly(self):
            lookups = [
                '/home/docyes/code/splunk/opt/ace/etc/apps/zoltar/appserver/modules/futures',
                '/home/docyes/code/splunk/opt/ace/etc/apps/launcher/appserver/modules/mystuff',
                '/home/docyes/code/splunk/opt/ace/etc/apps/search/appserver/modules/crap'
            ]
            class_name = 'Splunk.Module.EventsViewer'
            module_lookup = ModuleLookup()
            module_lookup.add(lookups[0], class_name)
            self.assertTrue(module_lookup.is_primary(lookups[0], class_name))
            module_lookup.add(lookups[1], class_name)
            self.assertTrue(module_lookup.is_primary(lookups[1], class_name))
            module_lookup.add(lookups[2], class_name)
            self.assertFalse(module_lookup.is_primary(lookups[2], class_name))
            self.assertTrue(module_lookup.is_primary(lookups[1], class_name))

        def testSystemOnly(self):
            lookups = [
                '/home/docyes/code/splunk/opt/ace/share/splunk/search_mrsparkle/modules/zoltar',
                '/home/docyes/code/splunk/opt/ace/share/splunk/search_mrsparkle/modules/bertrand',
                '/home/docyes/code/splunk/opt/ace/share/splunk/search_mrsparkle/modules/results',
            ]
            class_name = 'Splunk.Module.EventsViewer'
            module_lookup = ModuleLookup()
            module_lookup.add(lookups[0], class_name)
            self.assertTrue(module_lookup.is_primary(lookups[0], class_name))
            module_lookup.add(lookups[1], class_name)
            self.assertTrue(module_lookup.is_primary(lookups[1], class_name))
            module_lookup.add(lookups[2], class_name)
            self.assertFalse(module_lookup.is_primary(lookups[2], class_name))
            self.assertTrue(module_lookup.is_primary(lookups[1], class_name))

    loader = unittest.TestLoader()
    suites = []
    suites.append(loader.loadTestsFromTestCase(ModuleLookupTest))
    unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(suites))
