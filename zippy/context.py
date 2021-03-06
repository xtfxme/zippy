#!/usr/bin/env python

#TODO: re-link in build order

from __future__ import division
from __future__ import absolute_import
from __future__ import print_function

import sys, os

import json
from functools import partial
from waflib import ConfigSet, Context

from .util import get_module
database = get_module('distlib.database')
metadata = get_module('distlib.metadata')
locators = get_module('distlib.locators')
compat = get_module('distlib.compat')

import logging
logger = logging.getLogger(__name__)

import glob
import urllib2
import subprocess
from urlparse import urlsplit
from urlparse import urlunsplit
from urlparse import parse_qsl
from hashlib import sha256
from base64 import b64encode


def open(self, *args, **kwds):
    request = compat.Request(*args, **kwds)
    if self.username or self.password:
        request.add_header('Authorization', 'Basic ' + b64encode(
            (self.username or '') + ':' + (self.password or '')
            ))
    fp = self.opener.open(request)
    return fp
locators.Locator.username = None
locators.Locator.password = None
locators.Locator.open = open
# unmask builtin
del open


class ContextZeroLog(object):

    def __init__(self, op, cls=None):
        self.oper = 'zero_oper_' + op
        self.noop = 'zero_noop_' + op
        if cls:
            setattr(cls, self.oper, getattr(cls, op, None))

    def __get__(self, obj, cls):
        if obj is None:
            return self

        if obj.zero_log:
            return getattr(obj, self.noop)

        return getattr(obj, self.oper)

    def __set__(self, obj, attr):
        if obj.zero_log:
            return None

        return setattr(obj, self.oper, attr)

def zero_log(self):
    if hasattr(self, 'options'):
        return self.options.zero_log

    if hasattr(self, 'env'):
        return self.zpy.opt['zero_log']

    return False
Context.Context.zero_log = property(zero_log)
del zero_log

Context.Context.zero_noop_logger = None
Context.Context.logger = ContextZeroLog('logger', Context.Context)
#Context.Context.zero_noop_msg = lambda *a, **k: None
#Context.Context.msg = ContextZeroLog('msg', Context.Context)


class PythonLocator(locators.Locator):

    _distributions = frozenset(('Python',))

    def __init__(self, **kwds):
        self.avail = dict()
        self.url = kwds.pop(
            'url',
            'http://hg.python.org/cpython/tags?style=raw',
            )
        self.source_url = kwds.pop(
            'source_url',
            'http://hg.python.org/cpython/archive/{0}.zip',
            )
        super(PythonLocator, self).__init__(**kwds)

        fp = self.open(self.url)
        try:
            for line in fp.readlines():
                ver, rev = line.strip().split('\t', 2)
                ver = ver.lstrip('v')
                if ver and ver[0].isdigit():
                    dist = database.make_dist(
                        'Python', ver, summary='Placeholder for summary',
                        )
                    dist.metadata.source_url = self.source_url.format(rev)
                    self.avail[ver] = dist
        finally:
            fp.close()

    def _get_project(self, name):
        if name.title() not in self._distributions:
            return dict()

        return self.avail

    def get_distribution_names(self):
        return self._distributions


class GitLocator(locators.Locator):

    def __init__(self, **kwds):
        super(GitLocator, self).__init__(**kwds)
        self.distributions = dict()
        self.repos = dict()

    def _get_project(self, name):
        if name not in self.distributions:
            return dict()

        dists = self.distributions[name]
        return dists

    def get_distribution_names(self):
        names = self.distributions.viewkeys()
        return names

    def add_hint(self, req, ctx):
        url = urlsplit(req.url)
        scheme = url.scheme.split('+')
        if scheme[0] != 'git':
            return req

        repo = dict(parse_qsl(url.fragment))
        if '@' in url.path:
            path, ref = url.path.rsplit('@', 1)
            url = url._replace(path=path)
            if len(ref) < 16 or set(ref) - set('0123456789abcdefABCDEF'):
                # ref is probably a branch/tag
                repo.update(ref=ref)
            else:
                # ref is a probably a specific commit
                repo.update(rev=ref)

        url = url._replace(scheme=scheme[-1], query='', fragment='')
        source = repo['url'] = urlunsplit(url)
        name = repo['ident'] = sha256(req.url).hexdigest()[:16]
        dest = os.path.join(ctx.zpy.top_xsrc, name)
        meta = dest + '.' + metadata.METADATA_FILENAME
        if not os.path.exists(dest):
            if repo.get('rev'):
                # must clone... cannot use targeted fetch
                subprocess.call(['git', 'clone', source, dest])
                subprocess.call(['git', 'checkout', repo['rev']], cwd=dest)
            else:
                # refs/HEAD can be targeted directly
                refspec = '{0}:master'.format(repo.get('ref', 'HEAD'))
                # create a blank repo
                subprocess.call(['git', 'init', dest])
                # rip down the ref we need
                subprocess.call([
                    'git',
                        'fetch',
                            '--update-head-ok',
                            '--force',
                            '--depth=1',
                            '--no-tags',
                                source,
                                refspec,
                                ],
                    cwd=dest,
                    )
                # refresh the work tree
                subprocess.call(['git', 'checkout'], cwd=dest)
            # generate egg-info
            subprocess.call([
                sys.executable, 'setup.py',
                    'egg_info',
                        '--no-svn-revision',
                        '--no-date',
                        '--tag-build='
                        ],
                cwd=dest,
                )
            info = glob.glob(os.path.join(dest, '*.egg-info')).pop()
            dist = database.EggInfoDistribution(path=info)
            #FIXME: setting source_url is not working?
            with open(meta, mode='w') as fp:
                pydist = dist.metadata.dictionary
                pydist['source_url'] = os.path.relpath(dest, ctx.zpy.top)
                json.dump(
                    fp=fp,
                    obj=pydist,
                    ensure_ascii=True,
                    sort_keys=True,
                    indent=2,
                    )
        meta = metadata.Metadata(path=meta)
        dist = database.Distribution(metadata=meta)
        repo.update(egg=meta.name, version=meta.version)
        self.repos[meta.name] = repo
        self.distributions[meta.name] = {meta.version: dist}
        req = database.parse_requirement('{0} (=={1})'.format(
            meta.name, meta.version,
            ))
        return req


class GlobLocator(locators.Locator):

    def __init__(self, **kwds):
        self.nodes = dict()
        self.distributions = dict()
        self.url = kwds.pop('url', None)
        self.ctx = kwds.pop('ctx', None)
        super(GlobLocator, self).__init__(**kwds)

        if self.url:
            if not self.url.path.endswith('/'):
                path = os.path.join(self.url.path, '')
                self.url = self.url._replace(path=path)
            potentials = glob.glob(self.url.path)
            for potential in potentials:
                potential = os.path.abspath(potential)
                path = os.path.join(potential, metadata.METADATA_FILENAME)
                if not os.path.exists(path):
                    continue

                # EggInfoDistribution?
                node = self.ctx.root.make_node(potential)
                pydist = metadata.Metadata(path=path)
                #NOTE: what if assigned Node here...?
                pydist.source_url = node.path_from(self.ctx.srcnode)
                dist = database.Distribution(metadata=pydist)
                info = self.distributions[dist.name] = {
                    dist.metadata.version: dist,
                    }

                dist_node = self.ctx.bldnode.make_node(str(node))
                nodes = self.nodes[dist.name] = (node, dist_node)

    def _get_project(self, name):
        if name not in self.distributions:
            return dict()

        #FIXME: self.node is probably cruft now
        dists = self.distributions[name]
        return dists

    def get_distribution_names(self):
        names = self.distributions.viewkeys()
        return names


#TODO: custom aggregating locator
#class CleanAggregatingLocator(...):
#    ...
#    dist.metadata._legacy = None
#    dist.metadata._data = pydist
#    dist.name = dist.metadata.name
#    ...


class JSONDirectoryLocator(locators.DirectoryLocator):

    def _get_project(self, *args, **kwds):
        dists = super(JSONDirectoryLocator, self)._get_project(*args, **kwds)
        for dist in dists.values():
            if not dist.source_url:
                continue

            if '://' in dist.source_url and not 'file://' in dist.source_url:
                continue

            pydist = dist.source_url + '.' + metadata.METADATA_FILENAME
            try:
                pydist = self.open(pydist).read()
            except IOError:
                logger.warn('missing {0} for {1}'.format(
                    metadata.METADATA_FILENAME,
                    dist
                    ))
                continue

            try:
                pydist = json.loads(pydist)
            except ValueError:
                logger.warn('corrupt {0} for {1}'.format(
                    metadata.METADATA_FILENAME,
                    dist
                    ))
                continue

            #NOTE: forces metadata 2.x!
            dist.metadata._legacy = None
            dist.metadata._data = pydist
            dist.name = dist.metadata.name
        return dists


#NOTE: maybe a bit funky, but this is 100% normal; dynamically splicing
# methods/attrs onto classes is *central* to using and understanding waf!
def zpy(ctx, _zpy=ConfigSet.ConfigSet()):
    env = ctx.env
    if not hasattr(ctx, 'zippy_dist_get'):
        if 'top_xsrc' in env:
            def dist_get(key=None, **kwds):
                if key and 'mapping' not in kwds:
                    kwds['mapping'] = env.dist[key]
                dist = database.Distribution(
                    metadata=metadata.Metadata(**kwds),
                    )
                return dist
            ctx.zippy_dist_get = dist_get

            ctx.git_locator = GitLocator()
            ctx.locators = [ctx.git_locator]
            for locator_str in env.opt['locator']:
                locator_url = compat.urlsplit(locator_str)
                if not locator_url.scheme:
                    locator_url = locator_url._replace(scheme='glob')
                locator_name = locator_url.scheme.title() + 'Locator'

                try:
                    locator = globals()[locator_name](
                        url=locator_url,
                        ctx=ctx,
                        )
                except KeyError:
                    raise ValueError('missing: {0}.{1} ({2})'.format(
                        __name__,
                        locator_name,
                        locator_str,
                        ))

                ctx.locators.append(locator)
            ctx.locators += [
                # eg. extern/sources/*.pydist.json
                JSONDirectoryLocator(env.top_xsrc, recursive=False),
                # eg. http://hg.python.org/cpython/archive/tip.zip
                PythonLocator(),
                # eg. https://www.red-dove.com/pypi/projects/U/uWSGI/
                locators.JSONLocator(),
                # eg. https://pypi.python.org/simple/uWSGI/
                locators.SimpleScrapingLocator(env.api_pypi, timeout=3.0),
                ]

            params = dict(
                # scheme here applies to the loose matching of dist version.
                # currently, most pypi dists are not PEP 426/440 compatible.
                # *DOES NOT* apply to returned [2.x] metadata!
                scheme='legacy',
                # return the first dist found in the stack and stop searching!
                merge=False,
                )
            ctx.aggregating_locator = locators.AggregatingLocator(
                *ctx.locators, **params
                )
            ctx.dependency_finder = locators.DependencyFinder(
                ctx.aggregating_locator,
                )
    return env
Context.Context.zpy = property(zpy)
