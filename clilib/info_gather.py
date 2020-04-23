""" Herein, you will find the implementation of splunk diag.

This is a self-contained tool for gathering troubleshoting data from Splunk
installs, so that Splunk Support can skip round trips with customers and be more
effective.

There are secondary user categories among customers themselves for their own
purposes, partners, Splunk core QA & performance, etc.

THE LAWS OF DIAG!

Diag is funny software, not useful for any normal purpose of getting work done,
and only called on when things are broken.  Diag must be self reliant and fail
gracefully.  Diag must handle weird shit.


I write this text to myself as well as others.  I have sinned in the past and I
have paid the price.

RULE 1: DO NOT MODIFY DIAG WITHOUT COMMUNICATION!
        Communicate with the diag maintainer: Joshua Rodman (or his replacement)
        Communicate with the undiag maintainers: Octavio DiSciullo & Louise Tang
                                                 (or their replacments)

RULE 2: Core users may veto.
        The above parties may veto your functionality.  They have to live with
        the changes, and you don't.

RULE 3: No external depdencies!
        Do not add external dependencies to diag.

        * If the entire etc dir is deleted, diag should do its best to still
          work.
        * If the user has arranged for df to hang, diag should do its best to
          still work.
        * If splunkd is replaced with a gif of a dancing bird, diag should do
          its best to still work.
        * NO REALLY!  when 4.1 shipped we ended up with a ton of users who could
          not launch splunkd at all and diag worked just fine and let us help
          them.

RULE 4: Fail gracefully

        * If files that absolutely must be present for splunk to work are
          missing entirely, then whine at the admin and move on.
        * If you run a system utility that is taking a long time.  Kill it and
          move on.
        * If you try to request information from a socket to splunkd that cannot
          be gotten any independent way, note when it has stopped providing
          data, close the socket and move on.

        * If diag is running against any supported version of Splunk, it should
          work! (really!  This is how we address customers with diag bugs.)

RULE 5: THOU SHALT TEST

        * If you are modifying diag, you will run the unit tests, and you will
          run diag yourself on both unix and windows.
        * If your changes are nontrivial, you should add a test to cover them.
          It's not hard!  We're talking probably less than 10 minutes here.

RULE 6: Use names that make sense!

        * People using diag are very stressed out, they don't need more Splunk
          nonsense.
        * I know we called the directory "frozznarb" and the class is called
          "DatabasePartionPolicy", but  describe the output and the command
          switches in PLAIN ENGLISH, example:
              "Gathering state files from the Deployment Server"

RULE 7: Do not make diag big or fat

        * Any data category that could take a long time should probably be off
          by default until we can demonstrate in customer environements that
          it doesn't take a long time.

        * Same for any data category that could occupy a lot of space.
          This is really far worse because the compression is the slowest part
          anyway, and big diags fail on upload to Splunk Support.

        * DO run the python -mprofile feature.

RULE 8: DO NOT HIDE ERRORS

        * Do not throw away stderr.
        * Do not hide system misconfiguration.
        * Do not prevent the display of exceptions
        * Do not except: or I will break your kneecaps.

        When things are broken, troubleshooters NEED ERRORS.

        Diag is easy to fix because it shows you what is broken when it breaks.

        * DO record errors in diag.log
        * DO offer guesses as to how the admin might work around the exception
          you are displaying.
        * DO eat (and log) stderr for commands that are expected to produce
          confusing output in the standard case.


RULE 9: DOCUMENT

        * Document new components in the spec file and on the web
        * If you change the meaning of flags by augmenting components, document
          in the spec file and on the web
        * If any new data categories are added to diag, document on the web.

        * Customers need to have confidence that they know exactly what they're
          sending us, and that they have the power to filter out things they
          deem unsafe.  (More filtering is coming in the future).
          See rule 6.

Rule 10: Do not covet thy neighbor''s ass.

        * HR will make you very unhappy.
        * Get your own donkey.

If you can do all these things reliably you will be a better man than I, but
strive to do them all the same.
-jrod
"""
from __future__ import division

from builtins import object
from splunk.util import cmp
from builtins import range
from builtins import map
from builtins import filter
from builtins import object

import os
import subprocess
import sys
import platform
import string
import shutil
import tarfile
import stat
import logging
import splunk.clilib.cli_common
try:
    import splunk.clilib.log_handlers
except:
    splunk.clilib.log_handlers = None
import datetime
import time # two time modules, zzz
import optparse
import fnmatch
import re
import glob
import socket
import traceback
if sys.version_info >= (3, 0):
    from io import StringIO
    from io import BytesIO
else: # py2 has io.StringIO but it behaves differently, we want the classic version:
    from StringIO import StringIO
import tempfile
from future.moves.urllib import request as urllib_request
from future.moves.urllib import error as urllib_error
from future.moves.urllib import parse as urllib_parse
import threading
import functools
import errno
import itertools
import base64
import getpass
import json
# ssl import can fail when diag is not run with proper splunk env
try:
    import ssl
except:
    ssl = object()
    ssl.SSLError = None
# and some digging into extension modules to enforce forward-compatibility
import inspect


SPLUNK_HOME   = os.environ['SPLUNK_HOME']
SPLUNK_ETC    = os.environ.get('SPLUNK_ETC')
RESULTS_LOC   = os.path.join(SPLUNK_HOME, 'var', 'run', 'splunk', 'diag-temp')
MSINFO_FILE   = 'msinfo-sum.txt'
SYSINFO_FILE  = 'systeminfo.txt'
COMPOSITE_XML = os.path.join(SPLUNK_HOME, 'var', 'run', 'splunk', 'composite.xml')
SSL_CONFIG_STANZA = 'sslConfig'

KNOWN_COMPONENTS = ("index_files",
                    "index_listing",
                    "dispatch",
                    "etc",
                    "log",
                    "searchpeers",
                    "consensus",
                    "conf_replication_summary",
                    "suppression_listing",
                    "rest",
                    "kvstore",
                    "file_validate",
                   )

system_info = None # systeminfo.txt file obj; if you want to use reporting
                   # functions externally, set this as desired.


# the general logger; gets to diag.log & screen
logger = logging
# only goes to diag.log (or a temp file on failure), used for adding additional
# info that isn't helpful onscreen
auxlogger = logging

# These two are initialized to the 'logging' module, so there's some kind of log
# behavior no matter what; but they're really set up in logging_horrorshow()

####################
# pathname overhead
def get_splunk_etc():
    """Since etc-redirection was introduced for common criteria,
       the SPLUNK_ETC env var is the truth for where to find configuration files
       However, diag supports being used against older versions of splunk where
       this may not exist
    """
    if SPLUNK_ETC:
        return SPLUNK_ETC
    return _traditional_splunk_etc()

def _traditional_splunk_etc():
    return os.path.join(SPLUNK_HOME, "etc")

def etc_redirected():
    """Returns True if the user has supplied an alternate SPLUNK_ETC at splunk
       start time.
       Note, we do not care if these two paths are effectively the same.  We're
       simply noting that the value has been adjusted in some regard.

       Thus we do not handle case insensitivity on windows, trailing slashes,
       symbolic links, etc.
    """
    return _traditional_splunk_etc() != get_splunk_etc()


# SPL-34231, horrid (microsoft) hack to support longer filenames --
# normally windows rejects 260+ char paths, unless unicode and
# containing this magic cookie
windows_magic_filepath_cookie = u"\\\\?\\"
def bless_long_path(path):
    if os.name == "nt":
        # SPL-145790 - Extra slashes cause issues with this translation
        # Make double slashes single slashes
        # avoiding the \\computername bit.
        path = path[0] + path[1:].replace("\\\\", "\\")
        return windows_magic_filepath_cookie + path
    return path

# undo jrod's unintended consequence of the above decorated path that
# affects string comparion when looking for file inclusions to the diag
def undecorate_path(path):
    if os.name == "nt":
        if path.startswith(windows_magic_filepath_cookie):
            path = path[len(windows_magic_filepath_cookie):]
    return path

##################
# various cruft around naming the diag file.
#

def get_diag_date_str():
    return datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

def get_splunkinstance_name():
    # Octavio says the hostname is preferred to machine-user, and that
    # multiple-splunks-per-host is now rare, so just use hostname
    return socket.gethostname()

def get_results_dir():
    """Where the output goes for file-copy purposes
       This is still used by the unit tests (but only them) """
    #results_path = os.path.join(RESULTS_LOC, get_diag_name())
    results_path = RESULTS_LOC
    return bless_long_path(results_path)

def format_diag_name(host_part):
    date_str = get_diag_date_str()
    return "diag-%s-%s" % (host_part, date_str)

diag_name = None
def get_diag_name(base=None):
    """Construct the diag's name,
       used both for paths inside and for the containing tarname"""
    # hack to create 'static' value
    global diag_name
    if not diag_name:
        if not base:
            base = get_splunkinstance_name()
        diag_name = format_diag_name(base)
        logger.info('Selected diag name of: ' + diag_name)
    return diag_name

def set_diag_name(name):
    global diag_name
    diag_name = name
    logger.info('Set diag name to: ' + diag_name)

def format_tar_name(basename, compressed=True):
    """ Construct a filename for the diag """
    extension = ".tar"
    if compressed:
        extension += ".gz"
    return basename + extension

def get_tar_pathname(filename=None, compressed=True):
    """ Construct the output pathname for the diag """
    if not filename:
        filename = get_diag_name()
    tar_filename = format_tar_name(filename)

    # TODO: give user control over output dir/path entirely
    return(os.path.join(SPLUNK_HOME, tar_filename))


# These get_x_conf functions exist to cache/avoid repeat calls, but also to
# allow tests to replace them with arbitrary answers

_server_conf = None
def get_server_conf():
    global _server_conf
    if not _server_conf:
        _server_conf = splunk.clilib.cli_common.getMergedConf('server')
    return _server_conf

_indexes_conf = None
def get_indexes_conf():
    global _indexes_conf
    if not _indexes_conf:
        _indexes_conf = splunk.clilib.cli_common.getMergedConf('indexes')
    return _indexes_conf

##################
# Scaffolding for accepting data to add to the diag

class DirectTar(object):
    def __init__(self, compressed=True):
        self.compressed = compressed
        self.stored_dirs = set()
        # stores set of directories so we can force parent dirs to
        # exist in the tarball

    def setup(self, options):
        if options.stdout:
            # start off without compression -- http server code compresses
            sys_stdout = sys.stdout
            if sys.version_info >= (3, 0):
                sys_stdout = sys.stdout.buffer
                # The underlying binary buffer (a BufferedIOBase instance)
            self.tarfile = tarfile.open(get_tar_pathname(compressed=False), 'w|',
                    fileobj=sys_stdout)
        elif not self.compressed:
            # This branch is not live -- should it ever be?
            self.tarfile = tarfile.open(get_tar_pathname(compressed=False), 'w')
        elif False:
            # If we want to do streaming compressed output.. this crap is needed
            # BEGIN MONKEY PATCHING!!!!
            def lower_compression_init_write_gz(self):
                desired_compression = 6
                self.cmp = self.zlib.compressobj(6, self.zlib.DEFLATED,
                        -self.zlib.MAX_WBITS, self.zlib.DEF_MEM_LEVEL, 0)
                timestamp = tarfile.struct.pack("<L", int(time.time()))
                self._Stream__write("\037\213\010\010%s\002\377" % timestamp)
                if isinstance(self.name, unicode):
                    self.name = self.name.encode("iso-8859-1", "replace")
                if self.name.endswith(".gz"):
                    self.name = self.name[:-3]
                self._Stream__write(self.name + tarfile.NUL)
            tarfile._Stream._init_write_gz = lower_compression_init_write_gz
            # END MONKEY PATCHING!!!!
            self.tarfile = tarfile.open(get_tar_pathname(), 'w|gz')
        else:
            # default case
            self.tarfile = tarfile.open(get_tar_pathname(), 'w:gz',  compresslevel=6)
        self._add_empty_named_dir(get_diag_name())

    def _add_empty_named_dir(self, diag_path):
        "Add a directory of a particular name, for tar completeness"
        logger.debug("_add_empty_named_dir(%s)" % diag_path)
        tinfo = tarfile.TarInfo(diag_path)
        tinfo.type = tarfile.DIRTYPE
        tinfo.mtime = time.time()
        tinfo.mode = 0o755 # dir needs x
        self.tarfile.addfile(tinfo)
        self.stored_dirs.add(diag_path)

    def _add_unseen_parents(self, file_path, diag_path):
        """Add all parents of a dir/file of a particular name,
           that are not already in the tar"""
        logger.debug("_add_unseen_parents(%s, %s)" % (file_path, diag_path))
        parents = []
        src_dir = file_path
        tgt_dir = diag_path
        # we are looking for two goals here:
        # 1 - create an entry in the tar so we get sane behavior on unpack
        # 2 - if the source dir is from a file inside splunk_home, the dir
        #     entries should match the permissions, timestamps,
        if file_path.startswith(SPLUNK_HOME):
            while True:
                logger.debug("_add_unseen_parents() -> tgt_dir=%s src_dir=%s", tgt_dir, src_dir)
                prior_tgt_dir = tgt_dir
                prior_src_dir = src_dir
                tgt_dir = os.path.dirname(tgt_dir)
                src_dir = os.path.dirname(src_dir)
                if not tgt_dir or tgt_dir == "/" or tgt_dir == get_diag_name() or tgt_dir == prior_tgt_dir:
                    break
                if not src_dir or src_dir in ("/", "\\") or src_dir == prior_src_dir:
                    # This is here because this case almost certainly represents
                    # a logic bug (one existed at some point)
                    raise Exception("Wtf.  "+
                            "You copied a shorter source dir into a longer target dir? "+
                            "Does this make sense in some universe? "+
                            "args were: file_path=%s diag_path=%s" % (file_path, diag_path))
                if not tgt_dir in self.stored_dirs:
                    parents.append((src_dir, tgt_dir))
            if not parents:
                return
            logger.debug("_add_unseen_parents --> parents:(%s)" % parents)
            parents.reverse() # smallest first
            for src_dir, tgt_dir in parents:
                if os.path.islink(src_dir) or not os.path.isdir(src_dir):
                    # We can't add a non-directory as a parent of a directory
                    # or file, extracting will fail or be unsafe.
                    # it should be a symlink
                    if os.path.islink(src_dir):
                        # XXX change to auxlogger
                        msg = "Encountered symlink: %s -> %s; storing as if plain directory. Found adding parent dirs of %s."
                        logging.warn(msg, src_dir, os.readlink(src_dir), file_path)
                        # for symlinks, we want to get the perm data from the
                        # target, since owner/perm/etc data on a symlink has no
                        # meaning.
                        symlink_target = os.path.realpath(src_dir)
                        if not os.path.exists(symlink_target):
                            logging.error("Link target does not exist(!!)")
                            tarinfo = self.tarfile.gettarinfo(src_dir, arcname=tgt_dir)
                        else:
                            tarinfo = self.tarfile.gettarinfo(symlink_target, arcname=tgt_dir)
                    else:
                        # This should not be a possible branch, but paranoia
                        msg = "Encountered unexpected filetype: %s stat: %s storing as if directory. Found adding parent dirs of %s."
                        logging.warn(msg, src_dir, os.stat(src_dir), file_path)
                        tarinfo = self.tarfile.gettarinfo(src_dir, arcname=tgt_dir)
                    # Either way, pretend it's a directory
                    tarinfo.type = tarfile.DIRTYPE
                    self.tarfile.addfile(tarinfo)
                else:
                    self.tarfile.add(src_dir, arcname=tgt_dir, recursive=False)
                self.stored_dirs.add(tgt_dir)
        else:
            # we're adding a file from outside SPLUNK_HOME.  Probably a temp
            # file.  TODO -- should we enforce something here?
            while True:
                logger.debug("_add_unseen_parents() outside SPLUNK_HOME -> tgt_dir=%s", tgt_dir)
                prior_tgt_dir = tgt_dir
                tgt_dir = os.path.dirname(tgt_dir)
                if not tgt_dir or tgt_dir == "/" or tgt_dir == get_diag_name() or tgt_dir == prior_tgt_dir:
                    break
                if not tgt_dir in self.stored_dirs:
                    parents.append(tgt_dir)
            if not parents:
                return
            logger.debug("_add_unseen_parents --> parents:(%s)" % parents)
            parents.reverse() # smallest first
            for dir in parents:
                self._add_empty_named_dir(tgt_dir)


    def complete(self, *ignore):
        self.tarfile.close()

    def add(self, file_path, diag_path):
        logger.debug("add(%s)" % file_path)
        if diag_path in self.stored_dirs:
            # nothing to do
            return
        try:
            self._add_unseen_parents(file_path, diag_path)
            if os.path.isdir(file_path):
                self.stored_dirs.add(diag_path)
            self.tarfile.add(file_path, diag_path, recursive=False)
        except IOError as e:
            # report fail, but continue along
            err_msg = "Error adding file '%s' to diag, failed with error '%s', continuing..."
            logger.warn(err_msg % (file_path, e))
            pass

    def add_fileobj(self, fileobj, diag_path):
        logger.debug("add_fileobj(%s)" % diag_path)
        if os.name == "nt":
            # Tar only supports /; we handle changing to / here because:
            # 1 - dir implementation would want \
            # 2 - tarfile.add already seamlessly handles this for add and add_dir
            diag_path = diag_path.replace("\\", "/")

        # tarfile imposes the need to handle stringio files completely
        # differently from real files :-(
        if hasattr(fileobj, 'is_stringio') and fileobj.is_stringio:
            tinfo = tarfile.TarInfo(diag_path)
            tinfo.size = fileobj.size()
            tinfo.mtime = fileobj.mtime()
            self.tarfile.addfile(tinfo, fileobj=fileobj)
        else:
            # gettarinfo is very insistent that it finds out the name itself for a fileobj
            fileobj.name = diag_path
            tinfo = self.tarfile.gettarinfo(arcname=diag_path, fileobj=fileobj)
            self.tarfile.addfile(tinfo, fileobj=fileobj)

    def add_dir(self, dir_path, diag_path, ignore=None):
        logger.debug("add_dir(%s)" % dir_path)
        adder = functools.partial(add_file_to_diag, add_diag_name=False)
        collect_tree(dir_path, diag_path, adder, ignore=ignore)

    def add_string(self, s, diag_path):
        if os.name == "nt":
            # Tar only supports /; we handle changing to / here because:
            # 1 - dir implementation would want \
            # 2 - tarfile.add already seamlessly handles this for add and add_dir
            diag_path = diag_path.replace("\\", "/")
        tinfo = tarfile.TarInfo(diag_path)
        tinfo.size = len(s)
        tinfo.mtime = time.time()
        # tarfile requires a file for adding.. sigh
        if sys.version_info >= (3, 0):
            if isinstance(s, str):
                s = s.encode()
            s_file = BytesIO(s)
        else:
            s_file = StringIO(s)
        self.tarfile.addfile(tinfo, fileobj=s_file)
        s_file.close()

    def add_fake_file(self, file_path, diag_path):
        logger.debug("add_fake_file(%s) %s" % (file_path, diag_path))

        tinfo = tarfile.TarInfo(name=diag_path)

        statres = os.lstat(file_path)
        tinfo.mode = statres.st_mode
        tinfo.uid = statres.st_uid
        tinfo.gid = statres.st_gid
        tinfo.mtime = statres.st_mtime
        tinfo.size = 0
        tinfo.type = tarfile.REGTYPE
        self.tarfile.addfile(tinfo)

class OutputDir(object):
    def __init__(self):
        pass

    def setup(self, options):
        options = None # not used
        try:
            logger.info("Ensuring clean temp dir...")
            # delete any old results tarball, and create a fresh results directory
            clean_temp_files()
        except Exception as e:
            logger.error("""Couldn't initally clean diag temp directory.
    To work around move %s aside, or delete it.
    A listing of the entire contents of this directory will help us identify the problem.""" % get_results_dir())
            raise

        output_dir = os.path.join(get_results_dir(), get_diag_name())
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

    def complete(self, options, log_buffer):
        create_tar(options, log_buffer)

    def add(self, file_path, diag_path):
        logger.debug("add(%s, %s)" % (file_path, diag_path))
        target_path = os.path.join(get_results_dir(), diag_path)

        if os.path.isdir(file_path):
            if not os.path.exists(target_path):
                os.makedirs(target_path)
            try:
                shutil.copystat(file_path, target_path)
            except OSError as why:
                if WindowsError is not None and isinstance(why, WindowsError):
                    # Copying file access times may fail on Windows
                    pass
                else:
                    raise
        else:
            copy_file_with_parent(file_path, target_path)

    def add_fileobj(self, fileobj, diag_path):
        logger.debug("add_fileobj(%s)" % (diag_path))
        target_path = os.path.join(get_results_dir(), diag_path)

        with open(target_path, "wb") as write_f:
            while True:
                data = fileobj.read()
                if not data:
                    break
                if sys.version_info >= (3, 0) and isinstance(data, str):
                    data = data.encode()
                write_f.write(data)

    def add_dir(self, dir_path, diag_path, ignore=None):
        logger.info("add_dir(%s)" % dir_path)
        target_path = os.path.join(get_results_dir(), diag_path)
        #logger.debug("OutputDir: add_dir %s -> %s" % (dir_path, target_path))
        collect_tree(dir_path, target_path, self.add, ignore=ignore)

    def add_string(self, string, diag_path):
        target_path = os.path.join(get_results_dir(), diag_path)
        parent_dir = os.path.dirname(target_path)
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
        with open(target_path, "wb") as f:
            if sys.version_info >= (3, 0) and isinstance(string, str):
                string = string.encode()
            f.write(string)

storage = None
def set_storage(style):
    global storage
    if style == "directory":
        storage = OutputDir()
    elif style == "tar":
        storage = DirectTar()
    else:
        raise "WTF"

_storage_filters = []
def set_storage_filters(filter_list):
    global _storage_filters
    _storage_filters = filter_list

def path_unwanted(filename):
    for regex in _storage_filters:
        if regex.match(filename):
            return True
    return False

def is_special_file(path):
    """Determines if a path is something unsual, ie, something other than a
       file, dir or symbolic link. Caller will have to ask again for a link
       target

       If file/dir/symlink, returns False
       Otherwise returns "socket", "device" or "fifo"

       Can throw an exception on stat,
       or if it's a type of file I never heard of"""
    logger.debug('is_special_file(%s)' % path)
    f_stat = os.lstat(path)
    if stat.S_ISLNK(f_stat.st_mode):
        return False
    elif stat.S_ISDIR(f_stat.st_mode) or stat.S_ISREG(f_stat.st_mode):
        return False
    elif stat.S_ISSOCK(f_stat.st_mode):
        return "socket"
    elif stat.S_ISCHR(f_stat.st_mode) or stat.S_ISBLK(f_stat.st_mode):
        return "device"
    elif stat.S_ISFIFO(f_stat.st_mode):
        return "fifo"
    else:
        raise AssertionError("stat(%s) was not any known kind of file" % path)

def add_fake_special_file_to_diag(file_path, diag_path, special_type):
    "Add a hinty, zero-byte file that suggests there was a special file"
    name = "%s.%s" % (diag_path, special_type)
    storage.add_fake_file(file_path, name)

# These are requests -- exclusion filtering happens transparently in here.
def add_file_to_diag(file_path, diag_path, add_diag_name=True):
    """Add a single file: file_path points to file on disk,
                          diag_path says where to store in the tar"""
    logger.debug("add_file_to_diag(%s, %s)" % (file_path, diag_path))

    # all files live in the diag prefix
    if add_diag_name:
        diag_path = os.path.join(get_diag_name(), diag_path)

    if path_unwanted(diag_path):
        add_excluded_file(diag_path)
        return

    if wants_filter(diag_path):
        add_filtered_file_to_diag(file_path, diag_path)
        return

    # We don't put in block devices, sockets, etc in the tar.
    # No booby-traps
    special_type = is_special_file(file_path)
    if special_type:
        add_fake_special_file_to_diag(file_path, diag_path, special_type)
        return

    storage.add(file_path, diag_path)

def add_filtered_file_to_diag(file_path, diag_path):
    """ Add the contents of a readable object (like an open file) to the
        diag at diag_path """
    filterclass = get_filter(diag_path)
    with open(file_path, "rb") as f:
        filtering_object = filterclass(f)

        storage.add_fileobj(filtering_object, diag_path)
        filtering_object.log_stats(diag_path)
        filtering_object.close()

def add_string_to_diag(content, diag_path):
    """ Write contents of a string into the diag at diag_path """
    logger.debug("add_string_to_diag(%s)" % diag_path)
    # all files live in the diag prefix
    diag_path = os.path.join(get_diag_name(), diag_path)
    if path_unwanted(diag_path):
        add_excluded_file(diag_path)
        return
    storage.add_string(content, diag_path)

def add_dir_to_diag(dir_path, diag_path, ignore=None):
    """Add a single file: dir_path points to dir on disk,
                          diag_path says where to store it (and its children) in the tar"""
    logger.debug("add_dir_to_diag(%s, %s)" % (dir_path, diag_path))

    # all files live in the diag prefix
    diag_path = os.path.join(get_diag_name(), diag_path)

    if path_unwanted(diag_path):
        add_excluded_file(diag_path)
        return
    # should never be a special file, but this is called infrequently
    # so go ahead and be paranoid
    special_type = is_special_file(dir_path)
    if special_type:
        msg = "attempted to add a special file '%s' of type %s.  Ignoring."
        logger.error(msg % (dir_path, special_type))
        return
    storage.add_dir(dir_path, diag_path, ignore=ignore)

##################
# UP/Down test
#
_splunkd_running = None
def splunk_running():
    "Is splunk up right now?"

    global _splunkd_running
    if _splunkd_running == None:
        logger.info("Testing to see if splunkd is running right now...")

        splunk_program_path = os.path.join(SPLUNK_HOME, 'bin', 'splunk')
        if os.name == "nt":
            # paranoia
            splunk_program_path = splunk_program_path + ".exe"
        return_code = subprocess.call([splunk_program_path, "status"],
                                      stdout=open(os.devnull, "w"))
        # shell has inverted success/fail values from programming
        if return_code == 0:
            logger.info("  splunkd is running.")
            _splunkd_running = True
        else:
            logger.info("  splunkd is not running.")
            _splunkd_running = False

    return _splunkd_running


##################
# REST!
# TODO - It would be preferable to simply rely on splunk.rest  / splunk.auth
#        However, those apis cannot recover from stalls currently which are
#        pretty likely in diagnostic situations
""" Just some convenient wrappers for some global info """
# session key for local splunkd
_session_key = None
# session key for cluster master
_cm_key =  None
def get_session_key(cm=False):
    "retreive current session key for local splunkd, or cluster master if cm=True"
    if cm:
        return _cm_key
    return _session_key

def set_session_key(key, cm=False):
    "store current session key for local splunkd, or cluster master if cm=True"
    if cm:
        global _cm_key
        _cm_key = key
    else:
        global _session_key
        _session_key = key

rest_needed = False
def _will_need_rest():
    global rest_needed
    rest_needed = True

def close_it(f):
    """ Crappy helper to force read blocking urllib to timeout by closing the
    file its using after a time. """
    f.close()

def cli_get_sessionkey(url):
    """Returns a cached sessionkey for a given url, if one is available."""
    launcher_path = os.path.join(SPLUNK_HOME, 'bin', 'splunk')
    if os.name == "nt":
        launcher_path += ".exe"

    cmd_args = [launcher_path, "_authtoken", url]
    p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE)
    out, _ = p.communicate()
    if sys.version_info >= (3, 0):
        out = out.decode()
    if p.returncode != 0:
        # failure!
        return None
    return out.strip()

def getSSLContext():
    """
    Grab settings from server.conf sslConfig stanza
    :return: ssl.SSLContext to use for http requests
    """
    try:
        import splunk.ssl_context

        server_conf = get_server_conf()
        sslconfig_stanza = server_conf.get(SSL_CONFIG_STANZA, {})
        sslHelper = splunk.ssl_context.SSLHelper()
        return sslHelper.createSSLContextFromSettings(
            sslConfJSON=sslconfig_stanza,
            serverConfJSON=sslconfig_stanza,
            isClientContext=True)
    except Exception as e:
        logging.debug("Could not create ssl.SSLContext. Error was %s" % e)
        return None

def loginresponse_parse(text):
    """Find the actual session key in the response from splunkd"""
    import xml.dom.minidom

    doc_tree = xml.dom.minidom.parseString(text)
    session_key_elements = doc_tree.getElementsByTagName('sessionKey')
    # there's only one of these of course...
    session_key_element = session_key_elements[0]
    return session_key_element.firstChild.toxml()

def ping(protocol_host_port, cm=False):
    """Tries to determine if a session key that we already have enables us to
    talk to the running service.
    By default checks local splunkd.
    Checks the cluster master if cm=True"""
    key = get_session_key(cm)
    if not key:
        return False
    ping_uri = protocol_host_port + '/services'
    req = urllib_request.Request(ping_uri)
    req.add_header("Authorization", "Splunk %s" % key)
    try:
        try:
            urllib_request.urlopen(req, timeout=5, context=getSSLContext()) # ignore return value
        except TypeError as e:
            urllib_request.urlopen(req, timeout=5) # ignore return value
    except urllib_error.HTTPError as error:
        msg = "When hitting /services for status, got error %s" % (error,)
        logger.error(msg) # XXX should become debug when confidence arises.
        return False
    return True


def read_rest_endpoint(url, diag_path, cm=False):
    """Reads the contents of a splunkd url, and adds it the diag at the desired
    path.
    Accesses local splunkd by default, or cluster master if cm=True

    On success, returns bytes added to the diag.

    Does not take any responsibility to check if splunkd is running etc.
    """
    #results_temp_output_path = os.path.join(get_results_dir(), diag_path)
    #write_dir = os.path.dirname(results_temp_output_path)
    #if not os.path.exists(write_dir):
    #    os.makedirs(write_dir)
    tmpfilename = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix='splunkdiag_rest', delete=False) as results_output_file:
            tmpfilename = results_output_file.name
            req = urllib_request.Request(url)
            req.add_header("Authorization", "Splunk %s" % get_session_key(cm))
            try:
                try:
                    rest_f = urllib_request.urlopen(req, timeout=5, context=getSSLContext())
                except TypeError as e:
                    rest_f = urllib_request.urlopen(req, timeout=5)
                threading.Timer(10, close_it, [rest_f])
                while True:
                    chunk = rest_f.read(1024 * 64)
                    if not chunk:
                        break
                    results_output_file.write(chunk)
            except urllib_error.HTTPError as error:
                msg_str = ("Couldn't retreive REST endpoint %s, got %s " +
                           "-- if reasonable, attempt gathering this data manually (curl, browser, etc.)")
                msg = msg_str % (url, error)
                logger.warn(msg)
                results_output_file.write(msg)
    finally:
        if tmpfilename and os.path.exists(tmpfilename):
            file_size = os.stat(tmpfilename).st_size
            try:
                add_file_to_diag(tmpfilename, diag_path)
            finally:
                os.unlink(tmpfilename)
            return file_size

def process_rest_list(rest_list, protocol_host_port, cm=False):
    """ Collect a set of rest documents as directed by a datastructure of
      'friendly name', 'url', 'filename to put it into'

      Accesses local splunkd by default, or cluster master if cm=True
      """
    for entry in rest_list:
        name, resource, filename = entry
        url = protocol_host_port + resource
        # octavio wants this visible by default.
        logger.info("     REST-collecting %s ..." % name)
        read_rest_endpoint(url, os.path.join("rest-collection", filename), cm)

def gather_cluster_rest_content(mode, protocol_host_port):
    """ Cluster-specific REST data gathering."""
    master_rest_paths = (
        ("Master streaming fixups",
            "services/cluster/master/fixup?count=0&level=streaming",
            "cluster/master-streaming-fixups.xml"),
        ("Master data_safety fixups",
            "services/cluster/master/fixup?count=0&level=data_safety",
            "cluster/master-data_safety-fixups.xml"),
        ("Master generation fixups",
            "services/cluster/master/fixup?count=0&level=generation",
            "cluster/master-generation-fixups.xml"),
        ("Master replication_factor fixups",
            "services/cluster/master/fixup?count=0&level=replication_factor",
            "cluster/master-replication_factor-fixups.xml"),
        ("Master search_factor fixups",
            "services/cluster/master/fixup?count=0&level=search_factor",
            "cluster/master-search_factor-fixups.xml"),
        ("Master checksum_sync fixups",
            "services/cluster/master/fixup?count=0&level=checksum_sync",
            "cluster/master-checksum_sync-fixups.xml"),
        ("Master peer-list",
            "services/cluster/master/peers?count=0",
            "cluster/master-peer_list.xml"),
        ("Master indexes",
            "services/cluster/master/indexes",
            "cluster/master-indexes.xml"),
        ("Master generation",
            "services/cluster/master/generation",
            "cluster/master-generation.xml"),
        ("Master info",
            "services/cluster/master/info",
            "cluster/master-info.xml"),
        ("Master replications",
            "services/cluster/master/replications",
            "cluster/master-replications.xml"),
        ("Master buckets",
            "services/cluster/master/buckets?count=10000",
            "cluster/master-buckets.xml"),
        ("Master noprimary nofrozen",
            "services/cluster/master/buckets?filter=has_primary=false&filter=frozen=false",
            "cluster/master-buckets_noprimary_nofrozen.xml"),
    )
    slave_rest_paths = (
        ("Slave buckets", "services/cluster/slave/buckets?count=0", "cluster/slave-buckets.xml"),
        ("Slave info", "services/cluster/slave/info", "cluster/slave-info.xml"),
    )
    if mode == 'searchhead':
        return # told not to do anything for this case yet
    if mode == 'slave':
        logger.info("This appears to be a cluster slave, gathering cluster REST info.")
        # get the slave paths
        process_rest_list(slave_rest_paths, protocol_host_port)

        # TODO - should probably get this data from REST
        server_conf = get_server_conf()
        clustering_stanza = server_conf.get('clustering', {})
        protocol_host_port = clustering_stanza.get('master_uri') # not a uri!
        process_rest_list(master_rest_paths, protocol_host_port, cm=True)
    elif mode == 'master':
        logger.info("This appears to be a cluster master, gathering cluster REST info.")
        # get the slave paths? TODO
        process_rest_list(master_rest_paths, protocol_host_port)
    else:
        logger.error("unknown cluster mode %s" % mode)

def interactive_restsession_setup(protocol_host_port, clustering):
    """ get username & password from user, use them to generate
        a valid session with the splunkd behind protocol_host_port

        Optionally, if clustering == "slave", then we also set up a session with
        the cluster-master.
        """
    user = None
    pword = None
    params = None

    have_valid_sessionkey = False
    password_retries = 3

    # We can enter this path a second time for clustering, in which case we skip
    # setting up communicating with the local system
    if not clustering and not local_splunkd_protocolhostport:
        user = input("  Splunk username: ")
        user = user.rstrip("\r")

        for i in range(password_retries):
            if i == 0:
                pword = getpass.getpass("  Splunk password: ")
            else:
                pword = getpass.getpass("  retype Splunk password (typo?): ")

            # verify our credentials
            logger.info("  Logging into local system")

            params = urllib_parse.urlencode({'username':user, 'password':pword})
            #params = urllib.urlencode({'username':'admin', 'password':'changeme'})

            if type(params) is str:
                params = params.encode()

            url = protocol_host_port + "/services/auth/login"
            try:
                try:
                    resp = urllib_request.urlopen(url, params, timeout=5, context=getSSLContext())
                except TypeError as e:
                    resp = urllib_request.urlopen(url, params, timeout=5)

                text = resp.read()
                key = loginresponse_parse(text)
                set_session_key(key)
                have_valid_sessionkey = True
                break # success! exit loop
            except urllib_error.HTTPError as error:
                msg = "  Couldn't log in via REST endpoint, got %s" % (error)
                logger.warn(msg)

        if not have_valid_sessionkey:
            return False

    if clustering == "slave":
        have_valid_cluster_sessionkey = False

        server_conf = get_server_conf()
        clustering_stanza = server_conf.get('clustering', {})
        protocol_host_port = clustering_stanza.get('master_uri') # not a uri!
        url = protocol_host_port + "/services/auth/login"
        for i in range(password_retries):
            if i == 0:
                user = input("  Cluster-master Splunk username: ")
                user = user.rstrip("\r")
                pword = getpass.getpass("  Cluster-master Splunk password: ")
            elif i == 1 or i == 2:
                pword = getpass.getpass("  re-type Cluster-master Splunk password (typo?): ")
                msg = "  Failed to login to Cluster master"
                logger.warn(msg)
                # cluster login failed, while local auth worked, so maybe
                # the logins are different

            params = urllib_parse.urlencode({'username':user, 'password':pword})

            try:
                try:
                    resp = urllib_request.urlopen(url, params, timeout=5, context=getSSLContext())
                except TypeError as e:
                    resp = urllib_request.urlopen(url, params, timeout=5)
                text = resp.read()
                key = loginresponse_parse(text)
                set_session_key(key, cm=True)
                have_valid_cluster_sessionkey = True
                break
            except urllib_error.HTTPError as error:
                msg = "  Couldn't log into cluster master via REST endpoint, got %s" % (error)
                logger.warn(msg)

        if not have_valid_cluster_sessionkey:
            return False

    return True


def calculate_local_splunkd_protocolhostport():
    # Determine Host+port; stored in one place
    web_conf = splunk.clilib.cli_common.getMergedConf('web')
    settings_stanza = web_conf.get('settings', {})
    mgmtHostPort = settings_stanza.get('mgmtHostPort')
    if not mgmtHostPort:
        logger.warn("mgmtHostPort setting missing in web.conf?? Will try 127.0.0.1:8089.")
        mgmtHostPort = "127.0.0.1:8089"

    # Determine http vs https from a totally unreated place (woohoo)
    server_conf = get_server_conf()
    ssl_stanza = server_conf.get('sslConfig', {})
    ssl_enabled_setting = ssl_stanza.get('enableSplunkdSSL')
    if not ssl_enabled_setting:
        logger.error("enableSplunkdSSL setting missing in server.conf?? Will assume true.")
        ssl_enabled_setting = "true"

    # I'm pretty sure I'm writing yet another boolean normalizer bug here :-(
    if ssl_enabled_setting.lower().strip() in ('true', 't', '1', 'yes'):
        protocol = 'https'
    else:
        protocol = 'http'
    # Now we can construct a base url
    return "%s://%s" % (protocol, mgmtHostPort)

local_splunkd_protocolhostport = None
def prepare_local_rest_access(options):
    """ Set up for REST access of local splunk instance.
        Determines the port & protocol, and sets up a session for access

        Returns: true if we have a working session
                 false if we for any reason do not.
    """
    if not splunk_running():
        return False

    global local_splunkd_protocolhostport
    if local_splunkd_protocolhostport:
        logger.error("Incorrect attempt made to setup local rest access a second time.")
        return False

    protocol_host_port = calculate_local_splunkd_protocolhostport()
    #Okay, now, can we talk to it?

    key = get_session_key()
    if key and ping(protocol_host_port):
        logger.info("  User provided session key validated")
    else:

        if key:
            logger.info("  Falling back to alternate auth methods due to invalid user provided session key")

        #If there happens to be a session key on the filesystem already..
        key = cli_get_sessionkey(protocol_host_port)
        if key:
            # remember this for later implicit use by all rest requests including
            # the just-below ping()
            set_session_key(key)

        if key and ping(protocol_host_port):
            logger.info("  Already logged into local splunkd.")

        elif options.nologin:
            logger.info("  Skipping interactive login because --nologin option used")
            return False

        # we dont' have a valid local splunkd login, so we ask the user to type in
        # stuff..
        else:
            logger.info("  Not logged in.  Please log in now.  (You can avoid this logging in ahead with 'splunk login' or using the --nologin option which will disable REST gathering.")
            logged_in = interactive_restsession_setup(protocol_host_port, clustering=False)
            if not logged_in:
                # give up
                return False

    # everything worked out, so record the calculated access path to splunkd
    local_splunkd_protocolhostport = protocol_host_port
    return True


def gather_rest_content(options):
    """ Implementation of REST gathering data category."""
    options = None # not used currently

    if not splunk_running():
        logger.info("Skipping REST gathering, because splunk is not running.")
        return

    if not local_splunkd_protocolhostport:
        logger.info("Skipping REST gathering, because we were unable to acquire a rest session.")
        return

    logger.info("Gathering REST info...")

    # if we're a cluster member, do cluster stuff. TODO - should probably get from REST
    server_conf = get_server_conf()
    clustering_stanza = server_conf.get('clustering', {})
    cluster_mode = clustering_stanza.get('mode')
    cluster = cluster_mode in ('master', 'slave', 'searchhead')

    if cluster:
        if not interactive_restsession_setup(local_splunkd_protocolhostport, clustering=cluster_mode):
            return

    diag_rest_paths = (
            ("File monitor input", "/services/data/inputs/monitor?count=0", "input_monitor.xml"),
            ("Monitor input file status",
                "/services/admin/inputstatus/TailingProcessor%3AFileStatus?count=0", "input_monitor.xml"),
            #("Search Jobs", "/servicesNS/-/-/search/jobs", "search_jobs.xml"),
            # We decided search jobs is of marginal utility & expensive
            ("Server info", "/services/server/info?count=0", "server_info.xml"),
            ("Server messages", "/services/messages?count=0", "server_messages.xml"),
            ("Licenser messages", "/services/licenser/messages?count=0", "licenser_messages.xml"),
            ("Cachemanager metrics", "/services/admin/cacheman/_metrics", "cachemanager_metrics.xml"),
    )
    process_rest_list(diag_rest_paths, local_splunkd_protocolhostport)

    if cluster:
        gather_cluster_rest_content(cluster_mode, local_splunkd_protocolhostport)
    logger.info("  REST info gathering complete")


##################
# functions to gather data
#
def systemUsername():
    if os.name == 'posix':
        import pwd
        # get the name for the UID for the current process
        username = pwd.getpwuid(os.getuid())[0]
    elif os.name == 'nt':
        # thanks internets -- http://timgolden.me.uk/python/win32_how_do_i/get-the-owner-of-a-file.html`
        #pylint: disable=F0401
        import win32api
        import win32con
        username = win32api.GetUserNameEx(win32con.NameSamCompatible)
    else:
        username = 'unknown for platform:' + os.name
    system_info.write('diag launched by: %s\n' % username)
    system_info.write('\n')

def get_env_info():
    home_info = "SPLUNK_HOME: %s\n" % SPLUNK_HOME
    if etc_redirected():
        etc_info = "SPLUNK_ETC: redirected to %s\n" % get_splunk_etc()
    else:
        etc_info = "SPLUNK_ETC: not redirected\n\n"
    return home_info + etc_info


def splunkVersion():
    """ Use splunk version to figure the version"""

    system_info.write('********** Splunk Version **********\n\n')
    exit_code, output = simplerunner(["splunk", "version"], timeout=5)
    if output:
        system_info.write(output)

# this uses python's uname function to get info in a cross-platform way.
def systemUname():
    """ Python uname output """

    system_info.write('\n\n********** Uname **********\n\n')
    suname = platform.uname()[:]
    system_info.write(str(suname))
    #SPL-18413
    system_info.write("\n")
    system_info.write('\n\n********** splunkd binary format **********\n\n')
    splunkdpath = os.path.join(SPLUNK_HOME, 'bin', 'splunkd')
    if suname[0] == 'Windows':
        splunkdpath += ".exe"
    arch = str(platform.architecture(splunkdpath))
    system_info.write(arch)
    if suname[0] == 'Linux':
        system_info.write('\n\n********** Linux distribution info **********\n\n')

        cmd_args = ['lsb_release', '-a']
        system_info.write("running: %s\n" % " ".join(cmd_args))

        lsbinfo_opener = functools.partial(subprocess.Popen, cmd_args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
        lsbinfo_desc = "lsb_release run for more detailed distribution version info"
        lsbinfo_runner = PopenRunner(lsbinfo_opener, description=lsbinfo_desc)
        lsbinfo_exit_code = lsbinfo_runner.runcmd()
        if lsbinfo_runner.stdout:
            output=lsbinfo_runner.stdout
        elif lsbinfo_runner.traceback:
            output=lsbinfo_runner.traceback
        else:
            output="trouble running lsb_release -- oddly with no exception captured"
        system_info.write(output + '\n')

def networkConfig():
    """ Network configuration  """

    system_info.write('\n\n********** Network Config  **********\n\n')
    # we call different utilities for windows and "unix".
    if os.name == "posix":
        # if running as a non-root user, you may not have ifconfig in your path.
        # we'll attempt to guess where it is, and if we can't find it, just
        # assume that it is somewhere in your path.
        ifconfig_exe = '/sbin/ifconfig'
        if not os.path.exists(ifconfig_exe):
            ifconfig_exe = 'ifconfig'
        exit_code, output = simplerunner([ifconfig_exe, "-a"], timeout=3)
    else:
        exit_code, output = simplerunner(["ipconfig", "/all"], timeout=3)
    if output:
        system_info.write(output)

def get_process_listing_unix():
    lines = []
    lines.append("********** Process Listing (ps)  **********")
    lines.append("")

    suname = platform.uname()
    if suname[0] in ('Darwin', 'Linux', 'FreeBSD'):
        # systems known to support BSD-ps
        cmd = ["ps", "aux"]
    else:
        # the drek
        cmd = ["ps", "-elf"]
    lines.append("running: %s" % " ".join(cmd))
    exit_code, output = simplerunner(cmd, timeout=3)
    if output:
        output_lines = output.split("\n")
        splunk_lines = [line for line in output_lines if "splunk" in line]
    lines.extend(splunk_lines)
    return "\n".join(lines)

def get_process_listing_windows():
    lines = []
    lines.append("********** Process Listing (tasklist) of splunkd.exe  **********")
    lines.append("")
    cmd = ["tasklist", "/V", # verbose
           "/FI", # filter
           'IMAGENAME eq splunkd.exe', # for splunkd.exe procesess
    ]
    lines.append("running: %s" % " ".join(cmd))
    opener = functools.partial(subprocess.Popen, cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False)
    description = "Tasklist, windows process lister"
    runner = PopenRunner(opener, description)
    exit_code = runner.runcmd(timeout=10) # windows is slow

    if runner.stdout:
        output_lines = runner.stdout.splitlines()
    else:
        output_lines = []

    if exit_code != 0:
        s1 = "Tasklist returned failure, exit code %s; diag may lack running splunkd.exe information." % exit_code
        lines.append(s1)
        logger.warn(s1)
        lines.append("error output...")
        err_lines = runner.stderr.strip().splitlines()
        lines.extend(err_lines)
        for line in err_lines:
            logger.warn("  tasklist errortext: %s", line)
        if output_lines:
            lines.append("non-err output follows:")

    if output_lines:
        lines.extend(output_lines)
    else:
        lines.append("No output recevied")

    # wedging this "bonus" in here for now.
    lines.append("")
    lines.append("********** Service info (for splunkd) **********")

    svc_cmd = ["sc", "qc", "splunkd"] # windows is so user-friendly
    lines.append("running: %s" % " ".join(svc_cmd))
    exit_code, output = simplerunner(svc_cmd, timeout=10) #windows is slow
    if output:
        output_lines = output.split("\n")
        lines.extend(output_lines)
    else:
        lines.append("No output recevied")
    return "\n".join(lines)

def get_process_listing():
    """ what programs are running? """
    if os.name == "nt":
        return get_process_listing_windows()
    else:
        return get_process_listing_unix()

def get_file_validation():
    """ Return information about files modified or not as compared to the
        install media """

    header = "********** Installed File Correctness (splunk validate files)  **********"
    output_chunks = ["", header, ""]

    splunk_exe = os.path.join(SPLUNK_HOME, "bin", "splunk")
    if os.name == "nt":
        splunk_exe += ".exe"

    cmd_args = [splunk_exe, "validate", "files"]
    output_chunks.append("running: " + " ".join(cmd_args))

    p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, shell=False)

    out, _ = p.communicate()
    if sys.version_info >= (3, 0):
        out = out.decode()
    output_chunks.append(out)

    if p.returncode == 0:
        output_chunks.append("Command returned OK code.")
    else:
        output_chunks.append("Command returned NOT-OK code.")
    return "\n".join(output_chunks)


def networkStat():
    """ Network Status """

    system_info.write('\n\n********** Network Status **********\n\n')
    # just like with ifconfig, we attempt to guess the path of netstat.
    # if we can't find it, we leave it up to it being in your path.
    # also, if we're on windows, just go with the path-less command.
    netstat_exe = '/bin/netstat'
    if not os.name == "posix" or not os.path.exists(netstat_exe):
        netstat_exe = 'netstat'
    # special-case linux to get linux specific processname info
    if sys.platform.startswith("linux"):
        exit_code, output = simplerunner([netstat_exe, "-a", "-n", "-p"], timeout=3)
    else:
        exit_code, output = simplerunner([netstat_exe, "-a", "-n"], timeout=3)
    system_info.write(output)

def get_msinfo():
    """ Gather a huge dump of ms diagnostic info.
        Takes forever, so moved into a separate function to turn it off """
    msinfo_temp = tempfile.NamedTemporaryFile(prefix="splunk_msinfo", delete=False)
    msinfo_temp.close()
    temp_filename = msinfo_temp.name
    try:
        # this takes quite a while
        msinfo_cmd = ["msinfo32.exe",
                      "/report", temp_filename,
                      "/categories", "+SystemSummary"]
        exit_code, output = simplerunner(msinfo_cmd, timeout=180)
        try:
            add_file_to_diag(temp_filename, MSINFO_FILE)
        except IOError as e:
            # user probably clicked cancel on the msinfo gui
            err_msg = "Couldn't copy windows system info file to diag. %s"
            logger.warn(err_msg % (e.strerror,))
    finally:
        if os.path.exists(temp_filename):
            os.unlink(temp_filename)


def systemResources():
    """ System Memory """

    # on windows, we use msinfo to get all the relevant output.
    if os.name == "posix":
        system_info.write('\n\n********** System Ulimit **********\n\n')
        exit_code, output = simplerunner(["/bin/sh", "-c", "ulimit -a"], timeout=1)
        system_info.write(output)
        system_info.write('\n\n********** System Memory **********\n\n')
        suname = platform.uname()
        #SPL-17593
        if suname[0] == 'SunOS':
            # is it worth converting this? i am le tired.
            system_info.write(os.popen('/usr/sbin/prtconf | head -3').read())
        elif suname[0] == 'Darwin':
            exit_code, output = simplerunner(["vm_stat"], timeout=1)
        elif suname[0] == 'Linux':
            exit_code, output = simplerunner(["free"], timeout=1)
            system_info.write(output)
        else:
            # try vmstat for hpux, aix, etc
            exit_code, output = simplerunner(["vmstat"], timeout=2)
            if output:
                system_info.write(output)
        system_info.write('\n\n********** DF output **********\n\n')
        exit_code, output = simplerunner(["df"], timeout=2)
        system_info.write(output)
        system_info.write('\n')

        system_info.write('also df -i\n')
        exit_code, output = simplerunner(["df", "-i"], timeout=2)
        system_info.write(output)

        system_info.write('\n\n********** mount output **********\n\n')
        exit_code, output = simplerunner(["mount"], timeout=2)
        system_info.write(output)
        system_info.write('\n\n********** cpu info **********\n\n')
        if suname[0] == 'SunOS':
            exit_code, output = simplerunner(["/usr/sbin/psrinfo", "-v"], timeout=2)
        elif suname[0] == 'Darwin':
            # how long does a system_profiler take?
            exit_code, output = simplerunner(["system_profiler", "SPHardwareDataType"], timeout=10)
        elif suname[0] == 'Linux':
            if os.path.exists('/proc/cpuinfo'):
                with open('/proc/cpuinfo') as cpuinfo:
                    output = cpuinfo.read()
            else:
                output = "/proc/cpuinfo unavailable. no /proc mounted?\n"
        elif suname[0] == 'AIX':
            aix_horror = """ for processor in `lsdev -c processor | awk '{ print $1; }'` ; do
                                echo $processor;
                                lsattr -E -l $processor;
                             done """
            # for this horror, we have to actually run the shell.  Poopies.
            # TODO use a processgroup with setsid and killpg
            aix_opener = functools.partial(subprocess.Popen, aix_horror,
                                       stdout=subprocess.PIPE, shell=True)
            aix_desc = "Overly complicated AIX cpu info fetcher hairball"
            aix_runner = PopenRunner(aix_opener, description=aix_desc)
            aix_exit_code = aix_runner.runcmd(timeout=15)
            if aix_exit_code != 0:
                logger.warn("non-zero exit code from the cpu info fetcher..  :-(")
                logger.warn("Please let us know if there are interesting errors.")
            if aix_runner.stdout:
                output=aix_runner.stdout
            else:
                output="trouble running the aix_horror\n"
        elif suname[0] == 'FreeBSD':
            # shell here too; linux  systcl has patternmatching, but not FreeBSD
            freebsd_blob = "sysctl -a | egrep -i 'hw.machine|hw.model|hw.ncpu'"
            freebsd_opener = functools.partial(subprocess.Popen, freebsd_blob,
                                               stdout=subprocess.PIPE, shell=True)
            freebsd_desc = "'sysctl -a' with egrep postfilter"
            freebsd_runner = PopenRunner(freebsd_opener, description=freebsd_desc)
            freebsd_runner.runcmd(timeout=2)
            output = freebsd_runner.stdout
            if not output:
                output = "trouble running the freebsd sysctl groveller\n"
        elif suname[0] == 'HP-UX':
            cmd_input = "selclass qualifier cpu;info;wait;infolog\n"
            exit_code, output = simplerunner(["cstm"],
                                             description="hpux cpu info command",
                                             timeout=15,
                                             input=cmd_input)
            if exit_code != 0:
                logger.warn("non-zero exit code from the hpux cpu info fetcher..  :-(")
                logger.warn("Please let us know if there are interesting errors.")
            if not output:
                output = "trouble running the hpux cpu info command\n"
        else:
            output = "access to cpu data not known for this platform.\n"
        system_info.write(output)

    else:
        get_msinfo()

def kvStoreListing():
    """ KV Store Listing Output"""

    server_conf  = get_server_conf()
    stanza = server_conf.get('kvstore')
    if not stanza:
        msg = " No [kvstore] stanza found in server.conf.  Is this splunk 6.1 or earlier?"
        logger.info(msg)
        logger.info("Skipping KV Store listings...")
        return
    kvpath = normalize_path(stanza['dbPath'])

    logger.info("Getting KV Store listings...")
    system_info.write('\n\n********** kvstore **********\n\n')
    if not os.path.exists(kvpath):
        msg = "kvstore directory (%s) not found" % kvpath
        system_info.write(msg +"\n")
        logger.warn(msg)
        return

    if os.name == "posix":
        cmd = ["ls", "-alR", kvpath]
        system_info.write(" ".join(cmd) + "\n")
        exit_code, output = simplerunner(cmd, timeout=120)
        if output:
            system_info.write(output)
    else:
        # if/when converted to subprocess, will need to run via shell
        system_info.write("dir /s/a \"%s\"\n" % kvpath)
        system_info.write(os.popen("dir /s/a \"%s\"" % kvpath).read())

    return

def splunkDBListing(options):
    """ Index Listing Output"""
    system_info.write('\n\n********** find **********\n')

    if options.index_listing == 'none':
        system_info.write("index_files diag option set to 'none', so there is no listing.\n")
        return

    db_paths = splunkDBPaths()

    bucket_keys = (
      ('homePath',        'location of hot & warm index buckets'),
      ('coldPath',        'location of cold index buckets'),
      ('thawedPath',      'restore point for archived index buckets'),
    )
    summary_keys = (
      ('tstatsHomePath',  'datamodel acceleration storage (tstats)'),
      ('summaryHomePath', 'report acceleration storage (TSUM)'),
    )

    # merely informational
    if options.index_listing == "light":
        system_info.write("index_files diag option set to 'light_listing', only showing contents of hot buckets.\n")
    else:
        system_info.write("showing full index content listing.\n")

    for index_name, path_set in db_paths.items():
        system_info.write("\nIndex listing for index: %s\n\n" % index_name)

        if options.index_listing == "light":
            # TODO: integrate these with get_dir_listing()
            if os.name == "posix":
                dir_cmd = ["ls", "-al"]
                recursive_dir_cmd = ["ls", "-alR"]
                start_shell = False
            else:
                dir_cmd = ["dir", "/a"]
                recursive_dir_cmd = ["dir", "/s/a"]
                start_shell = True

            for key_name, key_description in bucket_keys:
                system_info.write("---for %s, listing %s: %s.\n\n" % (index_name, key_name, key_description))
                db_path = path_set[key_name]
                # db_path can be None
                if not db_path:
                    system_info.write("No path could be determined for this setting.\n")
                    continue # no reason to do ls/dir

                cmd_args = dir_cmd + [db_path]
                as_string = " ".join(cmd_args)
                system_info.write(as_string + "\n")

                p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     shell=start_shell)
                out, err = p.communicate()
                if sys.version_info >= (3, 0):
                    out = out.decode()
                    err = err.decode()

                system_info.write(out + "\n")
                if err:
                    system_info.write("Command listing command returned error text!\n")
                    system_info.write(err + "\n")

                # Get contents of hot dirs for stuff like throttling!
                try:
                    entries = os.listdir(db_path)
                except OSError as e:
                    template = "Could not get entries for directory '%s' referenced by index configuration: got system error: %s\n"
                    msg = template % (db_path, e)
                    system_info.write(msg)
                    logger.warn(msg)
                    continue
                hot_entries = [s for s in entries if s.startswith("hot_")]
                for hot in hot_entries:
                    hot_path = os.path.join(db_path, hot)
                    if not os.path.isdir(hot_path):
                        continue
                    cmd_args = recursive_dir_cmd + [hot_path]
                    p = subprocess.Popen(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         shell=start_shell)

                    as_string = " ".join(cmd_args)
                    system_info.write(as_string + "\n")

                    out, err = p.communicate()
                    if sys.version_info >= (3, 0):
                        out = out.decode()
                        err = err.decode()

                    system_info.write(out + "\n")
                    if err:
                        system_info.write("Command listing command returned error text!\n")
                        system_info.write(err + "\n")
        else:  # this is the 'full' listing
            for key_name, key_description in bucket_keys:
                system_info.write("---for %s, listing %s: %s.\n\n" % (index_name, key_name, key_description))
                db_path = path_set[key_name]

                # db_path can be None
                if not db_path:
                    system_info.write("No path could be determined for this setting.\n")
                    continue # no reason to do ls/dir

                if os.name == "posix":
                    # get a recursive listing of that path.
                    system_info.write('\nls -alR "%s"\n' % db_path)
                    system_info.write(os.popen('ls -alR "%s"' % db_path).read())
                else:
                    system_info.write(os.popen('dir /s/a "%s"' % db_path).read())

        # and now we list the 'summary' locations, independently.
        # split because they're always a recursive listing
        for key_name, key_description in summary_keys:
            system_info.write("---for %s, listing %s: %s.\n\n" % (index_name, key_name, key_description))
            db_path = path_set[key_name]

            # db_path can be None
            if not db_path:
                system_info.write("No path could be determined for this setting.\n")
                continue # no reason to do ls/dir

            # summary dir will often not exist
            if key_name == "summaryHomePath" and not os.path.exists(db_path):
                system_info.write("Report acceleration dir was not created.\n")
                system_info.write("This is normal when none have run yet.\n")
                continue # no reason to do ls/dir

            if os.name == "posix":
                # get a recursive listing of that path.
                system_info.write('\nls -alR "%s"\n' % db_path)
                system_info.write(os.popen('ls -alR "%s"' % db_path).read())
            else:
                system_info.write(os.popen('dir /s/a "%s"' % db_path).read())

def get_a_manifest(db_path, diag_base_path, manifest_filename):
    "Condensation of the below collectors"
    file_path = os.path.join(db_path, manifest_filename)
    if os.path.exists(file_path):
        diag_path = os.path.join(diag_base_path, manifest_filename)
        add_file_to_diag(file_path, diag_path)

def get_bucketmanifest_files(db_path, diag_base_path):
    """return relative paths for the .bucketManifest file under a given path (hopefully a SPLUNK_DB or index)
     reqested by vishal, SPL-31499"""
    get_a_manifest(db_path, diag_base_path, ".bucketManifest")

def get_manifest_csv_files(db_path, diag_base_path):
    """return relative path for the manifest.csv file under a given path;
       this file should only exist inside a volume summary (tsum or tstats summary)
       reqested by igor, SPL-91173"""
    get_a_manifest(db_path, diag_base_path, "manifest.csv")

def get_worddata_files(db_path, diag_base_path):
    """add Host/Source/SourceTypes .data to the diag
    db_path:   a filesystem location from which to collect the files
    diag_path: where to store them inside the diag"""
    logger.debug("get_worddata_files: db_path=%s diag_base_path=%s" % (db_path, diag_base_path))

    wanted_filenames = ("Hosts.data", "Sources.data", "SourceTypes.data")

    for dir, subdirs, files in os.walk(db_path):
        # skip over dirs that are short lived and not considered desired data
        if dir.endswith("-tmp") or dir.endswith("-inflight"):
            continue
        index_relative_path = os.path.relpath(dir, db_path)
        logger.debug("db_path=%s dir=%s index_relative_path=%s" % (db_path, dir,
            index_relative_path))
        for filename in files:
            if filename in wanted_filenames:
                file_path = os.path.join(db_path, dir, filename)
                diag_path = os.path.join(diag_base_path, index_relative_path, filename)
                add_file_to_diag(file_path, diag_path)

def get_cachemanager_local_files(db_path, diag_base_path):
    """cachemanager_local.json to the diag per bucketj
    db_path:   a filesystem location from which to collect the files
    diag_path: where to store them inside the diag"""
    logger.debug("get_cachemanager_local_files: db_path=%s diag_base_path=%s" % (db_path, diag_base_path))

    for bucket in os.listdir(db_path):
        get_a_manifest(os.path.join(db_path, bucket), os.path.join(diag_base_path, bucket), "cachemanager_local.json")

def copy_dispatch_dir():
     dispatch_dir = os.path.join(SPLUNK_HOME, "var", "run", "splunk", "dispatch")
     dispatch_dir = bless_long_path(dispatch_dir)

     # collect info iff dispatch dir is present
     if not os.path.exists(dispatch_dir):
         return

     listing = get_dir_listing(dispatch_dir)
     add_string_to_diag(listing, os.path.join("dispatch", "dir_info.txt"))

     for job in os.listdir(dispatch_dir):
         try:
             job_dir = os.path.join(dispatch_dir, job)
             if not os.path.isdir(job_dir):
                continue

             listing = get_recursive_dir_listing(job_dir)
             add_string_to_diag(listing, os.path.join("dispatch", job, "dir_info.txt"))
             # copy only files in the job's dir, skip all the .gz stuff
             for f in os.listdir(job_dir):
                src_file = os.path.join(job_dir, f)
                # don't capture the customer's data, or zero byte status indicator files.
                if f.endswith('.gz') or f.startswith('results') or f.endswith(".token"):
                    continue
                if os.path.isdir(src_file):
                    # don't collect subdirs, could be big, wont' be informative.
                    continue
                add_file_to_diag(src_file, os.path.join("dispatch", job, f))
         except OSError as e:
             # report fail, but continue along
             err_msg = "Error capturing data for dispatch job dir '%s', %s, continuing..."
             logger.warn(err_msg % (job_dir, e.strerror))
             pass

def copy_raft_dir():
    "Something something files for search head clustering.  See ewoo/anirbahn"
    raft_dir = os.path.join(SPLUNK_HOME, "var", "run", "splunk", "_raft")
    raft_dir = bless_long_path(raft_dir)

    if not os.path.exists(raft_dir):
        return

    try:
        add_dir_to_diag(raft_dir, "_raft")
    except shutil.Error as copy_errors:
        msg = "Some problems were encountered copying consensus files (likely these are not a problem):\n" + str(copy_errors)
        logger.warn(msg)

def copy_win_checkpoints():
    "get the checkpoint files used by windows inputs"
    p_storage = os.path.join("var", "lib", "splunk", "persistentStorage")
    checkpoint_files = ["wmi_checkpoint", "regmon-checkpoint"]
    checkpoint_dirs = ["WinEventLog", "ADmon"]

    for c_file in checkpoint_files:
        c_path = os.path.join(p_storage, c_file)
        src = os.path.join(SPLUNK_HOME, c_path)
        if os.path.exists(src):
            add_file_to_diag(src, c_path)

    for c_dir in checkpoint_dirs:
        c_dir_path = os.path.join(p_storage, c_dir)
        src = os.path.join(SPLUNK_HOME, c_dir_path)
        if os.path.exists(src):
            try:
                add_dir_to_diag(src, c_dir_path)
            except shutil.Error as copy_errors:
                msg = "Some problems were encountered copying windows checkpoint files for '%s' :%s"
                logger.warn(msg % (c_dir, str(copy_errors)))

def copy_indexfiles(level="full"):
    "copy index files such as .data and .bucketmanifest in all indices to the results dir"

    # nothing was wanted, so do nothing.
    if level in ("none", "light_listing"):
        return

    indexdata_filenames = []
    index_diag_pairs = []

    for path_set in splunkDBPaths().values():
        # summary locations we get the manifests
        for key_name in ['tstatsHomePath', 'summaryHomePath']:
            summary_path = path_set.get(key_name)
            if summary_path and level in ("manifests", "manifest", "full"):
                diag_path = transform_index_path_to_cute_relative_path(summary_path)
                get_manifest_csv_files(summary_path, diag_path)

        # bucket paths we process below
        for key_name in ['homePath', 'coldPath', 'thawedPath']:
            db_path = path_set.get(key_name)
            # paths can be None
            if not db_path:
                continue
            if not os.path.isabs(db_path):
                errmsg = ("Received relative path back from splunkDBPaths()" +
                          " '%s' -- please file a bug. :-(")
                logger.error(errmsg % db_path)
                continue
            diag_path = transform_index_path_to_cute_relative_path(db_path)
            pair = (db_path, diag_path)
            index_diag_pairs.append(pair)
            del db_path # name clash paranoia

    for db_path, diag_path in index_diag_pairs:
        logger.debug("in copy_indexfiles() diag pair: %s, %s" % (db_path, diag_path))
        if level == "full":
            get_worddata_files(db_path, diag_path)
            get_cachemanager_local_files(db_path, diag_path)
        if level in ("manifests", "manifest", "full"):
            # manifests is a subset
            get_bucketmanifest_files(db_path, diag_path)
        else:
            raise Exception("Invalid value for index_level: %s" % level)

def warn_if_logcfg_has_odd_paths():
    """ Some customers have moved their logfiles via log.cfg modifications.
        I guess they hate things to work.
        SPL-76976"""
    # only check the splunkd main log config files
    log_cfg_files = [
            os.path.join(get_splunk_etc(), "log.cfg"),
            os.path.join(get_splunk_etc(), "log-local.cfg"),
    ]
    log_path = os.sep.join(["${SPLUNK_HOME}", "var", "log", "splunk"])
    # vainstein needless inconsistency wastes my time again.
    introspection_path = os.sep.join(["${SPLUNK_HOME}", "var", "log", "introspection"])
    watchdog_path = os.sep.join(["${SPLUNK_HOME}", "var", "log", "watchdog"])

    msg1 = "Unusual path identified in logging configuration in '%s', line: %s"
    msg2 = "It is likely the diag will not have complete logs."
    for filename in log_cfg_files:
        if not os.path.isfile(filename):
            continue
        with open(filename) as f:
            for line in f:
                if not "fileName=" in line:
                    continue
                if log_path not in line and introspection_path not in line and watchdog_path not in line:
                    logger.warn(msg1, filename, line.strip())
                    logger.warn(msg2)

def copy_logs(options):
    """ copy contents of var/log/splunk
        However, we dont' copy files over log_age days old
        (see localopt log-age for default)
    """
    warn_if_logcfg_has_odd_paths()

    # Weird implementation is to pass back the entries we DO NOT want; see python's shutil
    def ignore_filter(dir, entries, all_dumps=options.all_dumps, age_cutoff=options.log_age):
        age_cutoff_seconds = age_cutoff * 60 * 60 * 24 # days -> seconds
        do_not_want = set()
        for ent in entries:
            if not all_dumps and ent.endswith(".dmp"):
                logger.debug('filtered out file for being a dump:' + ent)
                do_not_want.add(ent)
                continue
            elif options.log_age:
                path = os.path.join(dir, ent)
                if not os.path.isfile(path):
                    continue
                path_mtime = os.path.getmtime(path)
                age = time.time() - path_mtime
                # if files are in future, get them anyway
                if age < age_cutoff_seconds:
                    continue
                logger.debug('filtered out file for being old:' + ent)
                do_not_want.add(ent)
        return do_not_want

    size_filt = make_sizefilter(options.log_filesize_limit)
    combined_filter = make_filterchain(ignore_filter, size_filt)

    try:
        logdir = os.path.join(SPLUNK_HOME, "var", "log", "splunk")
        diag_dir = "log"
        add_dir_to_diag(logdir, diag_dir, ignore = combined_filter)
        if not options.all_dumps:
            # get a list of the dmp files, if any exist
            dumps = glob.glob(os.path.join(logdir, "*.dmp"))
            useful_dumps = filter_dumps(dumps)
            for dump in useful_dumps:
                add_file_to_diag(dump, diag_dir)
                #shutil.copy(dump, targetdir)
    except shutil.Error as copy_errors:
        # log files might be rotated, I suppose?
        msg = "Some problems were encountered copying log files:\n" + str(copy_errors)
        logger.warn(msg)

    try:
        introspection_logdir = os.path.join(SPLUNK_HOME, 'var', 'log', 'introspection')
        if not os.path.isdir(introspection_logdir):
            logger.warn("No instrospection logdir found, continuing...")
            return
        add_dir_to_diag(introspection_logdir, "introspection", ignore= size_filt)
    except shutil.Error as copy_errors:
        msg = 'Problems copying introspection logs:\n\t' + str(copy_errors)
        logger.warn(msg)

    try:
        watchdog_logdir = os.path.join(SPLUNK_HOME, 'var', 'log', 'watchdog')
        if not os.path.isdir(watchdog_logdir):
            logger.warn("No watchdog logdir found, continuing...")
            return
        add_dir_to_diag(watchdog_logdir, "watchdog", ignore= size_filt)
    except shutil.Error as copy_errors:
        msg = 'Problems copying watchdog logs:\n\t' + str(copy_errors)
        logger.warn(msg)



def copy_etc(options):
    lookup_filt = make_lookupfilter(options.include_lookups, options.default_lookup_includes)
    size_filt = make_sizefilter(options.etc_filesize_limit)

    etc_dir = bless_long_path(get_splunk_etc())

    etc_passwd_path = os.path.join(etc_dir, "passwd")
    etc_passwd_glob = etc_passwd_path + "*" # etc/passwd* so .bak or ~ or w/e

    # First, add the passwd file (and possible backups) manually via a special
    # filtering function.
    etc_passwd_paths = glob.glob(etc_passwd_glob)
    for pwd_path in etc_passwd_paths:
        sanitized_text = filter_passwd(pwd_path)
        base = os.path.basename(pwd_path)
        add_string_to_diag(sanitized_text, "etc/" + base)

    # and then the other files in etc
    passwd_filt = make_pathfilter(etc_passwd_glob)

    etc_filt = make_filterchain(sslcertfilter, passwd_filt, lookup_filt, size_filt)
    try:
        add_dir_to_diag(etc_dir, "etc", ignore=etc_filt)

    except shutil.Error as copy_errors:
        # If files get moved or whatever during copy (like someone is moving
        # manager) this can happen
        msg = "Some problems were encountered copying etc/... (likely these are not a problem):\n" + str(copy_errors)
        logger.warn(msg)

    log_detected_certs()

def copy_scripts():
    """ added to make running btool on diags easier, but ultimately the approach
    didn't work"""
    return # XXX do we need this anymore? questionable
    try:
        src = os.path.join(SPLUNK_HOME, "share", "splunk", "diag", "scripts")
        add_dir_to_diag(src, "scripts")

    except shutil.Error as copy_errors:
        # log files might be rotated, I suppose?
        msg = "Some problems were encountered copying scripts:\n" + str(copy_errors)
        logger.warn(msg)

def copy_manifests():
    """Add the package-provided files-manifests, , e.g.
    splunk-6.0.3-203567-Linux-x86_64-manifest or manifest.txt on windows"""
    logger.info("Adding manifest files...")
    try:
        if os.name == "nt":
            manifests = glob.glob(os.path.join(SPLUNK_HOME, "manifest.txt"))
        else:
            manifests = glob.glob(os.path.join(SPLUNK_HOME, "*manifest"))
            for manifest in manifests:
                add_file_to_diag(manifest, os.path.basename(manifest))
    except IOError as e:
        logger.error("Failed to copy splunk manifeste files to diag.  Permissions may be wrong.")

def copy_cachemanager_upload():
    """Add var/run/splunk/cachemanger_upload.json"""
    logger.info("Adding cachemanager_upload.json...")
    try:
        cachemanager_upload_json = "cachemanager_upload.json"
        cachemanager_upload_json_path = os.path.join(SPLUNK_HOME, "var", "run", "splunk", cachemanager_upload_json)
        # Dumping it at the root for lack of a better place to put it.
        if os.path.exists(cachemanager_upload_json_path):
            add_file_to_diag(cachemanager_upload_json_path, cachemanager_upload_json)
    except IOError as e:
        logger.error("Failed to copy splunk manifest files to diag.  Permissions may be wrong.")

###################
# Data filtering

class FilteredFile(object):
    """Present a modified version of a file.  Operates in a
    non-streaming manner (stores modified data in memory or a temp file).

    This permits the size of the file to change, which FilteredStream
    cannot due to limitations in the tarfile module & format.
    """

    max_mem_size = 10 * 1024 * 1024 # 10MB

    def __init__(self, readable_obj):
        self.readable_obj = readable_obj
        self._setup()

        if sys.version_info >= (3, 0):
            self.mem_file = BytesIO()
        else:
            self.mem_file = StringIO()
        self.write_file = self.mem_file
        self.is_stringio = True

        self._process_file()
        self.mem_file.seek(0) # rewind to start


    def _process_file(self):
        """Subclass, and write function to bring data from readable_obj
        to self.write() """
        raise NotImplementedError("FilteredFile classes must implement a filter")

    def _setup(self):
        pass

    def log_stats(self, diag_path):
        "No stats for these by default; output should be obvious"
        pass

    def size(self):
        if self.is_stringio:
            return len(self.write_file.getvalue())
        else:
            return os.fstat(self.write_file.fileno()).st_size

    def mtime(self):
        return os.fstat(self.readable_obj.fileno()).st_mtime

    def fileno(self):
        return self.write_file.fileno()

    def read(self, size=-1):
        return self.write_file.read(size)

    def handle_overflow(self):
        if self.mem_file is None:
            return
        if self.write_file.tell() > self.max_mem_size:
            # move the data from memory to a tempfile
            disk_file = tempfile.TemporaryFile(prefix="splunk_diag_filt")

            blocksize = 1024 * 1024
            self.mem_file.seek(0) # rewind to start
            buf = self.mem_file.read(blocksize)
            while buf:
                disk_file.write(buf)
                buf = self.mem_file.read(blocksize)
            del buf
            self.is_stringio = False
            self.write_file = disk_file
            self.mem_file.close()
            self.mem_file = None

    def close(self):
        self.write_file.close()

    def write(self, s):
        if sys.version_info >= (3, 0) and isinstance(s, str):
            s = s.encode()
        self.write_file.write(s)

class FilteredTelemetryConf(FilteredFile):
    def _process_file(self):
        for line in self.readable_obj:
            if re.match(b'^\s*telemetrySalt\s*=', line):
                self.write(b"telemetrySalt=DIAG_REDACTED_TELEMETRY_SALT\n")
            else:
                self.write(line)

class FilteredConf(FilteredFile):

    password_fields_by_conftype = {}

    @classmethod
    def add_password_field(cls, conf_type, setting):
        logger.debug("Filtered_Conf, add_password_field: conf: '%s' setting: '%s'", conf_type, setting)
        password_settings = cls.password_fields_by_conftype.get(conf_type, [])
        if sys.version_info >= (3, 0):
            setting = setting.encode()
        password_settings.append(setting)
        cls.password_fields_by_conftype[conf_type] = password_settings

    def _process_file(self):
        self.redact_count = 0

        fname = self.readable_obj.name
        fname = os.path.basename(fname)
        conf_type, ignore = fname.split('.', 1)

        redact_settings = self.password_fields_by_conftype.get(conf_type)
        if not redact_settings:
            msg = "File (%s) of type (%s) passed to FilteredConf it doesn't know how to filter."
            raise NotImplementedError(msg % (fname, conf_type))

        linecount = 0
        for line in self.readable_obj:
            linecount += 1
            if b'=' in line:
                setting, rest = line.split(b'=', 1)
                if setting.strip() in redact_settings:
                    if not rest.strip():
                        # Line looks like
                        #    setting =
                        # nothing to redact
                        replacement = rest
                        logger.debug("conf category: %s not redacting: %s, blank",
                                     conf_type, str(setting))
                    elif b'$' in rest:
                        logger.debug("conf category: %s redacting: %s as encrypted",
                                     conf_type, str(setting))
                        replacement = b"DIAG_REDACTED_ENCRYPTED_PASSWORD\n"
                        self.redact_count += 1
                    else:
                        logger.debug("conf category: %s redacting: %s as plaintext",
                                     conf_type, str(setting))
                        replacement = b"DIAG_REDACTED_PLAINTEXT_PASSWORD\n"
                        self.redact_count += 1
                    line = setting + b'=' + replacement
            self.write(line)
            if linecount >= 1000:
                self.handle_overflow()
                linecount = -99999999999 # don't do it again (harmless but wasteful)

        def log_stats(self, diag_path):
            if self.redact_count:
                # drop diag-name from path
                ignore, rootless_filename = diag_path.split(os.path.sep, 1)

                auxlogger.info("While filtering %s, redacted %s password values.",
                               rootless_filename, self.redact_count)

class FilteredStream(object):
    """Modify a stream of data as it is being read.  Used to perform streaming
    filtering & tar addition of data

    NOTE: this approach is mostly limited to producing a byte stream that is the
    same size as the original byte stream.  tarfile needs to know the size of a
    file up-front to generate correct headers, and it will read the quantity of
    bytes from the file or pseudo-file that we tell it.  If the byte stream is
    smaller, tarfile will kick out an exception; and if it is larger not all
    bytes will be added to the tar.

    Future research: what happens to the tar datastream in regards to validity
    when a file is short?

    Requires:
        readable_obj: the file to read from
        read_func: a function which acts as read(), pulling data from the arg readable_obj
    """
    def __init__(self, readable_obj):
        self.remaining_bytes = b""
        self.readable_obj = readable_obj
        self._setup()

    def _setup(self):
        pass

    def filter_line(self, line):
        raise NotImplementedError("FilteredStream classes must implement a filter")

    def fileno(self):
        return self.readable_obj.fileno()

    def read(self, size=-1):
        if size == 0:
            # Strange, but okay, here's your zero bytes.
            return b""
        elif size == -1:
            # default to 16KB
            target_size = 16 * 1024
        else:
            target_size = size

        # for now, assume all filtering will be line-by-line; if we need
        # chunk-by-chunk at some point, split that out.

        # we already have enough bytes, return them keeping the remainder
        if len(self.remaining_bytes) >= target_size:
            result = self.remaining_bytes[:target_size]
            self.remaining_bytes = self.remaining_bytes[target_size:]
            return result

        # else we need to read until we have enough.
        unfiltered_bytes_read = 0
        bytes_read = 0
        bytes_to_read = target_size - len(self.remaining_bytes)

        # if we're at EOF, loop short-circuits
        lines_this_time = []
        for line in self.readable_obj:
            unfiltered_bytes_read += len(line)
            newline = self.filter_line(line)

            bytes_read += len(newline)
            lines_this_time.append(newline)

            if bytes_read >= bytes_to_read:
                break

        # bytes left from prior + newly read bytes in one buf
        tmp_buf = self.remaining_bytes + b''.join(lines_this_time)

        # handles the case of target_size > len(tmp_buf)
        self.remaining_bytes = tmp_buf[target_size:]

        return tmp_buf[:target_size]

    def close(self):
        self.readable_obj.close()

class FilteredAuditLog(FilteredStream):
    search_string_regex = re.compile(b"\ssearch='(.*)?',\sautojoin=")

    # set up by add_regex(); contains ("name", ".*a_regex.*", check_luhn)
    content_regexes = {}


    non_digits = re.compile(br"[^0-9]+")

    @classmethod
    def add_regex(cls, name, pattern, check_luhn=False):
        if sys.version_info >= (3, 0):
            name = name.encode()
            pattern = pattern.encode()
        entry = (pattern, check_luhn)
        cls.content_regexes[name] = entry

    _digit_map = { b'0':0, b'1':1, b'2':2, b'3':3, b'4':4, b'5':5, b'6':6, b'7':7, b'8':8, b'9':9 }
    def _luhn_verify(self, digits_s):
        """ If passed a sequence of string digits, determines if the set of
        numbers satisfies the luhn algorithm, the typical checksum used for
        payment card identifiers to avoid single-digit errors and some
        transpositions.

        Here we use it to sniff a number for being a financial id rather than a
        random large number. """
        even_digit = False
        digit_sum = 0

        for str_digit in reversed(digits_s):
            # perhaps surprisingly, a map lookup is a lot faster than a
            # conversion
            digit = self._digit_map[str_digit]

            if even_digit:
                digit *= 2
                if digit > 9:
                    digit -= 9
            digit_sum += digit
            even_digit = not even_digit
        return (digit_sum % 10) == 0


    def _setup(self):
        """ Constructs a regex similar to:
            (?P<name1>pattern1)|(?P<name2>pattern2)|(?P<name3>pattern3) ...
        """
        pattern_list = []
        hit_counter = {}
        for name, entry in self.content_regexes.items():
            base_pattern = entry[0]
            augmented_pattern = b"(?P<%s>%s)" % (name, base_pattern)
            pattern_list.append(augmented_pattern)

            # and init the stats
            hit_counter[name] = 0

        self.hit_counter = hit_counter

        combined_pattern = b"|".join(pattern_list)
        self.search_element_filter_regex = re.compile(combined_pattern)
        logger.debug("Set up a seach_element_filter_regex of: %s", str(combined_pattern))


    def has_searchstring(self, line):
        """Determines if line has a search string, if so returns re match
           If not, returns None
        """
        if not b"action=search" in line:
            return None
        return self.search_string_regex.search(line)

    def filter_line(self, line):
        logger.debug("audit filter_line called")
        # all search lines include this action

        search_match = self.has_searchstring(line)
        if not search_match:
            return line

        # group 1 is search string
        search_string = search_match.group(1)
        modified_search_string = search_string
        for match in self.search_element_filter_regex.finditer(search_string):
            # remove the nonmatches
            match_item = {k:v for k, v in match.groupdict().items() if v is not None}
            if len(match_item) != 1:
                logger.error("While filtering a search string, multiple regexes matched at once, unexpectedly: %s", match_item)
                return line
            do_replacement = True
            group_name, match_value = list(match_item.items())[0]

            if sys.version_info >= (3, 0):
                group_name = group_name.encode()
            if self.content_regexes[group_name][1]:
                # regex requested a luhn check.. so
                # drop all but digits
                digits_only = self.non_digits.sub(match_value, b'')
                really_paycard = self._luhn_verify(digits_only)
                if not really_paycard:
                    do_replacement = False

            if do_replacement:
                if sys.version_info >= (3, 0):
                    group_name_match = group_name.decode()
                else:
                    group_name_match = group_name
                replacement_text = b"X" * len(match_value)
                modified_search_string = (
                        modified_search_string[:match.start(group_name_match)] +
                        replacement_text +
                        modified_search_string[match.end(group_name_match):]
                )

                # record stats
                self.hit_counter[group_name] += 1

        modified_line = (line[:search_match.start(1)] +
                         modified_search_string +
                         line[search_match.end(1):])

        # this case shouldn't happen anymore, but in case we change our mind
        # about how to hide values (eg hide length)
        if len(modified_line) < len(line):
            padding = b" " * (len(line) - len(modified_line))
            modified_line = modified_line.rstrip(b'\n') + padding + b"\n"

        return modified_line

    def log_stats(self, filename):
        rootless_filename = filename.split(os.path.sep, 1)[1]

        log_bits = ["%s: %s" % (k, v) for k, v in self.hit_counter.items() if v != 0]
        joined_bits = ", ".join(log_bits)
        if not joined_bits:
            auxlogger.info("While filtering %s, no rules found hits.",
                           rootless_filename)
        else:
            auxlogger.info("While filtering %s, rule hit counts: %s",
                           rootless_filename, joined_bits)

class FilteredRemoteSearchLog(FilteredAuditLog):
    search_string_regex = re.compile(b"\ssearch='(.*)?',\ssavedsearch_name=\"")

    def has_searchstring(self, line):
        """Determines if line has a search string, if so returns re match
           If not, returns None
        """
        if not ((b"search starting:" in line) or
                (b"connection terminated:" in line)):
            return None
        return self.search_string_regex.search(line)

enabled_filters = []
# list of ("filtertype", "value for filtertype", filter Object)

def _setup_filters_passwords():
    server_conf = get_server_conf()
    shcluster_stanza = server_conf.get('shclustering')
    if not shcluster_stanza:
        msg = "Could not find shclustering stanza in server.conf, will not be able to auto-redact passwords"
        logger.error(msg)
        return
    field_encrypt_info = shcluster_stanza.get('encrypt_fields')
    if not field_encrypt_info:
        msg = "Cannot read 'encrypt_fields' setting from shclustering stanza in server.conf, will not be able to auto-redact passwords"
        logger.error(msg)
        return
    encrypt_field_items = field_encrypt_info.split(",")
    logger.debug("encrypt_field_items: %s", encrypt_field_items)
    conf_types = set()
    try:
        for item in encrypt_field_items:
            conf_type, stanza, setting = item.strip(' "').split(":")
            del stanza # currently ignoring the stanza to simplify parsing
            FilteredConf.add_password_field(conf_type, setting)
            conf_types.add(conf_type)
    except:
        msg = "Error while constructing map of 'encrypt' fields for conf password redaction; some passwords may not be auto-redacted."
        logger.error(msg)
    for conf_type in conf_types:
        enabled_filters.append(("basename_match", "%s.conf" % conf_type, FilteredConf))

def setup_filters(options):
    del enabled_filters[:] # essentially, .clear()

    # conf password redaction isn't optional, do it first
    _setup_filters_passwords()

    enabled_filters.append(("basename_match", "telemetry.conf", FilteredTelemetryConf))

    if not options.filtersearches:
        logger.info("Logged search filtering is disabled.")
    if options.filtersearches:
        logger.info("Logged search filtering is enabled.")

        for name, pattern in options.simple_searchfilters.items():
            FilteredAuditLog.add_regex(name, pattern)
        for name, pattern in options.luhn_searchfilters.items():
            FilteredAuditLog.add_regex(name, pattern, check_luhn=True)

        enabled_filters.append(("stem_match", "log/audit.log", FilteredAuditLog))
        enabled_filters.append(("stem_match", "log/remote_searches.log", FilteredRemoteSearchLog))


def get_filter(diag_path):
    root, rootless_diag_path = diag_path.split(os.path.sep, 1)
    basename = os.path.basename(diag_path)
    selected_filter = None
    for filt in enabled_filters:
        filter_type = filt[0]
        if filter_type == "stem_match" and rootless_diag_path.startswith(filt[1]):
            selected_filter = filt[2]
        elif ( filter_type == "basename_match" and
               basename.startswith(filt[1]) and
               not "README" in diag_path ):
            selected_filter = filt[2]
    logger.debug("get_filter(%s) -> %s", diag_path, selected_filter)
    return selected_filter

def wants_filter(diag_path):
    logger.debug("wants_filter(%s)", diag_path)
    return bool(get_filter(diag_path))



# match  .... ":uname:hash::User Name:roles:email:???:"
#      group:    1     2        3      4      5    6
ops_json_passwd_regex = re.compile('":([^:]*:){2}:([^:]*:){4}"')
def filtered_ops_json_read_func(self, readable_obj=None, size=-1):
    """ Filter an ops.json file to hide password hashes.
    Read in data as lines, but emit return bytes """
    if size == 0:
        # Strange, but okay, here's your zero bytes.
        return ""
    elif size == -1:
        # default to 16KB
        target_size = 16 * 1024
    else:
        target_size = size

    # we already have enough, return them keeping the remaineder
    if self.remaining_bytes <= target_size:
        self.remaining_bytes =  self.remaining_bytes[target_size:]
        return self.remaining_bytes[:target_size]

    # else we need to read until we have enough.
    bytes_read = 0
    bytes_to_read = target_size - len(self.remaining_bytes)

    # if we're at EOF, loop short-circuits
    for line in readable_obj:
        lines_this_time = []
        # lines over 1k aren't going to be passwd entries
        if len(line) < 1000:
            match = ops_json_passwd_regex.search(line)
            if match:
                # group 2 is the hash, contains "hashtext:"
                modified_line = (line[:match.start(2)] +
                                 "hash_removed:" +
                                 line[match.end(2):])
                line = modified_line
        bytes_read += len(line)
        lines_this_time.append(line)

        if bytes_read >= bytes_to_read:
            break

    # bytes left from prior + newly read bytes in one buf
    tmp_buf = self.remaining_bytes + ''.join(lines_this_time)

    # handles the case of target_size > len(tmp_buf)
    self.remaining_bytes = tmp_buf[target_size:]
    return tmp_buf[:target_size]

def filter_passwd(filename):
    """Reads the contents of a passwd file and returns them as a string
       Any password info present is redacted.

       Implemented (legacy) as a simple replacement because
       $SPLUNK_HOME/etc/passwd will never be large enough to threaten memory.
       """
    output_lines = ["# FILTERED BY DIAG TO REMOVE PASSWORD HASHES & SALTS\n"]
    for line in open(filename):
        parts = line.split(":")
        if len(parts) < 6:
            # heuristic from AuthenticationManagerSplunk::init()
            # such lines are ignored, so simply retain (comments?)
            output_lines.append(line)
            continue

        parts[2] = "hash_removed" # clobber the hashed password
        parts[3] = "salt_removed" # clobber the salt
        output_lines.append(':'.join(parts))
    return ''.join(output_lines)


###################
# internal utility

def simplerunner(cmd, timeout, description=None, input=None):
    """ Using PopenRunner directly every time is tedious.  Do the typical stuff.

        cmd         :iterable of strings, eg argv
        timeout     :time to give the command to finish in seconds before it is
                     brutally killed
        description :string for logging about the command failing etc
        input       :string to stuff into stdin pipe the command
    """
    cmd_string = " ".join(cmd)
    if not description:
        description = cmd_string
    opener = functools.partial(subprocess.Popen, cmd,
                               stdout=subprocess.PIPE, shell=False)
    runner = PopenRunner(opener, description=description)
    exit_code = runner.runcmd(timeout=timeout, input=input)
    out = runner.stdout
    if not out:
        out = "The command '%s' was cancelled due to timeout=%s\n" % (cmd_string, timeout)
    return exit_code, out

class PopenRunner(object):
    """ Run a popen object (passed in as a partial) with a timeout

        opener = functools.partial(subprocess.Popen, cmd=["rm", "/etc/passwd"], arg=blah, another_arg=blee)
        runner = PopenRunner(opener, description="password destroyer")
        returncode = runner.runcmd(timeout=15)
        print(runner.stdout)
        print(runner.stderr)
    """
    def __init__(self, popen_f, description=None):
        """popen_f     :function that when called, returns a Popen object
           description :string for logging about the command failing etc
        """
        self.opener = popen_f
        self.description = description
        self.p_obj = None
        self.stdout = None
        self.stderr = None
        self.exception = None
        self.traceback = None

    def runcmd(self, timeout=10, input=None):
        """timeout     :time to give the command to finish in seconds
           input       :string to stuff into stdin pipe for command
        """
        def inthread_runner(input=input):
            try:
                self.p_obj = self.opener()
                if (input is not None) and sys.version_info >= (3, 0): input = input.encode()
                self.stdout, self.stderr = self.p_obj.communicate(input=input)
                if sys.version_info >= (3, 0):
                    self.stdout = self.stdout.decode()
                    if self.stderr is not None:
                        self.stderr = self.stderr.decode()
            except Exception as e:
                class fake_pobj(object):
                    pass

                if isinstance(e, OSError) and e.errno in (errno.ENOENT, errno.EPERM, errno.ENOEXEC, errno.EACCES):
                    # the program wasn't present, or permission denied; just report that.

                    # Aside: Popen finds out about the problem during a read call on the
                    # pipe, so the exception doesn't know the filename, and we
                    # don't here either.  Sad.

                    # we'll fib a bit and claim the errror desc is the output, for
                    # consumer purposes
                    self.stdout = str(e)
                    self.stderr = str(e)

                    # However, if they really want to know, the returncode will
                    # tell them the command did not run (this will be the return
                    # value of runcmd)
                    self.p_obj = fake_pobj()
                    self.p_obj.returncode = 127

                else:
                    # for everything else we want the stack to log
                    self.exception = e
                    self.traceback = traceback.format_exc()

                    self.p_obj = fake_pobj()
                    self.p_obj.returncode = -1


        thread = threading.Thread(target=inthread_runner)
        thread.start()
        thread.join(timeout)
        def log_action(action):
            if self.description:
                logger.warn("%s %s." % (action, self.description))
            else:
                logger.warn("%s stalled command." %(action,))

        if thread.is_alive():
            log_action("Terminating")
            if self.p_obj: # the thread may not have set p_obj yet.
                self.p_obj.terminate()
            else:
                logger.warn("Unexpectedly tearing down a thread which never got started.")
            time.sleep(0.2)
            if thread.is_alive():
                log_action("Killing")
                if self.p_obj: # the thread may not have set p_obj yet.
                    self.p_obj.kill()
                else:
                    logger.error("A python thread has completely stalled while attempting to run: %s" % (self.description or "a command"))
                    logger.error("Abandoning that thread without cleanup, hoping for the best")
                    return 1
            thread.join()

        if self.exception:
            logger.error("Exception occurred during: %s" % (self.description or "a command"))
            logger.error(self.traceback)

        return self.p_obj.returncode

def make_filterchain(*filters):
    "Combine one or more filters for use with copytree/collect_tree"
    def new_filterchain(directory, entries, filters=filters):
        total_ignored = set()
        for filt in filters:
            filter_ignored = filt(directory, entries)
            total_ignored.update(filter_ignored)
        return total_ignored
    return new_filterchain

detected_certs = []
def looks_like_ssl_cert(path):
    # I cannot determine any way to recognize asn.1 binary without full parsing
    # them which seems scary. --jrod
    asn_1_extensions = [
            ".cer",
            ".der",
            ".pfx",
            ".p12",
    ]
    for ext in asn_1_extensions:
        if path.endswith(ext):
            return True

    # These guys have a fairly regular format.  The labels are moderately
    # ad-hoc, but the -----BEGIN line-opener is required; see RFC7468 and its
    # antecedents
    pem_extensions = [
        ".pem",
        ".key",
        ".crt",
    ]
    for ext in pem_extensions:
        if path.endswith(ext):
            try:
                with open(path) as f:
                    for line in f:
                        if line.startswith("-----BEGIN"):
                            return True
            except IOError as e:
                msg = "Failure to read file while trying to detect certs, presuming it is. %s"
                logger.warn(msg, e)
                return True
    return False

def log_detected_certs():
    global detected_certs
    if detected_certs:
        logger.info("The following certificates were excluded from the diag output automatically.")
        for cert in detected_certs:
            logger.info("\t%s" % cert)
        logger.info("If you have any certs that were not auto-detected, please add them to an EXCLUDE rule in the [diag] stanza of server.conf.")
    detected_certs = []

def sslcertfilter(directory, entries):
    ignore_set = set()
    for entry in entries:
        path = os.path.join(directory, entry)
        if looks_like_ssl_cert(path):
            detected_certs.append(path)
            add_sslcert_file_to_excludes(path)
            ignore_set.add(entry)
    return ignore_set

def make_pathfilter(unwanted_path):
    """Construct path_filtering for use in copytree/collect_tree
       Weeds out specific pathname, or names matching glob expression
    """
    def new_pathfilter(directory, entries, unwanted_path=unwanted_path):
        ignore_set = set()
        for entry in entries:
            # if the path is present, return it as the ignore set
            path = os.path.join(directory, entry)
            if fnmatch.fnmatch(path, unwanted_path):
                ignore_set.add(entry)
        return ignore_set
    return new_pathfilter

def make_lookupfilter(include_lookups, default_lookup_includes):
    """Construct a lookup-blocker for use in copytree/collect_tree """
    def lookup_filter(directory, entries, include_lookups=include_lookups,
                      gather_overrides=default_lookup_includes):
        if include_lookups:
            # lookups are included, so the "filter" is a no-op
            return set()
        if os.path.basename(directory) == 'lookups':
            # drop all by default
            drop_set = set(entries)
            # maybe we have some overrides for this one?
            parent = undecorate_path(os.path.dirname(directory))
            if parent in default_lookup_includes:
                # filter out (do not drop) files it said to gather
                keep_set = set(default_lookup_includes[parent])
                drop_set = drop_set.difference(keep_set)
            return drop_set
        # for (nonlookup dirs), drop nothing
        return set()
    return lookup_filter


def make_sizefilter(byte_limit):
    """Construct size-filtering for use in copytree/collect_tree
       Weeds out files over some size, logs, handles the excluded file
       collection.
    """
    def new_sizefilter(directory, entries, limit=byte_limit):
        # if limit is zero, no filtering
        if not limit:
            return set()
        ignore_set = set()
        for entry in entries:
            path = os.path.join(directory, entry)
            if os.path.isdir(path):
                continue
            file_size = os.stat(path).st_size
            if file_size > limit:
                logger.info("filtered out file '%s'  limit: %s  size: %s" %
                            (path, limit, file_size))
                ignore_set.add(entry)
        return ignore_set
    return new_sizefilter

def normalize_path(btool_path):
    """Attempt to regularlize the strings pasesd in by btool into some kind
       of regular state.

       This accretes over time as the things that splunkd accepts continously
       shift without ever being documented or communicated.

       Currently we
         * swap all windows forward slashes to backslashes,
         * Throw errors if there are no dirseps (most typically a\b\c on unix)
         * Expand $SPLUNK_DB or similar, though only at the start of a line
           (since I don't know what splunkd does, $ is a valid path component,
            and relying on env vars that are not guaranteed is a really bad
            idea)
      """
    # SPL-25568 first try to hack around use of '/' on Windows.. soo ugly
    # 'safe' because / is not legal path character on win32.
    # bundle layer should normalize this to universal separator or
    # always platform specific.
    if os.name == "nt":
        btool_path = os.path.normpath(btool_path)

        # For Windows if the path is a disk (i.e. "C:\") this will cause
        # the db path to be \\db, which will be treated like a network
        # path (No Good!).  Replace "\\" with "\" and the diag will be
        # created correctly.
        btool_path = os.path.expandvars(btool_path).replace("\\\\", "\\")
    else:
        btool_path = os.path.expandvars(btool_path)

    return btool_path

def expand_underscore_index_name(orig_path, index_name):
    """all index paths, as of 6.0, support a magic $_index_name variable
       signature, which should be expanded to have the name of the index

       This value is supported *anywhere* in an index path so technically
       you could even use it to set the name of your volume.

       This functionality does not exist for volume paths -- the paths
       specified inside a volume (nor could it really, as volumes do not
       have index names).
    """
    magic_string = "$_index_name"
    var_name_chars = string.ascii_letters + string.digits + "_"

    input_parts = orig_path.split(magic_string)
    logger.debug("expand_underscore_index_name: input_parts: %s", input_parts)

    # start with the first split component, which precedes the first split
    output_parts = input_parts[:1]
    logger.debug("expand_underscore_index_name: initial output-parts: %s", output_parts)

    for part in input_parts[1:]:
        # see if it's really another var, eg $index_name_whatever
        if part and part[0] in var_name_chars:
            # in this case we have to copy in the removed magic_string
            output_parts.append(magic_string)
            logger.debug("expand_underscore_index_name: %s", output_parts)
        else:
            # we were supposed to replace it, so copy in the index name instead
            output_parts.append(index_name)
            logger.debug("expand_underscore_index_name: %s", output_parts)
        # and lastly, we copy in the text after the substitute
        output_parts.append(part)
        logger.debug("expand_underscore_index_name: %s", output_parts)

    # and merge the text back into a string
    output_path = ''.join(output_parts)

    msg = "expand_underscore_index_name: mapped orig_path:%s index_name:%s to output_path:%s"
    logger.debug(msg, orig_path, index_name, output_path)
    return output_path

computed_db_paths = None
def splunkDBPaths():
    """ Returns a datastructure describing every index path in the system.

        This is a dictionary of dictionaries, eg
        { 'indexname1' :
                { 'pathname1' : path, ...},
          'indexname2' :
                { 'pathname2' : path, ...},
         ...
        }
        where pathnames are the names of the settings:
            homePath, coldPath, thawedPath, tstatsHomePath, summaryHomePath.

        If a given path is not defined in the configuration (explicitly or
        implied), then it is set to None.

        Computing this is pretty expensive, and has warning side-effects, so
        the result is cached for subsequent usage.

        May end up needing to convert this into some kind of datastructure
        allowing the consumers to pick and choose.
    """
    # if cached, return answer -- surprisingly computing this takes like 4 seconds
    global computed_db_paths
    if computed_db_paths:
        return computed_db_paths

    # first get all the index path config strings
    index_confs  = get_indexes_conf()

    req_parm_warning = 'Index stanza [%s] is missing required parameter "%s"'

    # identify the volumes vs indexes vs stuff we don't handle currently
    volume_stanzas = {}
    index_stanzas = {}

    for stanza_name, stanza_settings in index_confs.items():
        # ignore disabled stanzas
        if stanza_settings.get('disabled') == "true":
            continue
        # default isn't an index or a volume
        if stanza_name == "default":
            continue
        if stanza_name.startswith('volume:'):
            # build volume_stanzas list
            volume_stanzas[stanza_name] = stanza_settings
        # ignore all virtual indexes for diag-building purposes, but warn if they seem broken
        elif stanza_name.startswith('provider-family:'):
            if 'vix.mode' not in stanza_settings:
                logger.warn(req_parm_warning % (stanza_name, 'vix.mode'))
            if 'vix.command' not in stanza_settings:
                logger.warn(req_parm_warning % (stanza_name, 'vix.command'))
            continue
        elif stanza_name.startswith('provider:'):
            if 'vix.family' not in stanza_settings:
                logger.warn(req_parm_warning % (stanza_name, 'vix.family'))
            continue
        elif "vix.provider" in stanza_settings:
            logger.info('Virtual index "%s" found, not scanning for diag.' % stanza_name)
            continue
        else:
            index_stanzas[stanza_name] = stanza_settings

    # refactoring safety
    del stanza_name
    del stanza_settings

    # flatten volumes to the one param we need
    volumes = {}
    for vol_name, vol_settings in volume_stanzas.items():
        vol_path = vol_settings.get('path')
        # skip broken volume groups
        if not vol_path:
            logger.warn("The indexing volume %s does not have a path defined, this is an error." % (vol_name))
            continue
        volumes[vol_name] = vol_path

    db_paths = {}

    # these are expected
    path_keys = ['homePath', 'coldPath', 'thawedPath', 'tstatsHomePath']
    # these may not exist, don't complain
    opt_path_keys = ['summaryHomePath']

    for index_name, index_settings in index_stanzas.items():
        # compute summary path if not present
        sum_home_path = "summaryHomePath"
        if not sum_home_path in index_settings:
            if "homePath" in index_settings:
                home_path = index_settings['homePath']
                # we'd love to just do basename, but at this point homePath
                # can be full of weird junk
                # rfind for sep+altsep
                for i in reversed(list(range(len(home_path)))):
                    if home_path[i] in (os.sep, os.altsep):
                        break
                home_root = home_path[:i]
                if home_root:
                    new_sum_path = home_root + "/summary"
                    index_settings[sum_home_path] = new_sum_path
        resolved_index_paths = {}
        for path_key in path_keys + opt_path_keys:
            if path_key not in index_settings:
                if not path_key in opt_path_keys:
                    logger.warn("The index %s does not have a value set for %s, this is unusual." % (index_name, path_key))
                resolved_index_paths[path_key] = None
                continue
            cur_path = index_settings[path_key]
            # expand any $_index_name
            expanded_path = expand_underscore_index_name(cur_path, index_name)
            resolved_index_paths[path_key] = expanded_path
        db_paths[index_name] = resolved_index_paths

    def expand_vol_path(orig_path, volumes=volumes):
        """Index paths may contain volume:volumename/<relativepath>.
           Here we unpack that -- though it can thoretically be incomplete
           if the volume path itself has things like $SPLUNK_DB in it.
        """
        if not orig_path.startswith('volume:'):
            return orig_path
        tmp_path = orig_path
        if os.name == "nt" and  (not '\\' in tmp_path) and ('/' in tmp_path):
            tmp_path = orig_path.replace('/', '\\')
        if not os.path.sep in tmp_path:
            logger.warn("Volume based path '%s' contains no directory seperator." % orig_path)
            return None
        volume_id, tail = tmp_path.split(os.path.sep, 1)
        if not volume_id in volumes:
            logger.warn("Volume based path '%s' refers to undefined volume '%s'." % (orig_path, volume_id))
            return None
        output_path = os.path.join(volumes[volume_id], tail)

        msg = "expand_vol_path: mapped orig_path:%s to output_path:%s"
        logger.debug(msg, orig_path, output_path)
        return output_path

    def map_all_paths(f, index_paths):
        new_toplevel = {}
        for name, settings in index_paths.items():
            new_settings = {}
            for setting_name, path in settings.items():
                if path:
                    new_settings[setting_name] = f(path)
                else:
                    new_settings[setting_name] = None
            new_toplevel[name] = new_settings
        return new_toplevel


    # TODO: move $_index_name expansion here. expand_underscore_index_name()

    # detect and expand volume paths
    paths = map_all_paths(expand_vol_path, db_paths)
    paths = map_all_paths(normalize_path, paths)

    # cache answer
    logger.debug("db_paths: %s", paths)
    computed_db_paths = paths

    return paths

def get_listing(src, desc):
    """Gather recursive listing of files in a directory and all subdirectories"""
    system_info.write('\n\n********** %s dir listing **********\n' % desc)
    # TODO: move this implementation to get_recursive_dir_listing, and add the
    # header on top -- nontrivial due to handling failure cases.
    if not os.path.exists(src):
        return
    if os.name == "posix":
        # get a recursive listing of that path.
        system_info.write('\nls -alR "%s"\n' % src)
        system_info.write(os.popen('ls -alR "%s"' % src).read())
    else:
        system_info.write('\ndir /s/a "%s"\n' % src)
        system_info.write(os.popen('dir /s/a "%s"' % src).read())

def get_dir_listing(dir_path):
    """Gather flat non-recursive listing of contents of one directory"""
    if os.name == "posix":
        cmd = 'ls -al "%s"' % dir_path
    else:
        cmd = 'dir /a "%s"' % dir_path
    cmd_output = os.popen(cmd).read()
    # cmd_output is bytes of uknknown encoding, so we can't combine it with a
    # a potentially unicode command (dir_path, and thus cmd are unicode objects
    # on windows)
    # Therefore, produce a utf-8 byte string (str) which we can safely combine
    # the output bytes.
    # TODO: since the encoding of the output is unknown, it should be stored
    # independently
    if sys.version_info < (3, 0) and isinstance(cmd, unicode):
        cmd_bytes =  cmd.encode('utf-8')
    elif sys.version_info >= (3, 0) and isinstance(cmd, str):
        cmd_bytes =  cmd.encode('utf-8')
    else:
        cmd_bytes =  cmd
    if sys.version_info >= (3, 0):
        cmd_output = cmd_output.encode('utf-8')
    return cmd_bytes + b"\n" + cmd_output

def get_recursive_dir_listing(dir_path):
    """ return a recursive listing of the given dir """
    if os.name == "posix":
        cmd = 'ls -alR "%s"' % dir_path
    else:
        cmd = 'dir /s/a "%s"' % dir_path
    cmd_output = os.popen(cmd).read()
    # see comment in get_dir_listing() -- TODO: same
    if sys.version_info < (3, 0) and isinstance(cmd, unicode):
        cmd_bytes =  cmd.encode('utf-8')
    elif sys.version_info >= (3, 0) and isinstance(cmd, str):
        cmd_bytes =  cmd.encode('utf-8')
    else:
        cmd_bytes =  cmd
    if sys.version_info >= (3, 0):
        cmd_output = cmd_output.encode('utf-8')
    return cmd_bytes + b"\n" + cmd_output

# This function is derivative of the copytree in shutil.copytree under the
# Python Software Foundation License, which makes no obligations upon licensees
# beyond limited liability.

# The changes are as follows..  actually calls out to the storage object in
# place to do something with the files/dirs instead of doing the work itself.
def collect_tree(src, dst, actor, ignore=None):
    """Recursively walk a a directory tree, applying a function to each entry.
    Made for adding files to tar, a dir.

    If exception(s) occur, an Error is raised with a list of reasons.

    actor is a callable, which must accept src, the path to a real file, and
    dst, target path within the described namespace.

    The optional ignore argument is a callable. If given, it
    is called with the `src` parameter, which is the directory
    being visited by copytree(), and `names` which is the list of
    `src` contents, as returned by os.listdir():

        callable(src, names) -> ignored_names

    Since collect_tree() is called recursively, the callable will be
    called once for each directory that is copied. It returns a
    list of names relative to the `src` directory that should
    not be copied.
    """
    names = os.listdir(src)
    if ignore is not None:
        ignored_names = ignore(src, names)
    else:
        ignored_names = set()

    actor(src, dst)
    errors = []
    for name in names:
        if name in ignored_names:
            continue
        srcname = os.path.join(src, name)
        dstname = os.path.join(dst, name)
        try:
            if os.path.isdir(srcname):
                collect_tree(srcname, dstname, actor, ignore)
            else:
                # Will raise a SpecialFileError for unsupported file types
                actor(srcname, dstname)
        # catch the Error from the recursive copytree so that we can
        # continue with other files
        except shutil.Error as err:
            errors.extend(err.args[0])
        except EnvironmentError as why:
            errors.append((srcname, dstname, str(why)))
    # XXX Hack: have to give a chance to set modtime at the end
    actor(src, dst)
    if errors:
        raise shutil.Error(errors)

def copy_file_with_parent(src_file, dest_file):
    "copy a file while creating any parent directories for the target"
    # ensure directories exist
    dest_dir = os.path.dirname(dest_file)
    try:
        if not os.path.isdir(dest_dir):
            os.makedirs(os.path.dirname(dest_file))
        shutil.copy2(src_file, dest_file)
    except IOError as e:
        # windows sucks
        err_msg = "Error duping file for '%s' to '%s', %s, continuing..."
        logger.warn(err_msg % (src_file, dest_file, e.strerror))

def splitunc(p):
    """Split a pathname into UNC mount point and relative path specifiers.
    Return a 2-tuple (unc, rest); either part may be empty.
    If unc is not empty, it has the form '//host/mount' (or similar
    using backslashes).  unc+rest is always the input path.
    Paths containing drive letters never have a UNC part.
    """
    if p[1:2] == ':':
        return '', p # Drive letter present
    firstTwo = p[0:2]
    if firstTwo == '//' or firstTwo == '\\\\':
        # is a UNC path:
        # vvvvvvvvvvvvvvvvvvvv equivalent to drive letter
        # \\machine\mountpoint\directories...
        #           directory ^^^^^^^^^^^^^^^
        normp = p.replace('\\', '/')
        index = normp.find('/', 2)
        if index <= 2:
            return '', p
        index2 = normp.find('/', index + 1)
        # a UNC path can't have two slashes in a row
        # (after the initial two)
        if index2 == index + 1:
            return '', p
        if index2 == -1:
            index2 = len(p)
        return p[:index2], p[index2:]
    return '', p

def transform_index_path_to_cute_relative_path(index_path):
    """Somewhat misguided old logic to make the customer's indexing paths a bit more
       obvious when manually unpacking diags.
       Transformations:
         $SPLUNK_HOME/whatever     -> whatever
         /absolute/whatever        -> absolute/whatever
         C:\windows\drives\whatevr -> c\windows\drives\whatevr
         \\windows\shares\whatever -> windows\shares\whatever
         $SPLUNK_HOME              -> ""
    """
    tmp_path = index_path
    unc_dirnames = ""
    drive = ""

    # drop SPLUNK_HOME if present (default case), and only if there's a dir separator after SPLUNK_HOME
    if index_path == SPLUNK_HOME:
        logger.warn("You have an index path identical to SPLUNK_HOME.  This is quite bad. Please fix.")
        return "bad_index_path"

    if tmp_path.startswith(SPLUNK_HOME):
        if tmp_path[len(SPLUNK_HOME)] in ("/", "\\"): 
            tmp_path = tmp_path[len(SPLUNK_HOME):]

    # windows tomfoolery....
    if os.name == 'nt':
        # changes \\server\share\something... into "\\server\share", "\something..."
        # leaves other paths untouched in tmp_path
        unc_segment, tmp_path = splitunc(tmp_path) 

        # change "\\server\share" into "server\share"
        unc_dirnames = unc_segment.lstrip('\\')

    # capture drive letter if it exists "C:\what\ever" -> "C:", "\what\ever"
    drive, tmp_path = os.path.splitdrive(tmp_path)
    if drive:
        # "C:" -> "C", since windows is unahppy with colons in dirnames
        drive = drive[0]

    # /a/path/etc...        -> "a/path/etc..."
    # \Program Files\Splunk -> Program Files\Splunk
    if tmp_path[0] in (os.sep, os.altsep):
        tmp_path = tmp_path[1:]

    # put it all together!
    # "server\share", "c", "what\ever" -> "server\share\c\what\ever"
    # (not that this particular combination can occur..)
    # Note that join happily ignores empty strings
    return  os.path.join(unc_dirnames, drive, tmp_path)


def deleteTree(path):
    """ Delete a directory, if it exists. """
    def handle_readonly_filedir_errors(func_called, path, exc):
        "try to make this more like rm -rf"
        error_type = exc[1]
        full_perms = stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO

        parent = os.path.dirname(path)
        os.chmod(parent, full_perms)
        os.chmod(path, full_perms)
        if func_called == os.rmdir:
            os.rmdir(path)
        elif func_called == os.remove:
            os.remove(path)
        else:
            raise

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=handle_readonly_filedir_errors)

def clean_temp_files():
    "Wipe the diag-temp dir.  Probably obsolete" # TODO - kill?
    if os.path.isdir(RESULTS_LOC):
        for f in os.listdir(RESULTS_LOC):
            specific_tempdir = os.path.join(RESULTS_LOC, f)
            if os.path.isdir(specific_tempdir):
                deleteTree(specific_tempdir)

def filter_dumps(dump_filenames):
    """Do some "smart" filtering of the windows crash dumps.
       This is to try to keep file sizes sane.

       Current behavior is:
       * Don't include dumps over 30 days old
       * Of dumps under 30 days old, include a maximum of three
       * To select those three, choose the 2 most recent, and then one
         "at the beginning of the set"
       * That means if there's a 2 day gap between crashes, take the one after
         the gap
       * Goal of that odd filtering is to get "the cause of the fail" &
         "some expamples of current state"

       * In addition, because some DMPs are enormous, apply a further size
         constraint -- if we go over 8GB of DMP files, stop
    """
    class Dump(object):
        def __init__(self, filename):
            self.filename=filename
            self.stat = os.stat(filename)
    dumps = list(map(Dump, dump_filenames))
    total_dumpspace = sum(map(lambda d: d.stat.st_size, dumps))
    dump_megs = total_dumpspace // (1024 * 1024)
    if dump_megs >= 2000:
        logger.warn("""Note, you have %sMB in memory dump files in your $SPLUNK_HOME/var/log/splunk directory.
You may want to prune, clean out, or move this data.""" % dump_megs)
    now = datetime.datetime.now()
    def age_filter(dump):
        age = now - datetime.datetime.fromtimestamp(dump.stat.st_mtime)
        if age.days >= 30:
            logger.info("Not including crashdump %s, over 30 days old" % dump.filename)
            return False
        return True
    dumps = list(filter(age_filter, dumps))
    # sort dumps by time, newest first.
    dumps.sort(key=lambda x: x.stat.st_mtime, reverse=True)
    # try to get the useful crashes, starting with the two most recent.
    useful_crashes = dumps[:2]
    msg = "added two most recent dumps to collection set: %s"
    logger.debug(msg % ([c.filename for c in useful_crashes],))
    # now get a third, going backwards if they are consective, to try to find
    # the start of whatever the recent problem is
    i = 2
    while i < len(dumps):
        cur_crash = dumps[i]
        if i+1 == len(dumps):
            # if we get to the end, use that
            logger.debug("added earliest crash in 30 day window: %s" % (cur_crash.filename,))
            useful_crashes.append(cur_crash)
            break
        cur_crashtime = datetime.datetime.fromtimestamp(cur_crash.stat.st_mtime)
        older_crashtime = datetime.datetime.fromtimestamp(dumps[i+1].stat.st_mtime)
        tdelta = cur_crashtime - older_crashtime
        # 2 days we assume is large enough to indicate this is the start
        if tdelta.days >= 2:
            useful_crashes.append(cur_crash)
            logger.debug("added crash at beginning of sequence: %s" % (cur_crash.filename,))
            break
        i+=1
    # now enforce some size limits; drop dumps until we get under the limit (or
    # until we're down to 1)
    while len(useful_crashes) > 1:
        selected_dumpspace = sum(map(lambda d: d.stat.st_size, useful_crashes))
        selected_dump_megs = selected_dumpspace // (1024 * 1024)
        if selected_dump_megs <= 8000:
            break
        logger.info("Desired dump fileset currently over 8GB, dropping %s from set." % (useful_crashes[-1].filename,))
        useful_crashes.pop(-1)

    return [x.filename for x in useful_crashes]

app_info_dict = {}
# The information about apps with diag extensions
def _store_prepared_app_info(prepared_app_modules):
    global app_info_dict
    app_info_dict = prepared_app_modules

def invalidate_app_ext(name):
    """Removes an app from the list of valid apps providing extension data due to
       problem/error"""
    del app_info_dict[name]

app_setup_exceptions_dict = {}
def store_app_exception(name, exception_text):
    global app_setup_exceptions_dict
    app_setup_exceptions_dict[name] = exception_text

def app_ext_names():
    "returns the list of apps with diag extensions configured"
    return list(app_info_dict.keys())

def get_app_ext_info(app_name):
    "returns an AppInfo object for the specific named app"
    return app_info_dict[app_name]

def get_app_ext_info_by_component(component_name):
    "returns an AppInfo object for the diag component name"
    app_name = component_name[len("app:"):]
    if not app_name in app_info_dict:
        return None
    return get_app_ext_info(app_name)

def app_components():
    "returns a tuple of the component names for apps with diag extensions"
    return tuple(app.component_name for app in app_info_dict.values())

class AppInfo(object):
    """glorified dict recording information about an app,
    Possibly with diag extensions"""
    def __init__(self, app_name, app_dir, diag_conf):
        """Basic app facts,
        app_name: directory name of app, e.g. 'search'
        app_dir:  absolute path to app dir
        diag_conf may be an empty dict if it has no diag stanza (normal)"""
        self.app_name = app_name
        self.app_dir = app_dir
        self.diag_conf = diag_conf

    def add_extension_info(self, diag_extension_file, data_limit):
        "Add diag extension info, because this app extends diag"
        self.diag_extension_file = diag_extension_file
        self.data_limit = data_limit
        # module name is how the module is accessed in info_gather's namespace,
        # similar to if it had run "import <module_name>", so we shove these
        # inside ourselves, since we know that's not a used namespace
        self.module_name = "info_gather." + self.app_name
        # The name of the diag "component" to do or not do the work for the app
        self.component_name = "app:" + self.app_name

        self.module_obj = None # must be set up later

    def add_module(self, module_obj):
        self.module_obj = module_obj

    def __repr__(self):
        return "<%s at 0x%x: %s>" % (self.__class__.__name__, id(self), self)

    def __str__(self):
        return "(app_name=%s, diag_extension_file=%s)" % ( self.app_name, self.diag_extension_file)


def gather_app_extensions(options):
    class AppScopedDiagDataAdder(object):
        """ Class to proxy the data addition requests for app extensions,
            ensuring they're stored in an app location
        """
        # TODO: insert data caps here
        def __init__(self, app_name, data_limit):
            self.scoped_dir = os.path.join("app_ext", app_name)
            self.app_name = app_name
            self.data_limit = data_limit
            self.data_seen = 0

        def over_limit(self):
            return self.data_seen > self.data_limit
        def increase_added_data(self, b):
            self.data_seen += b
            if self.over_limit():
                msg = "Data collection for app '%s' has exceeded the configured"
                msg += " bytecount limit: '%s', further data for this app will be curtailed."
                logger.warn(msg, self.app_name, self.data_limit)

        def add_file(self, file_path, diag_path):
            if self.over_limit():
                return
            file_size = os.stat(file_path).st_size
            scoped_diag_path = os.path.join(self.scoped_dir, diag_path)
            # call to global func
            add_file_to_diag(file_path, scoped_diag_path)
            self.increase_added_data(file_size)

        def add_dir(self, file_path, diag_path):
            if self.over_limit():
                return

            offset_before = storage.tarfile.offset

            scoped_diag_path = os.path.join(self.scoped_dir, diag_path)
            # call to global func
            add_dir_to_diag(file_path, scoped_diag_path)

            offset_after = storage.tarfile.offset
            self.increase_added_data(offset_after - offset_before)

        def add_string(self, content, diag_path):
            if self.over_limit():
                return

            scoped_diag_path = os.path.join(self.scoped_dir, diag_path)
            # call to global func
            add_string_to_diag(content, scoped_diag_path)
            self.increase_added_data(len(content))

        def add_rest_endpoint(self, endpoint, diag_path):
            if self.over_limit():
                return

            if not local_splunkd_protocolhostport:
                # rest was disabled, do nothing
                return

            scoped_diag_path = os.path.join(self.scoped_dir, diag_path)
            full_url = local_splunkd_protocolhostport + endpoint
            # call to global func
            data_size = read_rest_endpoint(full_url, scoped_diag_path)
            self.increase_added_data(data_size)

    enabled_apps = [comp for comp in options.components if comp.startswith("app:")]
    for app in enabled_apps:
        app_info = get_app_ext_info_by_component(app)

        # Now construct an app-specific Values object
        app_options = optparse.Values()

        app_option_prefix = app_info.app_name + "."
        # optparse.Values obects are really Bunches; classes with a bunch of
        # manually-inserted properties.  This choice leaves us without a way to
        # nicely iterate through the contents to find app-specific options, so
        # we break the abstraction via __dict__, as it does itself.
        for attr, val in options.__dict__.items():
            if attr.startswith(app_option_prefix):
                unscoped_attr = attr[len(app_option_prefix):]
                app_options.__dict__[unscoped_attr] = val

        data_adder = AppScopedDiagDataAdder(app_info.app_name,
                                            app_info.data_limit)
        try:
            app_info.module_obj.collect_diag_info(diag=data_adder,
                                                  app_dir=app_info.app_dir,
                                                  options=app_options,
                                                  global_options=options)
        except Exception as e:
            output_path = os.path.join("app_ext", app_info.app_name,
                                       "data_collect_exception.output")
            add_string_to_diag(traceback.format_exc(), output_path)
            msg = "Diag extensions: App %s threw an exception during data collection.  Exception trace stored in diag at %s. Validity and completeness of app-extension collected data is unknown."
            logger.error(msg, app_info.app_name, output_path)
            logger.error(traceback.format_exc())

#############################
# Bringup, options & main

def make_human_sizestr(size_in_bytes):
    """accepts a size in bytes (presumably an integer, though a float will work
    too)
    returns a string describing that size
    """
    size = size_in_bytes
    if size == 1:
        return "1 byte"
    for suffix in [' bytes', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if size < 1000 or suffix == 'PB':
            numstr = "%.1f" % size
            numstr = numstr.rstrip('0').rstrip('.')
            return numstr + suffix
        size = size / 1024.0

def parse_human_sizestr(s, default_units="b"):
    """accepts human-friendly data size strings like 10kb or 2GB
       returns the size in byte

       errors parsing the string result in a raised ValueError
    """
    sizemap = {"b":      1,
               "kb": 2**10,
               "mb": 2**20,
               "gb": 2**30,
               "tb": 2**40,
               "pb": 2**50,   # perhaps being too completeist here
    }
    numbers = re.match('^\d+', s)
    if not numbers:
        msg = "No integer in size string '%s'" % s
        raise ValueError(msg)
    base_number = int(numbers.group(0))
    unit_s = s[numbers.end():]

    if not unit_s:
        unit_s = default_units

    if not unit_s.lower() in sizemap:
        msg = "Units substring '%s' is not known" % unit_s
        raise ValueError(msg)

    return base_number * sizemap[unit_s.lower()]

def local_getopt(file_options, cmd_argv=sys.argv):
    "Implement cmdline flag parsing using optparse"

    def set_components(option, opt_str, value, parser):
        "Override any existing set of enabled components with the provided string"
        if not value:
            raise optparse.OptionValueError("--collect argument missing")

        components = value.split(",")
        all_components = set(KNOWN_COMPONENTS).union(set(app_components()))
        if 'all' in components:
            parser.values.components = all_components
        else:
            req_components = set(components)
            unknown_components = req_components.difference(all_components)
            if unknown_components:
                as_string = ",".join(unknown_components)
                raise optparse.OptionValueError("Unknown components requested: " + as_string)
            parser.values.components = req_components

    def enable_component(option, opt_str, value, parser):
        "Add one component to the enabled set of components"
        component = value
        if component not in (KNOWN_COMPONENTS + app_components()):
            raise optparse.OptionValueError("Unknown component requested: " + component)
        elif component in parser.values.components:
            logger.warn("Requested component '%s' was already enabled.  No action taken." % component)
        else:
            parser.values.components.add(component)

    def disable_component(option, opt_str, value, parser):
        "Remove one component from the enabled set of components"
        component = value
        if component not in (KNOWN_COMPONENTS + app_components()):
            raise optparse.OptionValueError("Unknown component requested: " + component)
        elif component not in parser.values.components:
            logger.warn("Requested component '%s' was already disabled.  No action taken." % component)
        else:
            parser.values.components.remove(component)

    def _parse_size_string(value, setting_name):
        "accept sizes like 10kb or 2GB, returns value in bytes"
        sizemap = {"b":      1,
                   "kb": 2**10,
                   "mb": 2**20,
                   "gb": 2**30,
                   "tb": 2**40,
                   "pb": 2**50,   # perhaps being too completeist here
        }
        numbers = re.match('^\d+', value)
        if not numbers:
            msg = "Could not find integer in %s target '%s'" % (setting_name, value)
            raise optparse.OptionValueError(msg)
        base_number = int(numbers.group(0))
        rest = value[numbers.end():]

        if not rest:
            # no indication means kilobytes (history)
            rest = "kb"

        if not rest.lower() in sizemap:
            msg = "Could not understand '%s' as a size denotation" % rest
            raise optparse.OptionValueError(msg)
        number = base_number * sizemap[rest.lower()]
        return number

    def set_log_size_limit(option, opt_str, value, parser):
        "sets limit on files for var/log dir"
        number = _parse_size_string(value, "--log-filesize-limit")
        parser.values.log_filesize_limit = number

    def set_etc_size_limit(option, opt_str, value, parser):
        "sets limit on files for etc dir"
        number = _parse_size_string(value, "--etc-filesize-limit")
        parser.values.etc_filesize_limit = number

    # handle arguments
    parser = optparse.OptionParser(usage="Usage: splunk diag [options]")
    parser.prog = "diag"

    # yes, a negative option. I'm a bastard
    parser.add_option("--nologin", action="store_true",
                      help="override any use of REST logins by components/apps")

    parser.add_option("--auth-on-stdin", action="store_true",
                      help="Indicates that a local splunk auth key will be provided on the first line of stdin")

    component_group = optparse.OptionGroup(parser, "Component Selection",
                      "These switches select which categories of information "
                      "should be collected.  The current components available "
                      "are: " + ", ".join(KNOWN_COMPONENTS + app_components()))

    parser.add_option("--exclude", action="append",
                      dest="exclude_list", metavar="pattern",
                      help="glob-style file pattern to exclude (repeatable)")

    component_group.add_option("--collect", action="callback", callback=set_components,
                      nargs=1, type="string", metavar="list",
                      help="Declare an arbitrary set of components to gather, as a comma-separated list, overriding any prior choices")
    component_group.add_option("--enable", action="callback", callback=enable_component,
                      nargs=1, type="string", metavar="component_name",
                      help="Add a component to the work list")
    component_group.add_option("--disable", action="callback", callback=disable_component,
                      nargs=1, type="string", metavar="component_name",
                      help="Remove a component from the work list")

    parser.add_option("--uri",
                      dest="uri", metavar="url",
                      help="url of a management port of a remote splunk install from which to collect a diag.")

    parser.add_option_group(component_group)

    detail_group = optparse.OptionGroup(parser, "Level of Detail",
                      "These switches cause diag to gather data categories "
                      "with lesser or greater thoroughness.")

    detail_group.add_option("--include-lookups", action="store_true",
                      help="Include lookup files in the etc component [default: do not gather]")

    detail_group.add_option("--all-dumps", type="string",
                      dest="all_dumps", metavar="bool",
                      help="get every crash .dmp file, opposed to default of a more useful subset")
    detail_group.add_option("--index-files", default="manifests", metavar="level",
                      help="Index data file gathering level: manifests, or full (meaning manifests + metadata files) [default: %default]")
    detail_group.add_option("--index-listing", default="light", metavar="level",
                      help="Index directory listing level: light (hot buckets only), or full (meaning all index buckets) [default: %default]")

    etc_filesize_default="10MB"
    detail_group.add_option("--etc-filesize-limit", type="string",
                      default=_parse_size_string(etc_filesize_default, ""), action="callback",
                      callback=set_etc_size_limit, metavar="size",
                      help="do not gather files in $SPLUNK_HOME/etc larger than this. (accepts values like 5b, 20MB, 2GB, if no units assumes kb), 0 disables this filter [default: %s]" % etc_filesize_default)
    detail_group.add_option("--log-age", default="60", type="int", metavar="days",
                      help="log age to gather: log files over this many days old are not included, 0 disables this filter [default: %default]")

    log_filesize_default="1GB"
    detail_group.add_option("--log-filesize-limit", type="string",
                      default=_parse_size_string(log_filesize_default, ""), action="callback",
                      callback=set_log_size_limit, metavar="size",
                      help="fully gather files in $SPLUNK_HOME/var/log smaller than this size.  For log files larger than this size, gather only this many bytes from the end of the file (capture truncated trailing bytes). [default: %s]" % log_filesize_default)

    parser.add_option_group(detail_group)

    filter_group = optparse.OptionGroup(parser, "Data Filtering",
                      "These switches cause diag to redact or hide data from the output diag.")

    filter_group.add_option("--filter-searchstrings", action="store_true", dest="filtersearches",
                      default=True,
                      help="Attempt to redact search terms from audit.log & remote_searches.log that may be private or personally identifying")

    filter_group.add_option("--no-filter-searchstrings", action="store_false", dest="filtersearches",
                      help="Do not modify audit.log & remote_searches.log")

    parser.add_option_group(filter_group)

    output_group = optparse.OptionGroup(parser, "Output",
                      "These control how diag writes out the result.")

    output_group.add_option("--stdout", action="store_true", dest="stdout",
                      help="Write an uncompressed tar to standard out.  Implies no progress messages.")

    output_group.add_option("--diag-name", "--basename", metavar="name", dest="diagname",
                      help="Override diag's default behavior of autoselecting its name, use this name instead.")

    output_group.add_option("--statusfile", metavar="filename", dest="statusfile",
                      help="Write progress messages to a file specified by the given path. Useful with --stdout.")

    output_group.add_option("--debug", action="store_true", dest="debug",
                      help="Print debug output")

    parser.add_option_group(output_group)


    upload_group = optparse.OptionGroup(parser, "Upload",
                     "Flags to control uploading files\n Ex: splunk diag --upload")

    upload_group.add_option("--upload", action="store_true", dest="upload",
                      help="Generate a diag and upload the result to splunk.com")

    upload_group.add_option("--upload-file", metavar="filename",
                      dest="upload_file",
                      help="Instead of generating a diag, just upload a file")

    upload_group.add_option("--case-number", metavar="case-number",
                      type='int', dest="case_number",
                      help="Case number to attach to, e.g. 200500")

    upload_group.add_option("--upload-user", dest="upload_user",
                      help="splunk.com username to use for uploading")

    upload_group.add_option("--upload-description", dest="upload_description",
                      help="description of file upload for Splunk support")

    upload_group.add_option("--firstchunk", type="int", metavar="chunk-number",
                      help="For resuming upload of a multi-part upload; select the first chunk to send")

    parser.add_option_group(upload_group)

    class Extension_Callback(object):
        """proxy object to permit extension to communicate other facts back.
           All we have so far is a will_need_rest() method"""
        def will_need_rest(self):
            _will_need_rest()

    # add any further parser config stuff for app extensions
    class Parser_Proxy(object):
        "proxy object for option parser to handle namespacing app options"
        def __init__(self, app_info, parser):
            self.app_info = app_info
            self.parser = parser
            self.optiongroup = None

        def add_option(self, *flags, **kwargs):
            if  ((len(flags) != 1) or (not flags[0].startswith('--'))):
                raise NotImplementedError("Diag extensions only support long opts (--foo -> --app.foo) for apps")

            # create an option group for the app, since it has at least one
            # flag. (visual help treatment)
            if not self.optiongroup:
                self.optiongroup = optparse.OptionGroup(self.parser,
                                                        "%s options" % self.app_info.component_name)

            #namespace and pass along the option add
            option_str = flags[0]
            proxied_flag = '--%s:%s' % (self.app_info.app_name, option_str.lstrip('-'))
            if 'dest' in  kwargs:
                if not 'metavar' in kwargs:
                    kwargs['metavar'] = kwargs['dest']
                kwargs['dest']  = "%s.%s" % (self.app_info.app_name, kwargs['dest'])
            self.optiongroup.add_option(proxied_flag, **kwargs)

        def _complete(self):
            "If any options were added, add the group to the parser"
            if self.optiongroup:
                self.parser.add_option_group(self.optiongroup)

    # setup hook for app to provide its own options
    for diag_ext_app in app_ext_names():
        app_info = get_app_ext_info(diag_ext_app)
        logger.debug("app_info: %s" % app_info)
        # set up a proxy object to namespace app options
        parser_proxy = Parser_Proxy(app_info, parser)
        callback = Extension_Callback()
        try:
            app_info.module_obj.setup(parser=parser_proxy,
                                      app_dir=app_info.app_dir,
                                      callback=callback)
        except Exception as e:
            # for any kind of failure, log an error, store the exception in
            # the app_info object, and turn off collection for the app later.
            exception_text = traceback.format_exc()
            msg = "Diag extensions: App %s threw an exception during setup(). No Extension collection will happen for this app, exception text will be stored in the diag at %s."
            output_path = os.path.join("app_ext", diag_ext_app, "setup_failed.output")
            logger.error(msg, diag_ext_app, output_path)

            invalidate_app_ext(diag_ext_app)
            store_app_exception(diag_ext_app, exception_text)

        parser_proxy._complete()

    # diag-collect every category, except REST, by default
    # no REST for now  because there are too many question marks about reliability
    default_components = set(KNOWN_COMPONENTS) | set(app_components())
    default_components.remove('rest')
    parser.set_defaults(components=default_components)

    # override above defaults with any from the server.conf file
    parser.set_defaults(**file_options)

    options, args =  parser.parse_args(cmd_argv)

    if options.index_files not in ('manifests', 'manifest', 'full'):
        parser.error("wrong value for index-files: '%s'" % options.index_files)

    if options.index_listing not in ('light', 'full'):
        parser.error("wrong value for index-listing: '%s'" % options.index_listing)

    if options.upload and options.upload_file:
        parser.error("You cannot use --upload and --upload-file in one command")

    if options.upload_file:
        try:
            f = open(options.upload_file)
            f.close()
        except (IOError, OSError) as e:
            parser.error("Cannot open file %s for reading: %s" %
                         (options.upload_file, e.strerror))
    elif options.upload_file == "":
        parser.error("Empty-string is not a valid argument for --upload-file")

    if not (options.upload or options.upload_file) and (
               options.upload_user or
               options.case_number or
               options.upload_description):
        parser.error("Upload values provided, but no upload mode chosen: you need --upload or --upload-file")

    return options, args

def read_config(app_infos=None):
    """ read the conf-file configuration defaults & local static config from
        server.conf. These settings are a large subset of the commandline
        options.
        There's no support, for example for basename, stdout, or debug here."""
    # XXX DIAG_EXT: need way for app to augment this if needed; or infer
    # anything that needs inference; i think that means that read_config() needs
    # to be moved under control of local_getopt()
    file_options = {}

    server_conf = get_server_conf()
    diag_stanza = server_conf.get('diag', {})

    exclude_terms = [v for (k, v) in diag_stanza.items() if k.startswith("EXCLUDE")]
    file_options['exclude_list'] = exclude_terms

    # search string filtering in the case that people type or
    # drilldown into Personally Identifying Information data
    simple_regexes = {}
    luhn_regexes = {}
    for setting_name, regex in diag_stanza.items():
        if not setting_name.startswith('SEARCHFILT'):
            continue
        if not "-" in setting_name:
            continue
        tag_name = setting_name.split('-', 1)[1]
        if not tag_name:
            continue

        if setting_name.startswith("SEARCHFILTERSIMPLE"):
            simple_regexes[tag_name] = regex
        elif setting_name.startswith("SEARCHFILTERLUHN"):
            luhn_regexes[tag_name] = regex
        else:
            logger.warn("Unrecognized SEARCHFILTER setting name, ignoring:" +
                        "%s = %s", setting_name, regex)
    file_options['simple_searchfilters'] = simple_regexes
    file_options['luhn_searchfilters'] = luhn_regexes

    default_uri = "https://api.splunk.com"
    upload_uri = diag_stanza.get('upload_proto_host_port', default_uri)
    if upload_uri == "disabled":
        upload_uri = None # explicit disabled
    if upload_uri:
        parsed_obj = urllib_parse.urlparse(upload_uri)
        if not parsed_obj.scheme or not parsed_obj.netloc: # implicit disabled
            msg = "upload_proto_host_port value '%s' in server.conf [diag] is malformed."
            msg += "  Will treat as disabled."
            logger.warn(msg, upload_uri)
            upload_uri = None
    file_options['upload_uri'] = upload_uri

    other_settings = ("all_dumps", "index_files",  "log_age", "components")
    for setting in other_settings:
        if setting in diag_stanza:
            file_options[setting] = diag_stanza[setting]

    numeric_settings = ('log_age', 'etc_filesize_limit', 'log_filesize_limit')
    for num_sett in numeric_settings:
        if num_sett in file_options:
            try:
                file_options[num_sett] = int(file_options[num_sett])
            except ValueError as e:
                msg = "Invalid value '%s' for %s, must be integer."
                logger.error(msg % (file_options[num_sett], num_sett))
                raise

    if "components" in file_options:
        comp_list = file_options['components'].split(',')
        comp_list = [s.strip() for s in comp_list]
        all_components = set(KNOWN_COMPONENTS).union(set(app_components()))
        if 'all' in comp_list:
            file_options['components'] = all_components
        else:
            wanted_components = set(comp_list)
            unknown_components = wanted_components.difference(all_components)
            if unknown_components:
                as_string = ",".join(unknown_components)
                msg = "Unknown components listed in server.conf: '%s'" % as_string
                logger.error(msg)
                raise ValueError(msg)
            file_options['components'] = wanted_components

    # Some apps have app.conf [diag] directives for us, possibly.
    default_lookup_includes = {}
    if app_infos:
        for app_info in app_infos:
            # for now all we have is default_gather_lookups
            gatherlookup_settings = [v for (k, v) in app_info.diag_conf.items() if k.startswith("default_gather_lookups")]
            if not gatherlookup_settings:
                continue

            lookup_includes = []
            for item in gatherlookup_settings:
                entries = item.split(",")
                entries = [s.strip() for s in entries]
                entries = [_f for _f in entries if _f]
                lookup_includes.extend(entries)

            default_lookup_includes[app_info.app_dir] = lookup_includes

    file_options['default_lookup_includes'] = default_lookup_includes
    logger.debug("default lookups: %s", default_lookup_includes)

    return file_options

def build_filename_filters(globs):
    if not globs:
        return []
    glob_to_re = lambda s: re.compile(fnmatch.translate(s))
    return list(map(glob_to_re, globs))


# Next four functions are for tracking for files we did not put in the diag,
# that we might have normally done
excluded_filelist = []
def reset_excluded_filelist():
    "Wipe it, just in case we ever make this module persistent"
    global excluded_filelist
    excluded_filelist = []

def add_sslcert_file_to_excludes(path):
    """tell the file exclude tracker that an absolute path has not been included
       because it looks like an SSL cert"""
    logger.debug("add_sslcert_file_to_excludes(%s)" % path)
    if path.startswith(SPLUNK_HOME):
        path = path[len(SPLUNK_HOME):]
        path = get_diag_name() + path
    reason = "CERT"
    excluded_filelist.append((reason, path))


def add_excluded_file(path):
    """tell the file exclude tracker that a diag-relative path has not been included
       even though the component is enabled"""
    logger.debug("add_excluded_file(%s)" % path)
    reason = "EXCLUDE"
    excluded_filelist.append((reason, path))

def get_excluded_filelist():
    return excluded_filelist

def excluded_filelist_to_str(excl_list):
    """render the excluded list as a string for writing out to a file """
    # this is gross and I should probably build a clever object with a
    # progressive read method... later.
    lines = []
    for reason, filename in excl_list:
        padding = 10 - len(reason)
        lines.append("%s:%s%s" % (reason, " " * padding,  filename))
    return "\n".join(lines) + "\n"

def write_excluded_filelist(excl_list, tfile):
    """add the excluded list to a passed-in tarfile
       Dead code, kill it soon"""  # XXX
    exluded_filelist_text = excluded_filelist_to_str(excl_list)

    # at least free the memory
    reset_excluded_filelist()

    tinfo = tarfile.TarInfo("%s/%s" % (get_diag_name(), "excluded_filelist.txt"))
    tinfo.size = len(buf)
    tinfo.mtime = time.time()
    fake_file = StringIO(buf)
    tfile.addfile(tinfo, fileobj=fake_file)

def create_tar(options, log_buffer):
    """Create a tar from a directory of files

       Dead code, kill it soon  """ #XXX
    #create a tar.gz file out of the results dir
    logger.info("Creating archive file...")
    # use gzip default compression level of 6 for (much more) speed
    destFile = tarfile.open(get_tar_pathname(), 'w:gz',  compresslevel=6)
    destFile.add(get_results_dir(), get_diag_name(), exclude=path_unwanted)

    write_excluded_filelist(get_excluded_filelist(), destFile)

    # now add the log buffer for all output during diag run
    tinfo = tarfile.TarInfo("%s/%s" % (get_diag_name(), "diag.log"))
    tinfo.size = log_buffer.len
    tinfo.mtime = time.time()
    log_buffer.seek(0)
    destFile.addfile(tinfo, fileobj=log_buffer)

    destFile.close()

# If you add new units of work to create diag, please list them here so
# the unit tests will stub them out correctly
create_diag_work_functions = [
        'gather_rest_content',
        'get_listing',
        'kvStoreListing',
        'splunkDBListing',
        'copy_etc',
        'copy_logs',
        'copy_indexfiles',
        'copy_dispatch_dir',
        'copy_raft_dir',
        'create_tar',
        'create_tar',
        'gather_app_extensions',
        ]
original_workers = {}
for worker in create_diag_work_functions:
    original_workers[worker] = globals()[worker]

def create_diag(options, log_buffer):
    """ According to the options, create a diag """
    reset_excluded_filelist()
    filter_list = build_filename_filters(options.exclude_list)
    set_storage_filters(filter_list)

    if not options.stdout:
        # don't "clean up" in --stdout mode, or -uri localhost fails bizarrely
        if os.path.exists(get_tar_pathname()):
            os.unlink(get_tar_pathname())

    # initialize whatever is needed for the storage type
    storage.setup(options)

    # make sure it's a supported os.
    if not os.name in ("posix", "nt"):
        logger.error("FAIL: Unsupported OS (%s)." % os.name)
        write_logger_buf_on_fail(log_buffer)
        sys.exit(1)

    logger.info("Starting splunk diag...")

    setup_filters(options)

    # do rest first for now, simply to force interactive work to occur first
    if not 'rest' in options.components:
        logger.info("Skipping REST endpoint gathering...")
    else:
        gather_rest_content(options)

    sysinfo_filename = None

    try:
        try:
            global system_info
            system_info = tempfile.NamedTemporaryFile(prefix="splunk_sysinfo", mode="w+", delete=False)
            sysinfo_filename = system_info.name
        except IOError:
            logger.error("Exiting: Cannot create system info file.  Permissions may be wrong.")
            write_logger_buf_on_fail(log_buffer)
            sys.exit(1)

        logger.info("Determining diag-launching user...")
        # who is running me?
        systemUsername()

        # Log facts about SPLUNK_HOME & SPLUNK_ETC
        text = get_env_info()
        system_info.write(text)

        logger.info("Getting version info...")
        #get the splunk version
        splunkVersion()

        logger.info("Getting system version info...")
        #uname
        systemUname()

        # splunk valiate files
        if 'file_validate' in options.components:
            logger.info("Getting file integrity info...")
            output = get_file_validation()
            system_info.write(output)

        logger.info("Getting network interface config info...")
        #ifconfig
        networkConfig()

        logger.info("Getting splunk processes info...")
        text = get_process_listing()
        system_info.write(text)

        logger.info("Getting netstat output...")
        #netstat
        networkStat()

        logger.info("Getting info about memory, ulimits, cpu (on windows this takes a while)...")
        #ulimit
        systemResources()

        logger.info("Getting etc/auth filenames...")
        get_listing(os.path.join(get_splunk_etc(), 'auth'), 'auth')

        logger.info("Getting Sinkhole filenames...")
        get_listing(os.path.join(SPLUNK_HOME, 'var', 'spool', 'splunk'), 'sinkhole')

        desc = 'search peer bundles'
        if 'searchpeers' in options.components:
            logger.info('Getting %s listings...' % desc)
            src = os.path.join(SPLUNK_HOME, 'var', 'run', 'searchpeers')
            get_listing(src, desc)
        else:
            logger.info('Skipping %s listings...' % desc)

        desc = 'conf replication summary'
        if 'conf_replication_summary' in options.components:
            logger.info('Getting %s listings...' % desc)
            src = os.path.join(SPLUNK_HOME, 'var', 'run', 'splunk', 'snapshot')
            get_listing(src, desc)
        else:
            logger.info('Skipping %s listings...' % desc)

        desc = 'suppression files'
        if 'suppression_listing' in options.components:
            logger.info('Getting %s listings...' % desc)
            src = os.path.join(SPLUNK_HOME, 'var', 'run', 'splunk', 'scheduler', 'suppression')
            get_listing(src, desc)
        else:
            logger.info('Skipping %s listings...' % desc)

        if 'kvstore' in options.components:
            kvStoreListing()
        else:
            logger.info("Skipping KV Store listings...")

        if 'index_listing' in options.components:
            #ls
            logger.info("Getting index listings...")
            splunkDBListing(options)
        else:
            logger.info("Skipping index listings...")

    finally:
        system_info.close()

    try:
        add_file_to_diag(sysinfo_filename, SYSINFO_FILE)
    finally:
        os.unlink(sysinfo_filename)

    if not 'etc' in options.components:
        logger.info("Skippping Splunk configuration files...")
    else:
        #copy etc and log into results too
        logger.info("Copying Splunk configuration files...")
        copy_etc(options)

    if not 'log' in options.components:
        logger.info("Skipping Splunk log files...")
    else:
        logger.info("Copying Splunk log files...")
        copy_logs(options)

    if not 'index_files' in options.components:
        logger.info("Skipping index files...")
    else:
        # TODO: try to link these lines to the actual work more strongly
        if options.index_files == "full":
            logger.info("Copying index worddata, and bucket info files...")
        else:
            logger.info("Copying bucket info files...")
        copy_indexfiles(options.index_files)

    # TODO: combine next two blocks
    # There's no need to make this a component, it's a single file
    if not os.path.exists(COMPOSITE_XML):
        logger.warn("Unable to find composite.xml file, product has likely not been started.")
    else:
        try:
            add_file_to_diag(COMPOSITE_XML, os.path.basename(COMPOSITE_XML))
            #shutil.copy(COMPOSITE_XML, get_results_dir())
        except IOError as e:
            # windows sucks
            err_msg = "Error copying in composite.xml: '%s' continuing..."
            logger.warn(err_msg % e.strerror)

    client_serverclass_file = "serverclass.xml"
    client_serverclass_path = os.path.join(SPLUNK_HOME, "var", "run",
                                           client_serverclass_file)
    if os.path.exists(client_serverclass_path):
        try:
            logger.info("Adding deployment client info file 'serverclass.xml'.")
            add_file_to_diag(client_serverclass_path, client_serverclass_file)
        except IOError as e:
            # windows sucks
            err_msg = "Error copying in serverclass.xml: '%s' continuing..."
            logger.warn(err_msg % e.strerror)



    if not 'dispatch' in options.components:
        logger.info("Skipping Splunk dispatch files...")
    else:
        logger.info("Copying Splunk dispatch files...")
        copy_dispatch_dir()

    if not 'consensus' in options.components:
        logger.info("Skipping Splunk consensus files...")
    else:
        logger.info("Copying Splunk consensus files...")
        copy_raft_dir()

    # again.. so small...
    if os.name == "nt":
        logger.info("Copying windows input checkpoint files...")
        copy_win_checkpoints()
    copy_scripts()
    copy_manifests()
    copy_cachemanager_upload()

    gather_app_extensions(options)

    # write out all the files that have been skipped
    excl_filelist = get_excluded_filelist()
    add_string_to_diag(excluded_filelist_to_str(excl_filelist),
                       "excluded_filelist.txt")

    # Add any exception texts for apps that had an exception during setup()
    if app_setup_exceptions_dict:
        # non-empty
        logger.info("Adding app exception texts to diag....")
        for app_name, exception_text in app_setup_exceptions_dict.items():
            output_path = os.path.join("app_ext", app_name, "setup_failed.output")
            add_string_to_diag(exception_text, output_path)

    log_buffer.write("diag.log complete\n")
    # now add the log buffer for all output during diag run
    add_string_to_diag(log_buffer.getvalue(), "diag.log")

    storage.complete(options, log_buffer)

def logging_horrorshow():
    disable_clilib_logger()
    buffer_obj = setup_buffer_logger()
    setup_main_logger()
    return buffer_obj

def disable_clilib_logger():
    if not splunk.clilib.log_handlers:
        # can't control cli.py logging
        if '--stdout' in sys.argv:
            # logging here as special case; don't have our own logger yet.
            logger.error("stdout streaming requested, but this splunk version cannot support log adjustment")
            logger.error("tar will probably be corrupted")
        else:
            pass
            # we just use default loggers without complaint
        return
    stdout_handler = splunk.clilib.log_handlers.get('stdout')
    if stdout_handler:
        # if we didn't start via cli.py, stdout_handler is None
        l = logging.getLogger()
        l.removeHandler(stdout_handler)
        if hasattr(splunk.clilib.log_handlers, 'remove'):
            splunk.clilib.log_handlers.remove('stdout')
    stderr_handler = splunk.clilib.log_handlers.get('stderr')
    if stderr_handler:
        l = logging.getLogger()
        l.removeHandler(stderr_handler)
        if hasattr(splunk.clilib.log_handlers, 'remove'):
            splunk.clilib.log_handlers.remove('stderr')

    # the removes are only needed to hide a complainy message from a logging
    # module bug; so if on a retro spplunk version, we don't need it.

def setup_main_logger():
    """Creates a logger that is a child of the buffer logger (below)
    Messages that are bound for the screen land here, and flow on to the buffer
    logger.
    """
    global logger

    if '--debug' in sys.argv:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logger = logging.getLogger("diag.general")
    logger.setLevel(log_level)

    if '--stdout' in sys.argv:
        # log everything to stderr (default for streamhandler)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sh)
    else:
        # info to stdout
        out_h = logging.StreamHandler(sys.stdout)
        out_h.setFormatter(logging.Formatter("%(message)s"))
        # True == INFO only
        out_h.addFilter(splunk.clilib.cli_common.FilterThreshold(True))
        logger.addHandler(out_h)

        err_h = logging.StreamHandler(sys.stderr)
        err_h.setFormatter(logging.Formatter("%(message)s"))
        # False == anything other than INFO
        err_h.addFilter(splunk.clilib.cli_common.FilterThreshold(False))
        logger.addHandler(err_h)

def setup_buffer_logger():
    "Creates and returns a StringIO object which will receive all the logged mesages during run"
    global auxlogger

    if '--debug' in sys.argv:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    str_io_buf = StringIO()
    s_io_handler = logging.StreamHandler(str_io_buf)
    s_io_fmtr = logging.Formatter('%(asctime)s: %(levelname)s %(message)s', '%d/%b/%Y:%H:%M:%S')
    s_io_handler.setFormatter(s_io_fmtr)
    mem_logger = logging.getLogger("diag") # this is is a parent of the general logger

    auxlogger = mem_logger
    auxlogger.setLevel(log_level)
    auxlogger.addHandler(s_io_handler)

    return str_io_buf

def write_logger_buf_on_fail(buf):
    "On failure, try to capture the logged messages somewhere that they won't be mutilated"
    tf = tempfile.NamedTemporaryFile(prefix="diag-fail-", suffix='.txt', delete=False, mode='w+t')
    logger.info("Diag failure, writing out logged messages to '%s', please send output + this file to either an existing or new case ; http://www.splunk.com/support" % tf.name)
    tf.write(buf.getvalue())
    tf.close()

def adjust_logging(options):
    # Special wonky feature for remote-diag, dumps to file so endpoint can see
    # the data.
    if options.statusfile:
        statushandler = logging.FileHandler(options.statusfile, mode="w")
        #logging.Formatter('%(asctime)s: %(message)s', '%d/%b/%Y:%H:%M:%S')
        #statushandler.setFormatter(
        logger.addHandler(statushandler)

def get_appinfos(scan_path):
    """For a target directory, return a dict
       basename -> AppInfo
    """
    appinfos = {}
    if not hasattr(splunk.clilib.cli_common, 'getAppConf'):
        # old splunk version, no app extensions
        return appinfos

    if not os.path.isdir(scan_path):
        return appinfos
    try:
        entries = os.listdir(scan_path)
    except:
        return appinfos

    for dirent in entries:
        ent_path = os.path.join(scan_path, dirent)
        if os.path.isdir(ent_path):
            app_conf = splunk.clilib.cli_common.getAppConf(confName='app',
                                                           app=dirent,
                                                           use_btool=False,
                                                           app_path=ent_path)
            diag_conf = app_conf.get('diag', {})
            appinfos[dirent] = AppInfo(app_name=dirent,
                                       app_dir=ent_path,
                                       diag_conf=diag_conf)
    return appinfos

def discover_apps():
    """Load and handle diag stanza from all apps that have them.
    Used for diag app extensions, and lookup-filter overrides (so far).

    returns ext_app_map, diag_conf_list

    where: ext_app_map: app_name -> AppInfo for all diag-extending apps
           diag_conf_list: [AppInfo...] for all apps with app.conf [diag] conf

    """
    all_appdir_confs = {}

    # look for apps in the normal place
    plain_app_dir = os.path.join(get_splunk_etc(), 'apps')
    #plain_apps = get_dirs(plain_app_dir)
    plain_apps = get_appinfos(plain_app_dir)


    # and the less-normal place (index clustering)
    slave_app_dir = os.path.join(get_splunk_etc(), 'slave-apps')
    #slave_apps = get_dirs(slave_app_dir)
    slave_apps = get_appinfos(slave_app_dir)


    all_diag_conf_apps = []
    # Okay, one thing we're going to return here is the set of all apps with
    # app.conf [diag] stanzas, for any non-extension purposes, so build that.
    for app_info in itertools.chain(plain_apps.values(), slave_apps.values()):
        if app_info.diag_conf:
            all_diag_conf_apps.append(app_info)

    warned = False
    # do we have any collisions?
    plain_v_slave = set(plain_apps) & set(slave_apps)
    if plain_v_slave:
        logger.warn(" Apps exist in both the apps and the slave-apps dir: " +
                     ",".join(plain_v_slave))
        warned = True

    if warned:
        logger.warn("Duplicate apps will be skipped for app-collection purposes")

    # now build our dict of non-colliding apps.
    # I'm sure there's some simpler way to do this.
    unique_app_set = set(plain_apps) ^ set(slave_apps)

    combined_apps =  {}
    combined_apps.update(plain_apps)
    combined_apps.update(slave_apps)

    # NOTE: explicitly create list of keys to prevent
    # `RuntimeError: dictionary changed size during iteration` on Py3
    for key in list(combined_apps):
        if key not in unique_app_set:
            del combined_apps[key]

    extending_apps = []

    for app in sorted(combined_apps):
        app_info = combined_apps[app]
        extension_script = app_info.diag_conf.get('extension_script')
        data_limit_s = app_info.diag_conf.get('data_limit', "100MB")

        if not extension_script:
            continue

        data_limit = None # use system default
        if data_limit_s:
            try:
                data_limit = parse_human_sizestr(data_limit_s.strip())
            except ValueError as e:
                msg = "Error parsing app-specific data_limit setting '%s'. " % data_limit_s
                msg += str(e)
                msg += " Will use global value '%s'" % "100MB"
                logger.error(msg)

        # the app wants us to run it .. is the file there?
        script_path = os.path.join(app_info.app_dir, 'bin', extension_script)
        if os.path.isfile(script_path):
            # good enough, we'll collect this one
            app_info.add_extension_info(diag_extension_file = script_path,
                                        data_limit = data_limit)
            extending_apps.append(app_info)
        else:
            msg = ("App %s declared it wanted provide " +
                   "diag-extensions, but the script could " +
                   "not be located at %s")
            logger.warn(msg, app, script_path)

    return extending_apps, all_diag_conf_apps

def import_app_ext(app_info):
    """ accepts an AppInfo object, which describes the app name and the file
        implementing the diag extension

        returns None

        Side effects:
            - loads the module and attaches it to the AppInfo object
    """

    import imp # XXX DIAG_EXT: python 3 may require this to be done with importlib
    try:
        # ask python to read in the module as if we imported it
        module = imp.load_source(app_info.module_name,
                                 app_info.diag_extension_file)
        app_info.add_module(module)

    except IOError as e:
        # load_source() doesn't bother to provide the filename as part of the
        # exception for some reason????
        if e.errno == errno.ENOENT:
            if not hasattr(e, "filename") or not e.filename:
                e.filename = app_info.diag_extension_file
        raise e

    if hasattr(module, 'setup'):
        # setup function isn't required, but if it's present it should conform
        # to certain requirements.
        assert hasattr(module, 'setup') and "No setup function offered in diag extension"
        assert isinstance(module.setup, type(main)) and "No setup attribute is not a function in diag extension"

        # evil diggging
        setup_argspec = inspect.getargspec(module.setup)
        assert setup_argspec.keywords and "To support interface change, setup() must support **kwargs"

    # collection function is non-optional.
    assert hasattr(module, 'collect_diag_info') and "No collect_data_info() function offered in diag extension"
    assert isinstance(module.collect_diag_info, type(main)) and "collection_data_info is incorrectly not a function"

    assert isinstance(module.collect_diag_info, type(main)) and "collection_data_info is incorrectly not a function"

    collect_argspec = inspect.getargspec(module.collect_diag_info)

    assert collect_argspec.keywords and "To support interface change, collect_diag_info() must support **kwargs"

    # XXX maybe these hacks aren't needed
    #info_gather.__dict__[app_info.app_name] = module
    #info_gather.test_app.foobar = 4

    return None

def main():
    # We want all logged messages to hit our custom in-memory StringIO buffer;
    # but only *SOME* messages to land on the terminal.
    log_buffer = logging_horrorshow()

    # handle options

    extending_app_infos, diagconf_app_infos = discover_apps()
    prepared_app_modules = {}
    for app_info in extending_app_infos:
        try:
            import_app_ext(app_info) # modifies app_info
            prepared_app_modules[app_info.app_name] = app_info
        except:
            msg = "failure encountered initializing diag extensions for app %s..."
            logger.error(msg % app_info.app_name)
            logger.error(traceback.format_exc())
            logger.error("...proceeding")

    _store_prepared_app_info(prepared_app_modules)

    # hack.. if we're doing --uri, then we don't want to use our locally
    # configured conf file; proper solution is probably to parse arguments
    # first, and then handle file defaults after, I suppose, or to split arg
    # parsing into two phases.
    file_options = {}
    if not '--uri' in "".join(sys.argv):
        file_options = read_config(app_infos=diagconf_app_infos)

    logger.debug("app_modules: %s" % app_ext_names())
    options, args = local_getopt(file_options)

    if options.auth_on_stdin:
        token = sys.stdin.readline().strip()
        set_session_key(token)

    adjust_logging(options)
    if options.diagname:
        set_diag_name(options.diagname)

    if options.uri:
        remote_diag(options, log_buffer)
        return True # Done with success

    if options.statusfile:
        # this is typically used remotely; don't have another clear way of
        # knowing this at the moment.
        logger.info("Remote command line was: %s" % sys.argv)

    # messy, complete info on options no one wants to see...
    auxlogger.info("The full set of options was: %s" % options)
    auxlogger.info("The set of requested components was: %s" % sorted(list(options.components)))

    if not options.upload_file:
        # short version that's a bit more digestible
        logger.info("Collecting components: %s", ", ".join(sorted(list(options.components))))

        off_components = set(KNOWN_COMPONENTS).difference(set(options.components))
        logger.info("Skipping components: %s", ", ".join(sorted(list(off_components))))

    if options.upload or options.upload_file:
        sys.stdout.write("\n") # blank line, not logged
        if options.upload_file:
            if not upload_file(options.upload_file, 'splunkcom', options):
                #TODO: pass return value back through clilib
                sys.exit("Upload failed.")
            return True # --upload-file means no create_diag()

        else:
            # generate and upload a diag; just ensure they have the prereqs
            if not ensure_upload_options(options):
                #TODO: pass return value back through clilib
                sys.exit("\nCannot validate upload options, aborting...")
                #return False

    # Do the work, trying to ensure the temp dir gets cleaned even on failure
    try:
        # Obey options? Dump this configurability? TODO
        #set_storage("in_memory")
        #set_storage("directory")
        set_storage("tar")

        # if the rest component is used, or if an app declared it wanted to do
        # rest, then do logins and so on now.
        if 'rest' in options.components or rest_needed:
            prepare_local_rest_access(options)

        create_diag(options, log_buffer)
    except Exception as e:
        logger.error("Exception occurred while generating diag, we are deeply sorry.")
        logger.error(traceback.format_exc())
        # this next line requests a file to be logged, not sure of clearest order
        write_logger_buf_on_fail(log_buffer)
        logger.info("We will now try to clean out any temporary files...")
        clean_temp_files()
        os.unlink(get_tar_pathname())
        #TODO: pass return value back through clilib
        sys.exit(1)
        #return False

    # and for normal conclusion..
    try:
        #clean up the results dir
        logger.info("Cleaning up...")
        clean_temp_files()
    finally:
        if not options.statusfile: # TODO better way to know if run remotely
            logger.info("Splunk diagnosis file created: %s" % get_tar_pathname())

    if options.upload:
        logger.info("Will now upload result.")
        if upload_file(get_tar_pathname(), 'splunkcom', options):
            logger.info("You may wish to delete the diag file now.")
        else:
            logger.warn("Upload failed")
            if not chunk_complained:
                msg = "You may want to try uploading again with 'splunk diag --upload-file %s'"
                logger.info(msg, get_tar_pathname())
            #TODO: pass return value back through clilib
            sys.exit(1)
            #return False
    #return True

def remote_diag_status(options):
    diagstatus_url = options.uri + "/services/server/status/diag"
    http_params = [('ticket', 'default')]
    encoded_params = urllib_parse.urlencode(http_params)
    try:
        diagstatus_req = urllib_request.Request("%s?%s" % (diagstatus_url, encoded_params))
        diagstatus_req.add_header("Authorization", "Splunk %s" % get_session_key())
        try:
            diagstatus_f = urllib_request.urlopen(diagstatus_req, context=getSSLContext())
        except TypeError as e:
            diagstatus_f = urllib_request.urlopen(diagstatus_req)
        text = diagstatus_f.read()
        if text:
            return text
    except urllib_error.HTTPError as error:
        # this is expected if pointing at an older version of Splunk
        return None



def remote_diag(options, log_buffer):
    key = cli_get_sessionkey(options.uri)
    if key:
        set_session_key(key)
    if ping(options.uri):
        logger.info("  Already logged in.")
    else:
        logger.info("  Not logged in.  Please log in now.  (You can avoid this logging in ahead with 'splunk login --uri https://servername:port'.")
        if not interactive_restsession_setup(options.uri, clustering=False):
            logger.error("Cannot proceed with remote diag")
            return

    # Normal commandline parsing flattens the parameters.
    # let's not trust local parsing behaves same as remote parsing, and simply
    # pass the command line along

    def add_arg(option_list, arg, value):
        """ Helper to build up the argument list to render as HTTP params
            we use a dumb hack of NOVAL to represent arguments that do not have
            values, eg --all-dumps

            mutates input option_list
        """
        if arg == "uri":
            # we don't want to ask the remote system to do a remote diag on
            # itself
            return
        if not value:
            value = "NOVAL"
        option_list.append((arg, value))

    got_to_options = False
    option_list = []
    cur_opt = cur_val = None


    # we always need to pass along the selected name so the filename and the tar
    # contents agree; we don't do this in the general diag machinery because it
    # gets a bit twisty between remote and local logic
    if options.diagname:
        our_diagname = options.diagname
    else:
        url_parts = urllib_parse.urlparse(options.uri)
        our_diagname = format_diag_name(url_parts.hostname)
    our_tarname = get_tar_pathname(filename=our_diagname)
    logger.error("selected output filename of %s", our_tarname)

    add_arg(option_list, "basename", our_diagname)

    for arg in sys.argv:
        # sometimes we have ['cli.py', 'diag", ...]
        # other times we have ['info_gather.py', ...]
        # so we fish for the first thing starting with - :-(
        # TODO: move this normalization to main()
        if not got_to_options:
            if not arg.startswith("-"):
                continue
            got_to_options = True
        # we've already encountered one option, so now we test for '-' again to
        # determine if it's an option or option-value
        if arg.startswith("-"):
            # starting a new option, store any prior to option_list
            if cur_opt:
                add_arg(option_list, cur_opt, cur_val)
                cur_opt = cur_val = None
            cur_opt = arg.lstrip('-') # going to assume all options work with --
        elif not cur_val:
            cur_val = arg
        else:
            # case like: --flag thing1 thing2
            # thing 2 isn't part of an option, but a positional argument.
            # Currently diag silently ignores these, so we do here too.
            logger.debug("Got unexpected positional argument: %s", arg)
            pass
    # handle the last option
    if cur_opt:
        add_arg(option_list, cur_opt, cur_val)
    cur_opt = cur_val = None

    #option_list.append(("Authorization", "Splunk %s" % get_session_key()))
    encoded_options = urllib_parse.urlencode(option_list)

    diagdata_url = options.uri + "/services/streams/diag"

    # loop vars
    diag_status_failed = False
    prior_status_size = 0
    last_check_time = time.time()

    with open(our_tarname, "wb") as out_f:
        diagdata_req = urllib_request.Request("%s?%s" % (diagdata_url, encoded_options))
        diagdata_req.add_header("Authorization", "Splunk %s" % get_session_key())
        diagdata_req.add_header("Accept-encoding", "gzip")
        try:
            diagdata_f = urllib_request.urlopen(diagdata_req, context=getSSLContext())
        except TypeError as e:
            diagdata_f = urllib_request.urlopen(diagdata_req)

        first_read = True
        while True:
            if first_read:
                # small read to start printing progress info quickly
                chunk = diagdata_f.read(1024)
                first_read = False
            else:
                chunk = diagdata_f.read(1024 * 1024) # 1MB
            if not chunk:
                break

            out_f.write(chunk)
            # then see if we have more progress text to show
            if not diag_status_failed:
                if (time.time() - last_check_time) < 2:
                    continue
                last_check_time = time.time()
                status_output = remote_diag_status(options)
                if not status_output:
                    diag_status_failed = True
                    continue
                status_lines = status_output.splitlines()
                to_print = status_lines[prior_status_size:]
                for line in to_print:
                    logger.info(line)
                prior_status_size = len(status_lines)
        diagdata_f.close()
    if not diag_status_failed:
        # one last time, should show failure causes, as well as completion
        status_output = remote_diag_status(options)
        status_lines = status_output.splitlines()
        to_print = status_lines[prior_status_size:]
        for line in to_print:
            logger.info(line)
    logger.info("Splunk diagnosis file retreived to: %s", our_tarname)

def pclMain(args, fromCLI):
    main()

#######
# upload logic

class FileWrappingProgressBar(object):
    """Draw a progress bar on the terminal as the wrapped file-like readable
    object is read() from.
    This requires the size of the item (in bytes) be fixed and determinable."""
    def __init__(self, readable_obj, bar_size=59, read_max=None):
        """If read_max can be autoset if readable_obj is a real file
        ( ie, if os.fstat(obj.fileno()) returns the size."""
        self.wrapped_f = readable_obj
        if not read_max:
            # intentionally throws exception
            read_max = os.fstat(readable_obj.fileno()).st_size
        self.read_max = read_max
        self.bar_size = bar_size
        self.bytes_read = 0
        self.bar_drawn = 0
        self.percent_shown = 0

    def __len__(self):
        return self.read_max

    def read(self, size=-1):
        data = self.wrapped_f.read(size)
        self.bytes_read += len(data)
        self._update_bar()
        return data

    def _update_bar(self):
        bar_draw_target = int(self.bytes_read * 1.0 / self.read_max * self.bar_size)
        cur_percent = int(self.bytes_read * 1.0 / self.read_max * 100)

        if bar_draw_target > self.bar_drawn or cur_percent > self.percent_shown:
            self.percent_shown = cur_percent
            self.bar_drawn = bar_draw_target
            self._draw_bar()

    def _draw_bar(self):
        "in practice, draws between 1 and bar_size"
        if self.percent_shown >= 100:
            pct_text = "Done.\n"
        else:
            pct_text = "%2i" % self.percent_shown + "%"
        # includes an arrow and a sized space gap
        arrow_text = "-" * (self.bar_drawn-1) + ">" + " "*(self.bar_size-self.bar_drawn)
        bar_text = "%s|%s" % (arrow_text, pct_text)
        # rewind cursor to start of line, and draw new bar
        sys.stdout.write("\r" + bar_text)

    def close(self):
        self.wrapped_f.close()


class FileSlice(object):
    """Wraps a file object, and acts as a file that contains a subset of the
    wrapped file.
    The file is owned by this object for its duration, it can't be shared, and
    reads, seeks, etc of the underlying file object will break the behavior."""
    def __init__(self, f, bytestart, byteend):
        self.f = f
        self.bytestart = bytestart
        self.byteend = byteend
        self.f.seek(bytestart)

    def read(self, size=-1):
        fp_before_read = self.f.tell()
        # already at EOF?
        if fp_before_read >= self.byteend:
            return ""
        # would the read exceed EOF? adjust read size
        if size > 0 and fp_before_read + size > self.byteend:
            size = self.byteend - fp_before_read

        data = self.f.read(size)

        # did we read past EOF? (could happen for size -1)
        if fp_before_read + len(data) > self.byteend:
            should_be_avail = self.byteend - fp_before_read
            # trim
            data = data[:should_be_avail]
        return data

    def close(self):
        self.f.close()

class MenuState(object):
    """Remember enough information about the text menu display state to provide
    sane interactive behavior.
    Really we just remember the set of items out of the total list that were
    displayed last time, so we can "drag" the display as the user cursors past
    the edge."""
    def __init__(self):
        self.firstdisplayed = 0
        self.lastdisplayed = 0

    def first_pass(self):
        if self.firstdisplayed or self.lastdisplayed:
            return False
        return True

    def __repr__(self):
        return "[start=%s, end=%s]" % (self.firstdisplayed, self.lastdisplayed)

class DisplayableCaseData(object):
    "Represent values from a case in the spulnk support portal."
    def __init__(self, jsonblob):
        self.case_number = int(jsonblob['case_number'])
        self.case_id = jsonblob['case_id']
        self.title = jsonblob['title']
        self.description = jsonblob['description']
        self.priority = jsonblob['priority']
        self.status = jsonblob['status']
        self.product = jsonblob['product']

    def renderlines(self):
        "Give a brief terminal-friendly representation of the values."
        lines = []
        if len(self.title) < 60:
            displaytitle = self.title
        else:
            # 'abstract' it
            displaytitle = self.title[:60] + "..."
        lines.append("%s %s '%s'" % (self.case_number, self.priority, displaytitle))

        for descline in self.description.splitlines():
            if not descline.strip():
                continue
            logger.debug("Starting with descline: %s" % descline)

            while len(descline) > 75:
                logger.debug("... breaking the line")
                # break line
                lines.append(descline[:75])
                descline = descline[75:]
                logger.debug("line is now: %s" % descline)

            lines.append(descline)
            if len(lines) > 8:
                logger.debug("linecount is now : %s" % len(lines))
                break

        # suggest incompleteness on last line.
        last_linetext = lines[-1]
        if len(last_linetext) >= 69:
            last_linetext = last_linetext[:69]
        last_linetext += " ..."
        lines[-1] = last_linetext

        return lines

def interactive_choice(choices):
    """Accepts a set of objects which TODO are displayed.

    Returns the item selected by the user, or None if no
    selection.
    """
    # start of interface with the first item selected.
    user_selection = 0
    user_choice = None

    # Evil, because we don't know the terminal type, but hopefully most are
    # vt100-ish
    quit_keys = ['\x03', # ctrl-c
                 '\x1c', # ctrl-\
                 '\x1b', # escape
                 '\x1a', # ctrl-z
                 '\x7f', # backspace
                 '\x1b[3~', #delete (vt100)
                 '\xe0S',   #delete (win32 console)
                 'q',
                 'x',
    ]

    select_keys = ['\r', # enter
                   's',
                   'l', # vi for right, or select
                   '\x1b[C', # right-arrow (vt100)
                   '\xe0M',  # right-arrow (win32 console)
    ]

    next_keys = ['\x1b[B', # down-arrow (vt100)
                 '\xe0P',  # down-arrow (win32 console)
                 'j', # vi for down
                 ' ', # space
    ]

    prev_keys = ['k', # vi for up
               '\x1b[A', # up-arrow (vt100)
               '\xe0H',  # up-arrow (win32 console)
               'b', # "back"
               'p', # "previous"
    ]

    detected_terminal_lines = None
    if os.name == "posix":
        try:
            cmd = ["tput", "lines"]
            exit_code, output = simplerunner(cmd, timeout=1)
            if output:
                detected_terminal_lines = int(output)
        except:
            pass

    titlebar = "=== Please select a case using arrow keys, press enter to confirm ==="
    menustate = MenuState()
    while not user_choice:
        if detected_terminal_lines:
            display_menu(user_selection, choices, menustate,
                         titlebar, detected_terminal_lines)
        else:
            display_menu(user_selection, choices, menustate)
        #print("display_menu was called with selection: %s" % user_selection)
        try:
            key = diag_getkey()
        except Exception as e:
            # for any kind of failure, log an error, store the exception in
            # the app_info object, and turn off collection for the app later.
            exception_text = traceback.format_exc()
            logger.error(exception_text)
            logger.warn("Encountered exception reading keystroke.  Aborting menu.")
            return None
        logger.debug("key entered was: %s", key)
        # cancel, no choice
        if key in quit_keys:
            sys.stdout.write("\n")
            return None
        # accept, yes
        if key in select_keys:
            sys.stdout.write("\n")
            return choices[user_selection]
        if key in next_keys:
            user_selection += 1
            if user_selection >= len(choices):
                user_selection = len(choices) -1
        if key in prev_keys:
            user_selection -= 1
            if user_selection < 0:
                user_selection = 0

def clear_screen():
    "Awful hack to clear the screen without using curses & windows console calls"
    if os.name == "nt":
        subprocess.call(["cls"], shell=True)
    else:
        subprocess.call(["clear"])

def render_choice_lines(choice, selected):
    """Format a menu selection for terminal display.
    """
    normal_prepend = "   "
    #first line is special, may contain user indicator
    if selected:
        initial_prepend = "-->"
    else:
        initial_prepend = normal_prepend

    initial_lines = choice.renderlines()
    modified_lines = []

    first_line = initial_lines[0]
    modified_lines.append(initial_prepend + first_line)

    for line in initial_lines[1:]:
        modified_lines.append(normal_prepend + line)

    return modified_lines

def prune_texts(user_selection, choice_texts, menustate, terminal_lines):
    """Slice out a relevant section of information from menu text
    that will fit on the terminal
    """
    logger.debug("beginning prune; target linecount: %s", terminal_lines)
    # first do the short-curcuit
    total_lines = sum(map(len, choice_texts)) + len(choice_texts)*2 -1

    # If all the lines will fit, there's no pruning to do.
    if total_lines <= terminal_lines:
        return choice_texts

    logger.debug("estimated total lines: %s", total_lines)

    # encapsulating internal functions
    def text_walk(user_selection, choice_texts, menustate, accumulate_from):
        """A generator to encapsulate walking the data in different orders in
        different cases.
        """
        if menustate.first_pass():
            text_index = 0
        elif (user_selection < menustate.firstdisplayed or
              user_selection >= menustate.lastdisplayed):
            # cursor outside prior window => start with user cursor location
            text_index = user_selection
        else:
            # in this case, the window hasn't moved, so start with the top
            text_index = menustate.firstdisplayed

        if accumulate_from == "top":
            # Show from the top of the current menu downward until the window is filled
            text_candidates = choice_texts[text_index:]
        else:
            # Show from the user's current choice upward until the window is
            # filled
            text_candidates = reversed(choice_texts[:text_index+1])

        for text in text_candidates:
            yield text_index, text
            if accumulate_from == "top":
                text_index += 1
            else:
                text_index -= 1

    def set_beginning_window_edge(idx, menustate, accumulate_from):
        "Set the window edge that we encounter first in build-order"
        if accumulate_from == "top":
            menustate.firstdisplayed = idx
        else:
            menustate.lastdisplayed = idx

    def set_ending_window_edge(idx, menustate, accumulate_from):
        "Set the window edge that we encounter last in build-order"
        if accumulate_from == "top":
            menustate.lastdisplayed = idx
        else:
            menustate.firstdisplayed = idx

    def trim_text(text_item, linemax, accumulate_from):
        "slice off the top or bottom N lines of an item to fit the term"
        if accumulate_from == "top":
            return text_item[:linemax]     # first N lines
        else:
            return text_item[-linemax:]    # last N lines

    def add_text(item, itemset, accumulate_from):
        if accumulate_from == "top":
            itemset.append(item)
        else:
            itemset.insert(0, item)

    pruned_texts = []  # collected text to return
    linecount = 0      # tracked linecount in pruned_texts

    # normally build the menu from the top down
    accumulate_from = "top"
    if not menustate.first_pass() and user_selection >= menustate.lastdisplayed:
        # unless the user has cursored off the bottom
        accumulate_from = "bottom"

    set_beginning = False
    for idx, text_item in text_walk(user_selection, choice_texts, menustate, accumulate_from):
        if not set_beginning:
            set_beginning_window_edge(idx, menustate, accumulate_from)
            set_beginning = True

        linecount += len(text_item)
        logger.debug("adding item size to linecount, now: %s", linecount)
        if linecount <= terminal_lines:
            # fits entirely, so add it
            add_text(text_item, pruned_texts, accumulate_from)
        else:
            # too big for terminal (or just full), so cut to fit and finish
            trimmed = trim_text(text_item, terminal_lines - linecount,
                                accumulate_from)
            add_text(trimmed, pruned_texts, accumulate_from)
            break # terminal full

        # add idea of double-spacer line and handle overflow
        linecount += 2
        logger.debug("adding blank lines to linecount, now: %s", linecount)
        if linecount >= terminal_lines:
            break

    set_ending_window_edge(idx, menustate, accumulate_from)
    return pruned_texts

def display_menu(user_selection, choices, menustate, titlebar=None, terminal_lines=24):
    "Update the terminal with menu text"
    l = logging.getLogger()
    if not l.isEnabledFor(logging.DEBUG):
        # don't clear the screen in debug
        clear_screen()

    menu_text = render_menu(user_selection, choices, menustate, titlebar,
                            terminal_lines)
    sys.stdout.write(menu_text)

def render_menu(user_selection, choices, menustate, titlebar, terminal_lines=24):
    """user_selection is the integer representing the user's current choice,
       choices is the objects representing the options
       menustate must be a mutable sequence used on consecutive draws, eg an empty list
    """
    choice_texts = []
    logger.debug("target window size: %s", terminal_lines)
    logger.debug("choicecount: %s", choices)

    for choice_num in range(len(choices)):
        selected = user_selection == choice_num
        lines = render_choice_lines(choices[choice_num], selected)
        logger.debug("rendered_choice: %s", lines)
        choice_texts.append(lines)

    # If the set of options won't fit on the screen, throw some away.
    available_lines = terminal_lines
    if titlebar:
        available_lines -= 1
    display_texts = prune_texts(user_selection, choice_texts, menustate,
                                available_lines)
    logger.debug("display_texts: %s", display_texts)

    #flatten the list of lists
    output_lines = []
    if titlebar:
        output_lines.append(titlebar)

    for textset in display_texts:
        output_lines.extend(textset)
        #output_lines.append(' '*3 + '_'*35)
        #output_lines.append(' '*3 + '-'*35)
        output_lines.append('')
        output_lines.append('')
    # drop last (two!) empty lines
    del output_lines[-2:]

    return "\n".join(output_lines)

_located_getkey = None
def diag_getkey():
    """Read a single unbuffered keystroke.

    Turns out this is a relatively complex concept across operating systems and
    their differing ideas of how to presente the data.  The below is an enormous
    hack.
    """
    global _located_getkey

    # jump through hoops to set up a getch
    if not _located_getkey:
        if os.name == "nt":
            import msvcrt
            def win32_getkey():
                firstbyte = msvcrt.getch()
                if firstbyte != '\xe0':
                    return firstbyte
                else:
                    secondbyte = msvcrt.getch()
                    return firstbyte + secondbyte
            _located_getkey= win32_getkey
        else:
            import tty
            import termios
            import fcntl

            def unix_getkey():
                """set stdin to raw, do a 1 byte blocking raw read,
                then a nonblocking read for additional bytes representing keystroke,
                then restore terminal state """
                stdin_fd = sys.stdin.fileno()
                saved_fd_settings = termios.tcgetattr(stdin_fd)

                try:
                    # blocking read of 1 byte
                    tty.setraw(stdin_fd)
                    char = sys.stdin.read(1)
                    # nonblocking read of whatever else is there
                    saved_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
                    fcntl.fcntl(stdin_fd, fcntl.F_SETFL, saved_flags |
                                os.O_NONBLOCK)
                    try:
                        # Hitting this bug in python3, but
                        # specifying a read size avoids the decoder.
                        # See Python-3.7.4/Lib/_pyio.py:2466
                        # and https://bugs.python.org/issue13322
                        rest = sys.stdin.read(16)
                    except IOError:
                        rest = ''
                    finally:
                        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, saved_flags)
                finally:
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN,
                                      saved_fd_settings)
                return char + rest

            _located_getkey = unix_getkey
    # if we get here, we have a getch, so just call it
    return _located_getkey()

def get_upload_description(options):
    """Very simplistic 'text editor' to allow user to describe the upload."""
    ignore = options
    # TODO: implement use of $EDITOR or similar
    clear_screen()

    logger.info("Enter a short (~3000 chars or less) description of the diag/file being uploaded.")
    logger.info('Ex:  diag from staging search head a few hours after the problem happened.')
    logger.info('or.. comparison diag from indexer where the problem does not seem to occur')
    logger.info('')
    logger.info("Press enter twice to finish; use a - or similar for vertical space.")

    input_data = []
    while True:
        textline = input(">")
        textline = textline.strip()
        # XXX: this is evil; we don't know the encoding we're accepting bytes
        # in, but the API breaks if any byte is not "XML valid", so probably
        # cannot handle unicode in general, anyway. F it
        filt_textline = ''.join(c for c in textline if c in string.printable)
        if filt_textline != textline:
            logger.info("Stripped some non-ascii bytes; if this is breaking your language/environment plese let us know.")
            logger.info("Modified description line: %s", filt_textline)
        if not filt_textline:
            if input_data:
                # done
                break
            else:
                logger.info("No empty descriptions please.  Tell us *something* about the upload")
                continue
        input_data.append(filt_textline)
        total_bytes = sum(map(len, input_data))
        if total_bytes > 2800:
            logger.warn("Sorry, too much text; stopping here before the http PUT fails.")
            time.sleep(2)
            break
    return '\n'.join(input_data)

chunk_complained = False
def slice_and_upload(path, options, slicesize):
    """If we want to upload a file that is oversize, upload_to_splunkcom calls
    this, which calls in in turn N times, once per slice.

    Tries to create a very rudimentary user interface around communicating with
    admin that oversize files may be harmful to support process, and give a
    chance to bail.
    """
    #Will likely need rejiggering when we expose to UI.
    if not options.firstchunk:
        logger.info("---")
        logger.info("Upload file size exceeds the maximum per attachment.")
        logger.info("Oversize uploads can create extra work and slow case resolution.")
        logger.info(" *** To cancel this oversize upload, press CTRL-C ***")
        if options.upload:
            logger.info("You can re-run diag with some components disabled or some useless files in e.g. var/log/splunk removed.")
        elif options.upload_file:
            logger.info("If you're uploading a file besides a diag, consider using stronger compression on the file.")
        sleepsec =  5
        logger.info("Sleeping for %s seconds then continuing to upload as multiple parts.", sleepsec)
        for i in range(sleepsec, 0, -1):
            time.sleep(1)
            sys.stdout.write("%s... " % i)
        sys.stdout.write("\n")

    file_size = os.stat(path).st_size
    logger.info("Upload size '%s' larger than the limit '%s'", file_size,
                slicesize)
    logger.info("Splitting into chunks to upload.")
    offset_starts = list(range(0, file_size, slicesize))
    chunk_num = 1
    chunk_total = len(offset_starts)

    if options.firstchunk:
        chunk_num = options.firstchunk
        offset_starts = offset_starts[options.firstchunk-1:]

    def chunk_complain(chunk_num):
        # Helper to issue info & suggestion to continue
        if chunk_num >1:
            logger.error("Failed while uploading chunk %s.", chunk_num)
            logger.info("You may wish to retry uploading from this chunk with --upload-file <filename> --firstchunk %s", chunk_num)
            global chunk_complained
            chunk_complained = True

    logger.debug("Offset starts: %s", offset_starts)

    for file_start in offset_starts:
        logger.info("Uploading chunk '%s' of '%s'.", chunk_num, chunk_total)
        file_end = file_start + slicesize
        if file_end > file_size:
            file_end = file_size
        try:
            if not upload_to_splunkcom(path, options, file_start, file_end,
                                       chunk_num, chunk_total):
                chunk_complain(chunk_num)
                return False
        except:
            chunk_complain(chunk_num)
            raise
        chunk_num += 1

    logger.info("Chunk-uploading completed.")
    return True

def upload_slice_to_splunkcom(path, options, slicebegin=None, sliceend=None,
                              slicenum=None, slicetotal=None):
    """Upload a quantity of bytes from a file to splunk.com APIs.

    The quantity of bytes MAY be the entire file, in which case the
    various slice* args are None.

    In the case of uploading multiple slices, they are done sequentially (one
    per call to this function), mainly so that an easily-understood user-story
    can be presented externally, and we can describe how to retry partway
    through the data in case of transient network failure etc.
    """

    description = options.upload_description
    f = open(path, "rb")
    filename = os.path.basename(path)

    read_max = None
    if slicenum:
        # tweak description the file name
        prefix = "Chunk %s of %s: " % (slicenum, slicetotal)
        description = prefix + description
        filename = "%s.%s" % (filename, slicenum)

        # Also wrap the file in a proxy that presents a subset of bytes
        f = FileSlice(f, slicebegin, sliceend)
        read_max = sliceend - slicebegin

    fileproxy = FileWrappingProgressBar(f, read_max=read_max)

    url = options.upload_uri + '/1.0/rest/supportcase/casefile/'
    params = {'uname': options.upload_user,
              'case_number': options.case_number,
              'case_id': options.case_id,
              'filename': filename,
              'description': description,
    }
    try:
        response = make_splunkcom_request(url, options, params=params, method='PUT', body=fileproxy)
    except (urllib_error.URLError, ssl.SSLError) as e:
        if isinstance(e, urllib_error.URLError):
            if isinstance(e.reason, Exception):
                err_str = e.reason.strerror
            else:
                err_str = e.reason
            msg1 = "Encountered a failure while transmitting data to Splunk.com : %s" % err_str
        else:
            err_str = e.args[0]
            msg1 = "Encountered an SSL-layer error while transmitting data to Splunk.com : %s" % err_str
        msg2 = "Probably this represents a network problem."
        logger.error(msg1)
        logger.error(msg2)
        return False

    finally:
        fileproxy.close()
    sys.stdout.write("\n") # move past progressbar

    try:
        json_data = json.loads(response)
    except ValueError as e:
        text = response
        if not text:
            text = "an empty document"
        logger.error("Response from splunk.com was not a json document, was: %s", text)
        return False

    if not 'status' in json_data:
        logger.error("Response json is strangely malformed, no status provided: %s", response)
        logger.error("Upload success or failure is unknown.")
        return False

    if json_data['status'] != 1:
        logger.error("Server returned failure for upload: %s", response)
        return False

    if 'message' in json_data and 'msg' in json_data['message']:
        logger.info(json_data['message']['msg'])

    return True

def make_splunkcom_request(url, options, params={}, method='GET', body=None,
                           ok_statuses=[]):
    """Wrapper to make http GET, PUT and POSTS to api.splunk.com

    Unfortunately, has to combine error responses with success, since both are
    returned during normal operation of some APIs. The caller has to make
    decisions about whether the response is normal based on the url and status
    code.

    ok_statuses are http statuses that the caller does not wish this interface
    to complain about; necessary because validation api incorrectly returns
    status 400 values
    """
    encoded_params = urllib_parse.urlencode(params)

    query = url
    if method in ('GET', 'PUT'):
        if params:
            query= url + "?" + encoded_params
        if method == 'GET':
            req = urllib_request.Request(query)
        else: # PUT
            req = urllib_request.Request(query, data=body, method='PUT')
            if body:
                req.add_header("Content-length", '%d' % len(body))
    elif method == 'POST':
        req = urllib_request.Request(query, data=encoded_params.encode())
    else:
        raise NotImplementedError("No support for method %s" % method)

    auth_string = ('%s:%s' % (options.upload_user, options.upload_password)).encode('utf-8')
    auth_encoded = str(base64.encodestring(auth_string).strip().decode())

    req.add_header("Authorization", "Basic %s" % auth_encoded)
    try:
        rest_f = urllib_request.urlopen(req, timeout=15)
    except urllib_error.HTTPError as e:
        # pass back 401, caller should deal with "couldn't log in"
        # Maybe other status codes, but since API is insane, I don't know which
        # yet.
        if e.code in (401,):
            raise
        if not e.code in ok_statuses:
            logger.warn("received error: %s, proceeding with reading contained response", e)
        rest_f = e
    except (urllib_error.URLError, ssl.SSLError) as e:
        raise e
    except Exception as e:
        logger.exception("Unexpected exception while making http request")
        raise
    response = rest_f.read()
    return response # XXX return result code?

def splunkcom_upload_permitted(path, options, size=None):
    """Asks the splunk.com upload API whether this upload will be permitted.
    This exists to ensure we don't push gigabytes of data at splunk.com only to
    find out afterwards that it didn't work.

    path: the file to upload
    options: the optparse options object
    size: how much of the file we intend to upload.

    This function is called with None initially, where we propose to splunk.com
    to upload the whole file at once. If splunk.com says that's too big, this
    will be called again with what we think is the maximum allowed size, to
    ensure that's going to be permitted.
    """
    url = options.upload_uri + "/1.0/rest/supportcase/casefileuploadvalidation/?uname=%s"
    url = url % options.upload_user

    filename = os.path.basename(path)
    if not size:
        size = os.stat(path).st_size
    params = {'file_name': filename,
              'file_size': size,
              'case_id': options.case_id,
    }
    logger.debug("Requesting upload validation with params: %s", params)
    try:
        response = make_splunkcom_request(url, options, params, method='POST',
                                          ok_statuses=[400])
    except urllib_error.HTTPError as e:
        if e.code == 400:
            response = e.read()
    except (urllib_error.URLError, ssl.SSLError) as e:
        if isinstance(e, urllib_error.URLError):
            if isinstance(e.reason, Exception):
                err_str = e.reason.strerror
            else:
                err_str = e.reason
            return_str = "urllib error while attempting validation request: %s" % err_str
        else:
            err_str = e.args[0]
            return_str = "ssl error while attempting validation request: %s" % err_str
        return False, "exception", return_str

    logger.debug("response was: %s" % response)
    try:
        jsondata = json.loads(response)
    except ValueError as e:
        return False, "malformed_reply", "response was not a json document: '%s'" % response

    if not 'status' in jsondata:
        return False, "malformed_reply", "status element was missing from json response, probably server bug"

    if jsondata['status'] not in (1, 2, 3):
        return False, "malformed_reply", "status element was incorrectly not 1, 2 or 3 in json response, probably server bug"

    if jsondata['status'] == 3:
        return False, "invalid_request", "Server rejected the request for who-knows-why"

    if jsondata['status'] == 1:
        return True, None, None # this is the success case

    if jsondata['status'] == 2 and not 'message' in jsondata:
        return False, "malformed_reply", "message element missing from json response, probably server bug"

    msgobj = jsondata['message'][0] # first message 'entry' only for now
    # some responses have multiple entries, but there's no clarity on what to do
    # about it.

    # let's try to guess if this is the expected "file too big" case.
    if not 'short' in msgobj:
        return False, "malformed_reply", "'short' component missing from 'message' in json response; plausibly unintended changes to the content management system on the server"

    errinfo = msgobj['short']
    squashed_errinfo = errinfo.lower()

    if 'case id' in squashed_errinfo:
        return False, "permission_denied", "No access granted for the Case Id attempted: '%s'" % case_id

    if not ('file' in squashed_errinfo or
            'size' in squashed_errinfo or
            'bytes' in squashed_errinfo):
        return False, "unexpected_rejection", "'short' component missing from 'message' in json response; plausibly unintended changes to the content management system on the server"

    # okay, at this point it's probably the file too big case, let's fish out
    # the numbers

    m = re.match("^\D+(\d+)\D+$", errinfo)
    if not m:
        return False, "unexpected_rejection", "'short' mentions files, bytes, or sizes but offers no single number to suggest a limit.  File a bug on splunk.com implementation."
    else:
        nums = list(map(int, m.groups()))

    # the lower number has got to be the limit
    limit = min(nums)
    return False, "file_too_large", limit


def upload_to_splunkcom(path, options, slicebegin=None, sliceend=None,
                        slicenum=None, slicetotal=None):
    logger.debug("upload_to_splunkcom(path=%s, options, slicebegin=%s, sliceend=%s, slicenum=%s", path, slicebegin, sliceend, slicenum)

    # TODO: consider if we can skip validation of slices.
    if slicebegin is None:
        permitted, reason, more = splunkcom_upload_permitted(path, options)
    else:
        size_override = sliceend - slicebegin
        permitted, reason, more = splunkcom_upload_permitted(path, options,
                                                             size_override)

    if not permitted and reason == "file_too_large":
        sizelimit = more-1
        permitted, reason, more = splunkcom_upload_permitted(path, options, size=sizelimit)
        logger.debug("permission check gave permitted:%s resason:%s more:%s", permitted, reason, more)
        if not permitted and reason == "file_too_large":
            sys.exit("Splunk.com refused upload for being oversized when the requested size was smaller than the limit told to us by Splunk.com.  File bug on splunk.com")
        return slice_and_upload(path, options, slicesize=sizelimit)

    if not permitted:
        logger.error("Upload unexepectedly not permitted, category='%s'.", reason)
        logger.error("Guess: %s", more)
        return False

    if slicebegin is None:
        msg = "Uploading %s (%s) to case %s"
        size = os.stat(path).st_size
        logger.info(msg, os.path.basename(path), make_human_sizestr(size),
                     options.case_number)

    return upload_slice_to_splunkcom(path, options, slicebegin, sliceend,
                                     slicenum, slicetotal)

def ensure_upload_login(options):
    if not options.upload_user:
        tmp = input("  upload username: ")
        options.upload_user = tmp.rstrip("\r")
        if not options.upload_user:
            logger.error("No username acquired.")
            return False

    if not hasattr(options, 'upload_password'):
        options.upload_password = getpass.getpass("splunk.com upload password: ")
        if not options.upload_password:
            logger.error("No password acquired.")
            return False
    return True

def ensure_upload_options(options):
    if not options.upload_uri:
        logger.error("upload_uri unset in server.conf, cannot upload.  Aborting.")
        return False

    if not ensure_upload_login(options):
        return False

    def complaint_wrapper(f, *args):
        """Functionlet to wrap a mathod, catching urllib exceptions,  doing the
        logging dance for them.

        f: function
        *args: args for the function

        On exception, returns None for handled exceptions, otherwise returns
        whatever f returns
        """
        try:
            return f(*args)
        except urllib_error.HTTPError as e:
            if e.code == 401:
                logger.error("Unable to authenticate to splunk.com, please check username & password, and that this account has support-case access.")
            else:
                raise
        except (urllib_error.URLError, ssl.SSLError) as e:
            if isinstance(e, urllib_error.URLError):
                if isinstance(e.reason, Exception):
                    err_str = e.reason.strerror
                else:
                    err_str = e.reason
            else:
                err_str = e.args[0]
            logger.error("Unable to fetch case list: %s", err_str)
        return None

    # check the case id, or acquire one
    if not options.case_number:
        # no case selected; so user should choose
        sys.stdout.write("Fetching case list for %s.\n" % options.upload_user)
        out = complaint_wrapper(choose_case, options)
        if not out:
            return False
        case_number, case_id = out
        if not case_number:
            return False
        options.case_number = case_number
        options.case_id = case_id
    else:
        # User picked, a case, so verify they have that case

        # Fetching the case list also verifies their credentials
        cases = complaint_wrapper(fetch_case_list, options)
        if cases == None:
            return False

        case_identified = False
        for case in cases:
            if case.case_number == options.case_number:
                case_identified = True
                options.case_id = case.case_id
                break
        if not case_identified:
            msg = "Could not locate the chosen case number '%s' in the list of available cases."
            logger.error(msg, options.case_number)
            return False

    # if we got this far, we've got a valid case to upload to; now get a
    # description of the upload
    if not options.upload_description:
        options.upload_description = get_upload_description(options)
    return True

def upload_file(path, target, options):
    "Upload a single file to a target"

    if not ensure_upload_options(options):
        return False

    valid_targets = ['splunkcom']
    if not target in valid_targets:
        raise NotImplemented

    if not upload_to_splunkcom(path, options):
        logger.warn("Upload was not successful.")
        return False
    return True

def choose_case(options):
    """Fetch the open cases for the current user, and ask them to select one."""
    assert(not options.case_number)
    cases = fetch_case_list(options)
    if not cases:
        logger.warn("You seem to have no open support cases, open a case first using the support portal.")
        return None, None

    # Don't show the user the enhancements when selecting interactively
    cases = [case for case in cases if case.priority.lower() != 'p4']
    if not cases:
        logger.warn("You seem to have no open support cases (other than enhancement requests), open a case first using the support portal.")
        return None, None

    # sort cases by case Number
    cases.sort(key=lambda c: c.case_number)

    choice = interactive_choice(cases)
    if not choice:
        return None, None
    return choice.case_number, choice.case_id


def fetch_case_list(options):
    if not ensure_upload_login(options):
        logger.error("Cannot set up API-access login to fetch case list")
        return []

    # XXX more API bugs: the case list endpoint idiotically returns status 400
    # when the user doesn't have open cases.
    ok_statuses = [400]

    url = options.upload_uri + "/1.0/rest/supportcase/allcases/"
    response = make_splunkcom_request(url, options, ok_statuses=ok_statuses)
    logger.debug("allcases endpoint returned document: %s", response)
    try:
        json_data = json.loads(response)
    except ValueError as e:
        msg = "Case listing api did not return a json document, returned: '%s'"
        logger.error(msg, response)
        return []
    case_data = json_data['message']
    # XXX webdev brain damage
    if isinstance(case_data, str):
        logger.debug("Splunk.com developers incorrectly returned a string for a list a of cases.")
        logger.debug("This is their brain-damaged way to represent an empty list.")
        logger.debug("Here's the crap they provided: %s", response)
        return []
    json_choices = [DisplayableCaseData(d) for d in case_data]
    return json_choices


#######
# direct-run startup, normally splunk diag doesn't use this but
# splunk cmd python info_gather.py goes through here.

if __name__ == "__main__":
    main()
