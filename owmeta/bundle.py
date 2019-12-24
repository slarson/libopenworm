import re
from yarom.utils import FCN
from yarom.rdfUtils import transitive_lookup, BatchAddGraph
from os.path import join as p, exists, relpath, expanduser
from os import makedirs, rename, scandir, listdir
import logging
import hashlib
import shutil
import errno
import io
from rdflib.term import URIRef
from struct import pack
import yaml
import six
from .command_util import DEFAULT_OWM_DIR
from .context import DEFAULT_CONTEXT_KEY, IMPORTS_CONTEXT_KEY
from .context_common import CONTEXT_IMPORTS
from .data import Data
from .file_match import match_files
from .file_lock import lock_file
from .graph_serialization import write_canonical_to_file, gen_ctx_fname

try:
    from urllib.parse import quote as urlquote
except ImportError:
    from urllib import quote as urlquote

L = logging.getLogger(__name__)


class Remote(object):
    '''
    A place where bundles come from and go to
    '''
    def __init__(self, name, accessor_configs=()):
        '''
        Parameters
        ----------
        name : str
            The name of the remote
        accessor_configs : iterable of AccessorConfig
            Configs for how you access the remote
        '''

        self.name = name
        ''' Name of the remote '''

        self.accessor_configs = list(accessor_configs)
        ''' Configs for how you access the remote. Probably just URLs '''

        self._loaders = []

    def add_config(self, accessor_config):
        self.accessor_configs.append(accessor_config)

    def generate_loaders(self):
        '''
        Generate the bundle loaders for this remote
        '''
        self._loaders = []
        for ac in self.accessor_configs:
            for lc in LOADER_CLASSES:
                if lc.can_load_from(ac):
                    loader = lc(ac)
                    self._loaders.append(loader)
                    yield loader

    def write(self, out):
        '''
        Serialize the `Remote` and write to `out`

        Parameters
        ----------
        out : :term:`file object`
            Target for writing the remote
        '''
        yaml.dump(self, out)

    @classmethod
    def read(cls, inp):
        '''
        Read a serialized `Remote`

        Parameters
        ----------
        inp : :term:`file object`
            File-like object containing the serialized `Remote`
        '''
        res = yaml.full_load(inp)
        assert isinstance(res, cls)
        return res

    def __eq__(self, other):
        return (self.name == other.name and
                self.accessor_configs == other.accessor_configs)

    def __hash__(self):
        return hash((self.name, self.accessor_configs))


class DependencyDescriptor(object):
    __slots__ = ('id', 'version')

    def __new__(cls, id, version=None):
        res = super(DependencyDescriptor, cls).__new__(cls)
        res.id = id
        res.version = version
        return res

    def __eq__(self, other):
        return self.id == other.id and self.version == other.version

    def __hash__(self):
        return hash((self.id, self.version))

    def __repr__(self):
        return '{}({}{})'.format(
                FCN(type(self)),
                repr(self.id),
                (', ' + repr(self.version)) if self.version is not None else '')


class AccessorConfig(object):
    '''
    Configuration for accessing a remote. Loaders are added to a remote according to which
    accessors are avaialble
    '''

    def __eq__(self, other):
        raise NotImplementedError()

    def __hash__(self):
        raise NotImplementedError()


class URLConfig(AccessorConfig):
    '''
    Configuration for accessing a remote with just a URL.
    '''

    def __init__(self, url):
        self.url = url

    def __eq__(self, other):
        return self.url == other.url

    def __hash__(self):
        return hash(self.url)

    def __str__(self):
        return '{}(url={})'.format(FCN(type(self)), repr(self.url))

    __repr__ = __str__


class Descriptor(object):
    '''
    Descriptor for a bundle
    '''
    def __init__(self, ident):
        self.id = ident
        self.name = None
        self.version = 1
        self.description = None
        self.patterns = set()
        self.includes = set()
        self.dependencies = set()
        self.files = None

    @classmethod
    def make(cls, obj):
        '''
        Makes a descriptor from the given object.
        '''
        res = cls(ident=obj['id'])
        res.name = obj.get('name', obj['id'])
        res.version = obj.get('version', 1)
        res.description = obj.get('description', None)
        res.patterns = set(make_pattern(x) for x in obj.get('patterns', ()))
        res.includes = set(make_include_func(x) for x in obj.get('includes', ()))

        deps = set()
        for x in obj.get('dependencies', ()):
            if isinstance(x, six.string_types):
                deps.add(DependencyDescriptor(x))
            elif isinstance(x, dict):
                deps.add(DependencyDescriptor(**x))
            else:
                deps.add(DependencyDescriptor(*x))
        res.dependencies = deps
        res.files = FilesDescriptor.make(obj.get('files', None))
        return res

    def __str__(self):
        return (FCN(type(self)) + '(ident={},'
                'name={},version={},description={},'
                'patterns={},includes={},'
                'files={},dependencies={})').format(
                        repr(self.id),
                        repr(self.name),
                        repr(self.version),
                        repr(self.description),
                        repr(self.patterns),
                        repr(self.includes),
                        repr(self.files),
                        repr(self.dependencies))


class Bundle(object):
    def __init__(self, ident, bundles_directory=None, version=None, conf=None, remotes=()):
        if not ident:
            raise Exception('ident must be non-None')
        self.ident = ident
        if bundles_directory is None:
            bundles_directory = expanduser(p('~', '.owmeta', 'bundles'))
        self.bundles_directory = bundles_directory
        if not conf:
            conf = {'rdf.source': 'sqlite'}
        self.version = version
        self.remotes = remotes
        self._given_conf = conf
        self.conf = None
        self._contexts = None

    @property
    def identifier(self):
        return self.ident

    def resolve(self):
        try:
            bundle_directory = self._get_bundle_directory()
        except BundleNotFound:
            # If there's a .owm directory, then get the remotes from there
            if self.remotes:
                remotes = self.remotes

            if not remotes and exists(DEFAULT_OWM_DIR):
                # TODO: Make this search upwards in case the directory exists at a parent
                remotes = retrieve_remotes(DEFAULT_OWM_DIR)

            if remotes:
                f = Fetcher(self.bundles_directory, remotes)
                bundle_directory = f(self.ident, self.version)
            else:
                raise
        return bundle_directory

    def _get_bundle_directory(self):
        # - look up the bundle in the index
        # - generate a config based on the current config load the config
        # - make a database from the graphs, if necessary (similar to `owm regendb`). If
        #   delete the existing database if it doesn't match the store config
        version = self.version
        if version is None:
            bundle_root = bundle_directory(self.bundles_directory, self.ident)
            latest_version = 0
            try:
                ents = scandir(bundle_root)
            except (OSError, IOError) as e:
                if e.errno == 2: # FileNotFound
                    raise BundleNotFound(self.ident, 'Bundle directory does not exist')
                raise

            for ent in ents:
                if ent.is_dir():
                    try:
                        vn = int(ent.name)
                    except ValueError:
                        # We may put things other than versioned bundle directories in
                        # this directory later, in which case this is OK
                        pass
                    else:
                        if vn > latest_version:
                            latest_version = vn
            version = latest_version
        if not version:
            raise BundleNotFound(self.ident, 'No versioned bundle directories exist')
        res = bundle_directory(self.bundles_directory, self.ident, version)
        if not exists(res):
            if self.version is None:
                raise BundleNotFound(self.ident, 'Bundle directory does not exist')
            else:
                raise BundleNotFound(self.ident, 'Bundle directory does not exist for the specified version', version)
        return res

    def _make_config(self, bundle_directory, progress=None, trip_prog=None):
        self.conf = Data().copy(self._given_conf)
        self.conf['rdf.store_conf'] = p(bundle_directory, 'owm.db')
        self.conf[IMPORTS_CONTEXT_KEY] = fmt_bundle_ctx_id(self.ident)
        with open(p(bundle_directory, 'manifest')) as mf:
            for ln in mf:
                if ln.startswith(DEFAULT_CONTEXT_KEY):
                    self.conf[DEFAULT_CONTEXT_KEY] = ln[len(DEFAULT_CONTEXT_KEY) + 1:]
                if ln.startswith(IMPORTS_CONTEXT_KEY):
                    self.conf[IMPORTS_CONTEXT_KEY] = ln[len(IMPORTS_CONTEXT_KEY) + 1:]
        # Create the database file and initialize some needed data structures
        self.conf.init()
        if not exists(self.conf['rdf.store_conf']):
            raise Exception('Cannot find the database file at ' + self.conf['rdf.store_conf'])
        self._load_all_graphs(bundle_directory, progress=progress, trip_prog=trip_prog)

    @property
    def contexts(self):
        ''' Return contexts in a bundle '''
        # Since bundles are meant to be immutable, we won't need to add
        if self._contexts is not None:
            return self._contexts
        bundle_directory = self.resolve()
        contexts = set()
        graphs_directory = p(bundle_directory, 'graphs')
        idx_fname = p(graphs_directory, 'index')
        if not exists(idx_fname):
            raise Exception('Cannot find an index at {}'.format(repr(idx_fname)))
        with open(idx_fname, 'rb') as index_file:
            for l in index_file:
                l = l.strip()
                if not l:
                    continue
                ctx, _ = l.split(b'\x00')
                contexts.add(ctx.decode('UTF-8'))
        self._contexts = contexts
        return self._contexts

    def _load_all_graphs(self, bundle_directory, progress=None, trip_prog=None):
        # This is very similar to the owmeta.command.OWM._load_all_graphs, but is
        # different enough that it's easier to just keep them separate
        import transaction
        from rdflib import plugin
        from rdflib.parser import Parser, create_input_source
        contexts = set()
        graphs_directory = p(bundle_directory, 'graphs')
        idx_fname = p(graphs_directory, 'index')
        if not exists(idx_fname):
            raise Exception('Cannot find an index at {}'.format(repr(idx_fname)))
        triples_read = 0
        dest = self.rdf
        with open(idx_fname, 'rb') as index_file:
            if progress is not None:
                cnt = 0
                for l in index_file:
                    cnt += 1
                index_file.seek(0)

                progress.total = cnt

            parser = plugin.get('nt', Parser)()
            with transaction.manager:
                for l in index_file:
                    l = l.strip()
                    if not l:
                        continue
                    ctx, fname = l.split(b'\x00')
                    graph_fname = p(graphs_directory, fname.decode('UTF-8'))
                    ctx_str = ctx.decode('UTF-8')
                    contexts.add(ctx_str)
                    with open(graph_fname, 'rb') as f, \
                            BatchAddGraph(dest.get_context(ctx_str), batchsize=4000) as g:
                        parser.parse(create_input_source(f), g)

                    if progress is not None and trip_prog is not None:
                        progress.update(1)
                        triples_read += g.count
                        trip_prog.update(g.count)
                if progress is not None:
                    progress.write('Finalizing writes to database...')
        self._contexts = contexts
        if progress is not None:
            progress.write('Loaded {:,} triples'.format(triples_read))

    @property
    def rdf(self):
        return self.conf['rdf.graph']

    def __enter__(self):
        self._make_config(self.resolve())
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.conf.destroy()

    def __call__(self, target):
        if target and hasattr(target, 'contextualize'):
            return target.contextualize(self)
        return None


def bundle_directory(bundles_directory, ident, version=None):
    '''
    Get the directory for the given bundle identifier and version

    Parameters
    ----------
    ident : str
        Bundle identifier
    version : int
        Version number. If not provided, returns the directory containing all of the
        versions
    '''
    base = p(bundles_directory, urlquote(ident, safe=''))
    if version is not None:
        return p(base, str(version))
    else:
        return base


class Fetcher(object):
    ''' Fetches bundles '''

    def __init__(self, bundles_root, remotes):
        self.bundles_root = bundles_root
        self.remotes = remotes

    def __call__(self, *args, **kwargs):
        return self.fetch(*args, **kwargs)

    def fetch(self, bundle_name, bundle_version=None):
        '''
        Retrieve a bundle by name from a remote and put it in the local bundle index and
        cache

        Parameters
        ----------
        bundle_name : str
            The name of the bundle to retrieve. The name may include the version number.
        bundle_version : int
            The version of the bundle to retrieve. optional
        '''
        loaders = self._get_bundle_loaders(bundle_name, bundle_version)

        for loader in loaders:
            try:
                if bundle_version is None:
                    versions = loader.bundle_versions(bundle_name)
                    if not versions:
                        raise BundleNotFound(bundle_name, 'This loader does not have any'
                                ' versions of the bundle')
                    bundle_version = max(versions)
                bdir = bundle_directory(self.bundles_root, bundle_name, bundle_version)
                loader.base_directory = bdir
                loader(bundle_name, bundle_version)
                return bdir
            except Exception:
                L.warn("Failed to load bundle %s with %s", bundle_name, loader, exc_info=True)
        else: # no break
            raise NoBundleLoader(bundle_name, bundle_version)

    def _get_bundle_loaders(self, bundle_name, bundle_version):
        for rem in self.remotes:
            for loader in rem.generate_loaders():
                if loader.can_load(bundle_name, bundle_version):
                    yield loader


def retrieve_remotes(owmdir):
    '''
    Retrieve remotes from a owmeta project directory

    Parameters
    ----------
    owmdir : str
        path to the project directory
    '''
    remotes_dir = p(owmdir, 'remotes')
    if not exists(remotes_dir):
        return
    for r in listdir(remotes_dir):
        if r.endswith('.remote'):
            with open(p(remotes_dir, r)) as inp:
                try:
                    yield Remote.read(inp)
                except Exception:
                    L.warning('Unable to read remote %s', r, exc_info=True)


class Loader(object):
    '''
    Downloads bundles into the local index and caches them

    Attributes
    ----------
    base_directory : str
        The path where the bundle archive should be unpacked
    '''

    def __init__(self):
        # The base directory
        self.base_directory = None

    @classmethod
    def can_load_from(cls, accessor_config):
        ''' Returns True if the given accessor_config is a valid config for this loader '''
        return False

    def can_load(self, bundle_name, bundle_version=None):
        ''' Returns True if the bundle named `bundle_name` is supported '''
        return False

    def bundle_versions(self, bundle_name):
        '''
        List the versions available for the bundle.

        This is a required part of the `Loader` interface.

        Parameter
        ---------
        bundle_name : str
            ID of the bundle for which versions are requested

        Returns
        -------
            A list of int. Each entry is a version of the bundle available via this loader
        '''
        raise NotImplementedError()

    def load(self, bundle_name, bundle_version=None):
        '''
        Load the bundle into the local index

        Parameters
        ----------
        bundle_name : str
            ID of the bundle to load
        bundle_version : int
            Version of the bundle to load. Defaults to the latest available. optional
        '''
        raise NotImplementedError()

    def __call__(self, bundle_name, bundle_version=None):
        '''
        Load the bundle into the local index. Short-hand for `load`
        '''
        return self.load(bundle_name, bundle_version)


class HTTPBundleLoader(Loader):
    # TODO: Test this class...
    '''
    Loads bundles from HTTP(S) resources listed in an index file
    '''

    def __init__(self, index_url, cachedir=None, **kwargs):
        '''
        Parameters
        ----------
        index_url : str or URLConfig
            URL for the index file pointing to the bundle archives
        cachedir : str
            Directory where the index and any downloaded bundle archive should be cached.
            If provided, the index and bundle archive is cached in the given directory. If
            not provided, the index will be cached in memory and the bundle will not be
            cached.
        '''
        super(HTTPBundleLoader, self).__init__(**kwargs)

        if isinstance(index_url, str):
            self.index_url = index_url
        elif isinstance(index_url, URLConfig):
            self.index_url = index_url.url
        else:
            raise TypeError('Expecting a string or URLConfig. Received %s' %
                    type(index_url))

        self.cachedir = cachedir
        self._index = None

    def _setup_index(self):
        import requests
        if self._index is None:
            response = requests.get(self.index_url)
            self._index = response.json()

    @classmethod
    def can_load_from(cls, ac):
        return (isinstance(ac, URLConfig) and
                (ac.url.startswith('https://') or
                    ac.url.startswith('http://')))

    def can_load(self, bundle_name, bundle_version=None):
        self._setup_index()
        binfo = self._index.get(bundle_name)
        if binfo:
            if bundle_version is None:
                return True
            if not isinstance(binfo, dict):
                return False
            return bundle_version in binfo

    def bundle_versions(self, bundle_name):
        self._setup_index()
        binfo = self._index.get(bundle_name)

        if not binfo:
            return []

        res = []
        for k in binfo.keys():
            try:
                val = int(k)
            except ValueError:
                L.warning("Got unexpected non-version-number key '%s' in bundle index info", k)
            else:
                res.append(val)
        return res

    def load(self, bundle_name, bundle_version=None):
        '''
        Loads a bundle by downloading an index file
        '''
        import requests
        self._setup_index()
        binfo = self._index.get(bundle_name)
        if not binfo:
            raise LoadFailed(bundle_name, self, 'Bundle is not in the index')
        if not isinstance(binfo, dict):
            raise LoadFailed(bundle_name, self, 'Unexpected type of bundle info in the index')
        if bundle_version is None:
            max_vn = 0
            for k in binfo.keys():
                try:
                    val = int(k)
                except ValueError:
                    L.warning("Got unexpected non-version-number key '%s' in bundle index info", k)
                else:
                    if max_vn < val:
                        max_vn = val
            bundle_version = max_vn
        bundle_url = binfo.get(str(bundle_version))
        if bundle_url is None:
            raise LoadFailed(bundle_name, self, 'Did not find a URL for "%s" at'
                    ' version %s', bundle_name, bundle_version)
        response = requests.get(bundle_url, stream=True)
        if self.cachedir is not None:
            bfn = urlquote(bundle_name)
            with open(p(self.cachedir, bfn), 'w') as f:
                for chunk in response.iter_content(chunk_size=1024):
                    f.write(chunk)
            with open(p(self.cachedir, bfn), 'r') as f:
                self._unpack(f)
        else:
            bio = io.BytesIO()
            bio.write(response.raw.read())
            bio.seek(0)
            self._unpack(bio)

    def _unpack(self, f):
        import tarfile
        with tarfile.open(mode='r:xz', fileobj=f) as ba:
            ba.extractall(self.base_directory)


class DirectoryLoader(Loader):
    '''
    Loads a bundle into a directory.

    Created from a remote to actually get the bundle
    '''
    def __init__(self, base_directory=None):
        self.base_directory = base_directory

    def load(self, bundle_name):
        '''
        Loads a bundle into the given base directory
        '''
        super(DirectoryLoader, self).load(bundle_name)


class Installer(object):
    '''
    Installs a bundle locally
    '''

    # TODO: Make source_directory optional -- not every bundle needs files
    def __init__(self, source_directory, bundles_directory, graph,
                 imports_ctx=None, default_ctx=None, installer_id=None, remotes=()):
        '''
        Parameters
        ----------
        source_directory : str
            Directory where files come from
        bundles_directory : str
            Directory where the bundles files go
        installer_id : str
            Name of this installer for purposes of mutual exclusion. optional
        graph : rdflib.graph.ConjunctiveGraph
            The graph from which we source contexts for this bundle
        default_ctx : str
            The ID of the default context -- the target of a query when not otherwise
            specified. optional
        imports_ctx : str
            The ID of the imports context this installer should use. Imports relationships
            are selected from this graph according to the included contexts. optional
        remotes : iterable of Remote
            Remotes to be used for retrieving dependencies when needed during installation
        '''
        self.context_hash = hashlib.sha224
        self.file_hash = hashlib.sha224
        self.source_directory = source_directory
        self.bundles_directory = bundles_directory
        self.graph = graph
        self.installer_id = installer_id
        self.imports_ctx = imports_ctx
        self.default_ctx = default_ctx
        self.remotes = remotes

    def install(self, descriptor):
        '''
        Given a descriptor, install a bundle

        Parameters
        ----------
        descriptor : Descriptor
            The descriptor for the bundle

        Returns
        -------
            The directory where the bundle is installed
        '''
        # Create the staging directory in the base directory to reduce the chance of
        # moving across file systems
        try:
            staging_directory = bundle_directory(self.bundles_directory, descriptor.id,
                    descriptor.version)
            makedirs(staging_directory)
        except OSError:
            pass

        with lock_file(p(staging_directory, '.lock'), unique_key=self.installer_id):
            try:
                self._install(descriptor, staging_directory)
                return staging_directory
            except Exception:
                self._cleanup_failed_install(staging_directory)
                raise

    def _cleanup_failed_install(self, staging_directory):
        shutil.rmtree(p(staging_directory, 'graphs'))
        shutil.rmtree(p(staging_directory, 'files'))

    def _install(self, descriptor, staging_directory):
        graphs_directory, files_directory = self._set_up_directories(staging_directory)
        self._write_file_hashes(descriptor, files_directory)
        self._write_context_data(descriptor, graphs_directory)
        self._write_manifest(descriptor, staging_directory)

    def _set_up_directories(self, staging_directory):
        graphs_directory = p(staging_directory, 'graphs')
        files_directory = p(staging_directory, 'files')

        try:
            makedirs(graphs_directory)
            makedirs(files_directory)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        return graphs_directory, files_directory

    def _write_file_hashes(self, descriptor, files_directory):
        with open(p(files_directory, 'hashes'), 'wb') as hash_out:
            for fname in _select_files(descriptor, self.source_directory):
                hsh = self.file_hash()
                source_fname = p(self.source_directory, fname)
                with open(source_fname, 'rb') as fh:
                    hash_file(hsh, fh)
                hash_out.write(fname.encode('UTF-8') + b'\x00' + pack('B', hsh.digest_size) + hsh.digest() + b'\n')
                shutil.copy2(source_fname, p(files_directory, fname))

    def _write_manifest(self, descriptor, staging_directory):
        with open(p(staging_directory, 'manifest'), 'wb') as mf:
            if self.default_ctx:
                mf.write(DEFAULT_CONTEXT_KEY.encode('UTF-8') + b'\x00' +
                        self.default_ctx.encode('UTF-8') + b'\n')
            if self.imports_ctx:
                # If an imports context was specified, then we'll need to generate an
                # imports context with the appropriate imports. We don't use the source
                # imports context ID for the bundle's imports context because the bundle
                # imports that we actually need are a subset of the total set of imports
                mf.write(IMPORTS_CONTEXT_KEY.encode('UTF-8') + b'\x00' +
                         fmt_bundle_ctx_id(descriptor.id).encode('UTF-8') + b'\n')
            mf.write(b'version\x00' + pack('Q', descriptor.version) + b'\n')
            mf.flush()

    def _write_context_data(self, descriptor, graphs_directory):
        contexts = _select_contexts(descriptor, self.graph)

        # XXX: Find out what I was planning to do with these imported contexts...adding
        # dependencies or something?
        imports_ctxg = None
        if self.imports_ctx:
            imports_ctxg = self.graph.get_context(self.imports_ctx)

        included_context_ids = set()

        with open(p(graphs_directory, 'hashes'), 'wb') as hash_out,\
                open(p(graphs_directory, 'index'), 'wb') as index_out:
            imported_contexts = set()
            for ctxid, ctxgraph in contexts:
                hsh = self.context_hash()
                temp_fname = p(graphs_directory, 'graph.tmp')
                write_canonical_to_file(ctxgraph, temp_fname)
                with open(temp_fname, 'rb') as ctx_fh:
                    hash_file(hsh, ctx_fh)
                included_context_ids.add(ctxid)
                ctxidb = ctxid.encode('UTF-8')
                # Write hash
                hash_out.write(ctxidb + b'\x00' + pack('B', hsh.digest_size) + hsh.digest() + b'\n')
                gbname = hsh.hexdigest() + '.nt'
                # Write index
                index_out.write(ctxidb + b'\x00' + gbname.encode('UTF-8') + b'\n')

                ctx_file_name = p(graphs_directory, gbname)
                rename(temp_fname, ctx_file_name)

                if imports_ctxg is not None:
                    imported_contexts |= transitive_lookup(imports_ctxg,
                                                           ctxid,
                                                           CONTEXT_IMPORTS,
                                                           seen=imported_contexts)
            uncovered_contexts = imported_contexts - included_context_ids
            uncovered_contexts = self._cover_with_dependencies(uncovered_contexts, descriptor.dependencies)
            if uncovered_contexts:
                raise MissingImports(uncovered_contexts)
            hash_out.flush()
            index_out.flush()

    def _cover_with_dependencies(self, uncovered_contexts, dependencies):
        # TODO: Check for contexts being included in dependencies
        # XXX: Will also need to check for the contexts having a given ID being consistent
        # with each other across dependencies
        for d in dependencies:
            bnd = Bundle(d.id, self.bundles_directory, d.version, remotes=self.remotes)
            for b in bnd.contexts:
                uncovered_contexts.remove(URIRef(b))
                if not uncovered_contexts:
                    break
        return uncovered_contexts


def fmt_bundle_ctx_id(id):
    return 'http://openworm.org/data/generated_imports_ctx?bundle_id=' + urlquote(id)


def hash_file(hsh, fh, blocksize=None):
    '''
    Hash a file in chunks to avoid eating up too much memory at a time
    '''
    if not blocksize:
        blocksize = hsh.block_size << 15
    while True:
        block = fh.read(blocksize)
        if not block:
            break
        hsh.update(block)


class IndexManager(object):
    def __init__(self):
        self.index = None

    def add_entry(self, bundle):
        bundle_descriptor.files


class IndexEntry(object):
    '''
    An index entry.

    Points to the attached files and contexts
    '''

    def __init__(self):
        self.file_refs = set()
        ''' References to files in this bundle '''

        self.context_files = set()
        ''' A list of files in this bundle '''


class FilesDescriptor(object):
    '''
    Descriptor for files
    '''
    def __init__(self):
        self.patterns = set()
        self.includes = set()

    @classmethod
    def make(cls, obj):
        if not obj:
            return
        res = cls()
        res.patterns = set(obj.get('patterns', ()))
        res.includes = set(obj.get('includes', ()))
        return res


def make_pattern(s):
    if s.startswith('rgx:'):
        return RegexURIPattern(s[4:])
    else:
        return GlobURIPattern(s)


def make_include_func(s):
    return URIIncludeFunc(s)


class URIIncludeFunc(object):

    def __init__(self, include):
        self.include = URIRef(include.strip())

    def __hash__(self):
        return hash(self.include)

    def __call__(self, uri):
        return uri == self.include

    def __str__(self):
        return '{}({})'.format(FCN(type(self)), repr(self.include))

    __repr__ = __str__


class URIPattern(object):
    def __init__(self, pattern):
        self._pattern = pattern

    def __hash__(self):
        return hash(self._pattern)

    def __call__(self, uri):
        return False

    def __str__(self):
        return '{}({})'.format(FCN(type(self)), self._pattern)


class RegexURIPattern(URIPattern):
    def __init__(self, pattern):
        super(RegexURIPattern, self).__init__(re.compile(pattern))

    def __call__(self, uri):
        # Cast the pattern match result to a boolean
        return not not self._pattern.match(str(uri))


class GlobURIPattern(RegexURIPattern):
    def __init__(self, pattern):
        replacements = [
            ['*', '.*'],
            ['?', '.?'],
            ['[!', '[^']
        ]

        for a, b in replacements:
            pattern = pattern.replace(a, b)
        super(GlobURIPattern, self).__init__(re.compile(pattern))


def _select_files(descriptor, directory):
    fdescr = descriptor.files
    if not fdescr:
        return
    for f in fdescr.includes:
        if not exists(p(directory, f)):
            raise Exception('Included file in bundle does not exist', f)
        yield f

    for f in fdescr.patterns:
        for match in match_files(directory, p(directory, f)):
            yield relpath(match, directory)


def _select_contexts(descriptor, graph):
    for context in graph.contexts():
        ctx = context.identifier
        for inc in descriptor.includes:
            if inc(ctx):
                yield ctx, context
                break

        for pat in descriptor.patterns:
            if pat(ctx):
                yield ctx, context
                break


LOADER_CLASSES = [
    HTTPBundleLoader
]


class BundleNotFound(Exception):
    def __init__(self, bundle_ident, msg=None, version=None):
        msg = 'Missing bundle "{}"{}{}'.format(bundle_ident,
                '' if version is None else ' at version ' + str(version),
                ': ' + str(msg) if msg is not None else '')
        super(BundleNotFound, self).__init__(msg)


class LoadFailed(Exception):
    def __init__(self, bundle, loader, *args):
        msg = args[0]
        mmsg = 'Failed to load {} bundle with loader {}{}'.format(
                bundle, loader, ': ' + msg if msg else '')
        super(LoadFailed, self).__init__(mmsg, *args[1:])


class InstallFailed(Exception):
    pass


class MissingImports(InstallFailed):
    def __init__(self, imports):
        msg = 'Missing {} imports'.format(len(imports))
        super(MissingImports, self).__init__(msg)
        self.imports = imports


class FetchFailed(Exception):
    ''' Generic message for when a fetch fails '''
    pass


class NoBundleLoader(FetchFailed):
    '''
    Thrown when a loader can't be found for a loader
    '''

    def __init__(self, bundle_name, bundle_version=None):
        super(NoBundleLoader, self).__init__(
            'No loader could be found for "%s"%s' % (bundle_name,
                (' at version ' + bundle_version) if bundle_version is not None else ''))