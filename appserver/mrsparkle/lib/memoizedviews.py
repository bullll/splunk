import copy
from builtins import map
from builtins import object
import logging
from threading import Lock
import time

import splunk.util
import splunk.entity as en
import splunk.appserver.mrsparkle.lib.i18n as i18n
import splunk.appserver.mrsparkle.lib.message as message
import splunk.appserver.mrsparkle.lib.module as module
import splunk.appserver.mrsparkle.lib.util as util
import splunk.appserver.mrsparkle.lib.viewconf as viewconf
import cherrypy

logger = logging.getLogger('splunk.appserver.lib.memoizedviews')

# define the splunkd entity path where views are stored
VIEW_ENTITY_CLASS = 'data/ui/views'

# define the default number of views to cache
VIEW_CACHE_SIZE_DEFAULT = 300
# magic number indicating the view cache size has not been initialized
VIEW_CACHE_SIZE_NOT_SET = -1

class MemoizedViewsI18N(object):
    '''
    Hold a different MemoizedViews object for each "active" locale.
    '''
    def __init__(self):
        self.i18n_views_lock = Lock()
        # map: locale -> MemoizedViews
        self.memoized_views = {}
    def __getattr__(self, attr):
        if attr in self.__dict__:
            return getattr(self, attr)
        locale = i18n.current_lang(as_string=True)
        with self.i18n_views_lock:
            if locale not in self.memoized_views:
                self.memoized_views[locale] = MemoizedViews()
            # Look up the cache for this locale.
            obj = self.memoized_views[locale]
        # Forward the method call to the appropriate cache.
        return getattr(obj, attr)

    def clearCachedViews(self):
        for locale in self.memoized_views:
            self.memoized_views[locale].clearCachedViews()

class MemoizedViews(object):
    '''
    This class is used to memoize parsed views in the appserver.
    '''

    def __init__(self):
        self.views_lock = Lock()
        # map: view digest -> parsed view datastructre
        self.digest_to_view_map = {}
        # view digests, sorted from least to most recently used
        self.digest_usage = splunk.util.OrderedDict()
        # XXX: Initialize the max cache size later. It appears that we cannot
        # do this in __init__() because the ViewController is created before
        # cherrypy.config is initialized in root.py.
        self.max_cache_size = VIEW_CACHE_SIZE_NOT_SET
        logger.info("initialize MemoizeViews %s" % hash(self))

    def getAvailableViews(self, namespace, refresh, output, flash_ok=True):
        time1 = time.time()

        try:
            if refresh:
                util.auto_refresh_ui_assets(VIEW_ENTITY_CLASS)
            digests = en.getEntities(VIEW_ENTITY_CLASS, namespace=namespace, count=-1, digest=1)
        except splunk.ResourceNotFound:
            return

        time2 = time.time()

        rawxml = en.EntityCollection()
        for item, digest in digests.items():
            parsed_view = self.getParsedView(namespace, rawxml, item, digest.get('eai:digest'), flash_ok=flash_ok)
            if parsed_view:
                parsed_view = copy.copy(parsed_view)
                parsed_view['canWrite'] = splunk.util.normalizeBoolean(digest.get('eai:acl').get('can_write'))
                parsed_view['editUrlPath'] = dict(digest.links).get('edit')
                parsed_view['app'] = digest.get('eai:acl').get('app')
                parsed_view['label'] = digest.get('label', "")
                parsed_view['isDashboard'] = digest.get('isDashboard', 1)
                parsed_view['isVisible'] = splunk.util.normalizeBoolean(digest.get('isVisible', True))
                output[item] = parsed_view


        time3 = time.time()

        logger.info('PERF - getDigestTime=%ss getParsedViewTime=%ss' % (round(time2-time1, 4), round(time3-time2, 4)))

    def getParsedView(self, namespace, rawxml, viewid, viewdigest, flash_ok=True):
        # add to the cache key when the flash_ok boolean is False, since some views will be different in this case
        if not flash_ok:
            viewdigest = viewdigest + '_flash_ok=False'
        with self.views_lock:
            entry = self.digest_to_view_map.get(viewdigest)
            if entry:
                self.touchCacheEntry(viewdigest)
                # Cache hit!
                return entry

        if len(rawxml) == 0:
            # Only do a single viewstate GET for each batch of cache misses.
            rawxml.update(en.getEntities(VIEW_ENTITY_CLASS,
                                         namespace=namespace,
                                         count=-1))

        parsed_view = None
        try:
            viewobj = rawxml.get(viewid)
            if not viewobj:
                # This view's digest appeared in an earlier GET, but it no
                # longer exists now that we are trying to fetch its contents.
                # It has probably been deleted or re-permissioned. Ignore it.
                return None
            try:
                if viewobj.get('eai:type') == "views":
                    native_view = viewconf.loads(viewobj.get('eai:data'), viewid,
                                                 sourceApp=viewobj.get('eai:acl', {}).get('app', None),
                                                 flashOk=flash_ok)
                    native_view['viewEntry'] = viewobj
                elif viewobj.get('eai:type') == "html":
                    native_view = {'type': 'module', 'viewName': viewid, 'template': "view/dashboard_escaped_render.html", 'modules': {}, 'layoutRoster': {}, 'dashboard': viewobj.get("eai:data")}
            except Exception as e:
                native_view = {
                    "objectMode": "XMLError",
                    "message": str(e),
                    "modules": []
                }
            parsed_view = self._generateViewRoster(viewid, native_view, viewobj.getFullPath())
            with self.views_lock:
                self.digest_to_view_map[viewdigest] = parsed_view
                self.touchCacheEntry(viewdigest)
                self.evictLeastRecentlyUsed()
            logger.info('Populate cache for view "%s" (%s) with digest %s, making cache_size=%s' % (viewid, namespace, viewdigest, len(self.digest_to_view_map)))
        except Exception as e:
            logger.error('Error loading view "%s"' % viewid)
            logger.exception(e)

        return parsed_view

    def touchCacheEntry(self, viewdigest):
        '''
        Update the last used time for this cache entry.
        '''
        # Update the usage time for this cache entry.
        try:
            del self.digest_usage[viewdigest]
        except Exception as e:
            pass
        self.digest_usage[viewdigest] = time.time()

    def evictLeastRecentlyUsed(self):
        '''
        If necessary, evict the least recently used item from the cache.
        '''
        if self.max_cache_size == VIEW_CACHE_SIZE_NOT_SET:
            self.max_cache_size = cherrypy.config.get('max_view_cache_size', VIEW_CACHE_SIZE_DEFAULT)
        sz = len(self.digest_to_view_map)
        if sz <= self.max_cache_size:
            return
        evicted_digest, last_used = self.digest_usage.popitem(last=False)
        self.digest_to_view_map.pop(evicted_digest)
        logger.info('Evicted view with digest=%s last_used=%s because cache_size=%s' % (evicted_digest, last_used, sz))

    def clearCachedViews(self):
        '''
        resets the cache
        '''
        logger.info("Clearing the cache")
        logger.info("cachecacheiicachei: %s %d, %s %d" % (self.digest_to_view_map, len(self.digest_to_view_map), self.digest_usage, len(self.digest_usage)))
        with self.views_lock:
            self.digest_to_view_map.clear()
            self.digest_usage.clear()
        logger.info("Cleared the cachei: %s %d, %s %d" % (self.digest_to_view_map, len(self.digest_to_view_map), self.digest_usage, len(self.digest_usage)))

    def _generateViewRoster(self, viewName, viewConfig, urlPath):
        '''
        Returns a dict of properties for viewName, to be used by all the view
        components.

            layoutRoster: a dict of modules, keyed by their layoutPanel
                          assigment.
            activeModules: a dict of all modules requested by viewName

        SAMPLE OUTPUT:

            {
                layoutRoster: ...
                activeModules: ...
            }
        '''
        logger.debug("template for view " + viewName + " is " + viewConfig.get('template', "undefined"))

        output = {
            'layoutRoster': {},
            'activeModules': {},
            'isVisible'  : viewConfig.get('isVisible', True),
            'displayView': viewConfig.get('displayView', viewName),
            'refresh'    : viewConfig.get('refresh', None),
            'template'   : viewConfig.get('template', 'search.html'),
            'dashboard'   : viewConfig.get('dashboard', {}),
            'onunloadCancelJobs': viewConfig.get('onunloadCancelJobs', True),
            'isSticky': viewConfig.get('isSticky', True),
            'isPersistable': viewConfig.get('isPersistable', True),
            'autoCancelInterval': viewConfig.get('autoCancelInterval'),
            'stylesheet': viewConfig.get('stylesheet', None),
            'objectMode': viewConfig.get('objectMode'),
            'nativeObjectMode': viewConfig.get('nativeObjectMode', viewConfig.get('objectMode')),
            'hasAutoRun': self.hasAutoRun(viewConfig),
            'hasRowGrouping': viewConfig.get('hasRowGrouping', False),
            'message': viewConfig.get('message', None),
            'decomposeIntentions': viewConfig.get('decomposeIntentions', False),
            'type': viewConfig.get('type', 'module'),
            'target': viewConfig.get('target', None),
            'viewEntry': viewConfig.get('viewEntry', None)
        }

        localUsageCounter = {}

        # loop over all the modules requested in view config
        for module in viewConfig['modules']:

            # process the module config through generator, and collate them
            # into the layout panels, and identify as active module
            for item in self._moduleGenerator(module, panelName=viewName, usageCounter=localUsageCounter):
                if not output['layoutRoster'].get(item.get('layoutPanel')):
                    output['layoutRoster'][item.get('layoutPanel')] = []
                output['layoutRoster'][item.get('layoutPanel')].append(item)
                if 'inheritance' in item:
                    for inheritedClass in item['inheritance']:
                        output['activeModules'][inheritedClass] = 1
                if 'include' in item:
                    for includedClass in item['include']:
                        output['activeModules'][includedClass] = 1
                if output['isSticky'] == False and 'stickyParams' in item:
                    item['stickyParams'] = []
                if output['isPersistable'] == False and 'persistableParams' in item:
                    item['persistableParams'] = []

        output['activeModules'] = self._sortModuleNames(output['activeModules'])

        return output

    def _moduleGenerator(self, moduleConfig, position=0, depth=0, parentLayout=None, parentId=None, panelName=None, usageCounter=None, isHidden=False):
        '''
        Yields every module that is listed in moduleConfig.  The dict yielded
        for each module looks like:

            {
                'parentmodule': '#AsciiTimeline_0_0',
                'inheritance': [
                    'Splunk.Module',
                    'Splunk.Module.SearchModule',
                    'Splunk.Module.SimpleFieldViewer'
                ],
                'className': 'SimpleFieldViewer',
                'layoutPanel': 'fullWidthControls',
                'id': 'SimpleFieldViewer_1_1',
                'templatePath': '../../modules/field_viewer/field_viewer.html',

                'params': {
                    'paramName1': {
                        'default': 'defaultValue1',
                        'required': True,
                        'values': ['acceptableValue1','acceptableValue2','acceptableValue3']
                    }
                },
                'stickyParams': ['key1','key2','key3'],
                'persistableParams': ['key1','key2','key3'],

                // custom param fields...
                'count': 10,
                'field': 'twikiuser'
            }

        'id' is of the form: <module_name>_<module_usage_count>_<tree_depth>_<sibling_position>
        'templatePath' is the relative URL (mako seems to need it) of the template
        'parentmodule' is the DOM id of parent; not present if a top level module

        '''

        # first lookup the module definition
        # TODO: the module prefix is patched in here
        moduleName = 'Splunk.Module.' + moduleConfig['className']
        moduleDefinition = module.moduleMapper.getInstalledModules().get(moduleName, {})

        # generate module config for current module
        output = moduleConfig.copy()

        # add the module persistence information
        output['stickyParams'] = moduleDefinition.get('stickyParams', [])
        output['persistableParams'] = moduleDefinition.get('persistableParams', [])


        output.setdefault('params', {})

        # merge default params supplied by the module's manifest file with the view's params
        if moduleDefinition:

            # check that required params defined in .conf are present
            for param_name, param in list(moduleDefinition['params'].items()):

                if param_name not in output['params']:
                    if param['default'] is not None:
                        output['params'][param_name] = param['default']
                    elif param['required']:
                        message.send_client_message(
                            'error',
                            _("Misconfigured view '%(panel)s' - Parameter '%(param)s' is required for module %(module)s") %
                            {
                                'panel':panelName,
                                'param':param_name,
                                'module':moduleConfig['className']
                            }
                        )

                if param['values'] and param_name in output['params'] and splunk.util.normalizeBoolean(output['params'].get(param_name)) not in splunk.util.normalizeBoolean(param['values']):
                    logger.warn(
                        "Misconfigured view '%s' - Parameter '%s' is set to '%s', whereas it must be set to one of %s for module %s" % (
                            panelName, param_name, output['params'].get(param_name), param['values'],
                            moduleConfig['className']))

                    if param['required']:
                        message.send_client_message(
                            'error',
                            _(
                                "Misconfigured view '%(panel)s' - Parameter '%(param)s' must be set to one of %(values)s for module %(module)s") %
                            {
                                'panel': panelName,
                                'param': param_name,
                                'values': param['values'],
                                'module': moduleConfig['className']
                            }
                        )

            # check if the module config defines wildcarded parameters
            wildcardPrefixes = [x[0:-1] for x in moduleDefinition['params'] if x.endswith('*')]

            # now remove the wildcard placeholder from param list
            for wc in wildcardPrefixes:
                output['params'].pop(wc + '*', None)

            # the reverse check - enforce that all configured params are documented in the conf.
            for param_name in output['params'] :

                # ensure that params that match wildcards are allowed through
                if any(map(param_name.startswith, wildcardPrefixes)):
                    continue

                if param_name not in moduleDefinition['params'] and param_name not in {"group":1, "groupLabel": 1, "autoRun":1, "altTitle":1} :
                    message.send_client_message(
                        'error',
                        _("Misconfigured view '%(panel)s' - Unknown parameter '%(param)s' is defined for module %(module)s. Make sure the parameter is specified in %(module)s.conf.") %
                        {
                            'panel':panelName,
                            'param':param_name,
                            'module':moduleConfig['className']
                        }
                    )

                if param_name in moduleDefinition['params'] and moduleDefinition['params'][param_name]['translate']:
                    translate = moduleDefinition['params'][param_name]['translate']
                    if translate == 'string':
                        output['params'][param_name] = _(output['params'][param_name])
                    else:
                        i18n.translate_view_params(translate, output['params'][param_name])


        # assign a unique DOM ID to this module
        usageCounter.setdefault(moduleConfig['className'], -1)
        usageCounter[moduleConfig['className']] += 1
        output['id'] = '%s_%s_%s_%s' % (
            moduleConfig['className'],
            usageCounter[moduleConfig['className']],
            depth,
            position)

        output['isHidden'] = isHidden

        if moduleConfig.get('intersect'):
            output['intersect'] = moduleConfig.get('intersect')

        output['layoutPanel'] = moduleConfig.get('layoutPanel') or parentLayout

        if not output['layoutPanel']:
            message.send_client_message(
                'error',
                _("Misconfigured view '%(panel)s' - layoutPanel is not defined for module %(module)s. ") %
                {
                    'panel':panelName,
                    'module':moduleConfig['className']
                }
            )

        if parentId:
            output['parentmodule'] = '#%s' % parentId
        if 'html' in moduleDefinition:
            output["templatePath"] = '=' + moduleDefinition['html'] # use '=' prefix to denote absolute template path
        if 'inheritance' in moduleDefinition:
            output['inheritance'] = moduleDefinition['inheritance']
        if 'include' in moduleDefinition:
            output['include'] = moduleDefinition['include']
        if 'children' in output: del output['children']
        yield output

        # process children
        if 'children' in moduleConfig:
            for i, child in enumerate(moduleConfig['children']):
                hideChild = True if (isHidden or splunk.util.normalizeBoolean(output['params'].get('hideChildrenOnLoad'))) else False
                childOutput = self._moduleGenerator(child, i, depth+1, output['layoutPanel'], output['id'], panelName=panelName, usageCounter=usageCounter, isHidden=hideChild)
                if isinstance(childOutput, dict):
                    yield childOutput
                else:
                    for x in childOutput: yield x


    def _sortModuleNames(self, moduleList):
        '''
        Returns a module list, sorted by inheritance order.  Called from
        _generateViewRoster().
        '''

        output = []
        for moduleName in module.moduleMapper.getInstalledModules():
            if moduleName in moduleList:
                output.append(moduleName)

        return output


    def hasAutoRun(self, viewConfig):
        '''
        Inspects view config modules for the presence of a single autoRun param with a True value.
        Exits on first truthy condition.
        '''
        hasAutoRun = False
        for module in viewConfig.get("modules", []):
            if 'params' in module and module['params'].get('autoRun', False):
                hasAutoRun = True
                break
        return hasAutoRun

memoizedViews = MemoizedViewsI18N()
