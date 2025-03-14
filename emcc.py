#!/usr/bin/env python3
# Copyright 2011 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

"""emcc - compiler helper script
=============================

emcc is a drop-in replacement for a compiler like gcc or clang.

See  emcc --help  for details.

emcc can be influenced by a few environment variables:

  EMCC_DEBUG - "1" will log out useful information during compilation, as well as
               save each compiler step as an emcc-* file in the temp dir
               (by default /tmp/emscripten_temp). "2" will save additional emcc-*
               steps, that would normally not be separately produced (so this
               slows down compilation).
"""

from tools.toolchain_profiler import ToolchainProfiler

import base64
import glob
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import sys
import time
import tarfile
from enum import Enum, unique, auto
from subprocess import PIPE
from urllib.parse import quote


import emscripten
from tools import shared, system_libs, utils, ports, filelock
from tools import colored_logger, diagnostics, building
from tools.shared import unsuffixed, unsuffixed_basename, WINDOWS, safe_copy
from tools.shared import run_process, read_and_preprocess, exit_with_error, DEBUG
from tools.shared import do_replace
from tools.response_file import substitute_response_files
from tools.minimal_runtime_shell import generate_minimal_runtime_html
import tools.line_endings
from tools import feature_matrix
from tools import js_manipulation
from tools import webassembly
from tools import config
from tools import cache
from tools.settings import user_settings, settings, MEM_SIZE_SETTINGS, COMPILE_TIME_SETTINGS
from tools.utils import read_file, write_file, read_binary, delete_file, removeprefix

logger = logging.getLogger('emcc')

# endings = dot + a suffix, compare against result of shared.suffix()
C_ENDINGS = ['.c', '.i']
CXX_ENDINGS = ['.cppm', '.pcm', '.cpp', '.cxx', '.cc', '.c++', '.CPP', '.CXX', '.C', '.CC', '.C++', '.ii']
OBJC_ENDINGS = ['.m', '.mi']
PREPROCESSED_ENDINGS = ['.i', '.ii']
OBJCXX_ENDINGS = ['.mm', '.mii']
SPECIAL_ENDINGLESS_FILENAMES = [os.devnull]
C_ENDINGS += SPECIAL_ENDINGLESS_FILENAMES # consider the special endingless filenames like /dev/null to be C

SOURCE_ENDINGS = C_ENDINGS + CXX_ENDINGS + OBJC_ENDINGS + OBJCXX_ENDINGS + ['.ll', '.S']

EXECUTABLE_ENDINGS = ['.wasm', '.html', '.js', '.mjs', '.out', '']
DYNAMICLIB_ENDINGS = ['.dylib', '.so'] # Windows .dll suffix is not included in this list, since those are never linked to directly on the command line.
STATICLIB_ENDINGS = ['.a']
ASSEMBLY_ENDINGS = ['.s']
HEADER_ENDINGS = ['.h', '.hxx', '.hpp', '.hh', '.H', '.HXX', '.HPP', '.HH']

# Supported LLD flags which we will pass through to the linker.
SUPPORTED_LINKER_FLAGS = (
    '--start-group', '--end-group',
    '-(', '-)',
    '--whole-archive', '--no-whole-archive',
    '-whole-archive', '-no-whole-archive'
)

# Unsupported LLD flags which we will ignore.
# Maps to true if the flag takes an argument.
UNSUPPORTED_LLD_FLAGS = {
    # macOS-specific linker flag that libtool (ltmain.sh) will if macOS is detected.
    '-bind_at_load': False,
    # wasm-ld doesn't support soname or other dynamic linking flags (yet).   Ignore them
    # in order to aid build systems that want to pass these flags.
    '-soname': True,
    '-allow-shlib-undefined': False,
    '-rpath': True,
    '-rpath-link': True,
    '-version-script': True,
    '-install_name': True,
}

DEFAULT_ASYNCIFY_IMPORTS = [
  'wasi_snapshot_preview1.fd_sync', '__wasi_fd_sync', '__asyncjs__*'
]

DEFAULT_ASYNCIFY_EXPORTS = [
  'main',
  '__main_argc_argv',
  # Embind's async template wrapper functions. These functions are usually in
  # the function pointer table and not called from exports, but we need to name
  # them so the JSPI pass can find and convert them.
  '_ZN10emscripten8internal5async*'
]

# Target options
final_js = None

UBSAN_SANITIZERS = {
  'alignment',
  'bool',
  'builtin',
  'bounds',
  'enum',
  'float-cast-overflow',
  'float-divide-by-zero',
  'function',
  'implicit-unsigned-integer-truncation',
  'implicit-signed-integer-truncation',
  'implicit-integer-sign-change',
  'integer-divide-by-zero',
  'nonnull-attribute',
  'null',
  'nullability-arg',
  'nullability-assign',
  'nullability-return',
  'object-size',
  'pointer-overflow',
  'return',
  'returns-nonnull-attribute',
  'shift',
  'signed-integer-overflow',
  'unreachable',
  'unsigned-integer-overflow',
  'vla-bound',
  'vptr',
  'undefined',
  'undefined-trap',
  'implicit-integer-truncation',
  'implicit-integer-arithmetic-value-change',
  'implicit-conversion',
  'integer',
  'nullability',
}

# These symbol names are allowed in INCOMING_MODULE_JS_API but are not part of the
# default set.
EXTRA_INCOMING_JS_API = [
  'fetchSettings'
]

VALID_ENVIRONMENTS = ('web', 'webview', 'worker', 'node', 'shell')
SIMD_INTEL_FEATURE_TOWER = ['-msse', '-msse2', '-msse3', '-mssse3', '-msse4.1', '-msse4.2', '-msse4', '-mavx']
SIMD_NEON_FLAGS = ['-mfpu=neon']
COMPILE_ONLY_FLAGS = {'--default-obj-ext'}
LINK_ONLY_FLAGS = {
    '--bind', '--closure', '--cpuprofiler', '--embed-file',
    '--emit-symbol-map', '--emrun', '--exclude-file', '--extern-post-js',
    '--extern-pre-js', '--ignore-dynamic-linking', '--js-library',
    '--js-transform', '--memory-init-file', '--oformat', '--output_eol',
    '--post-js', '--pre-js', '--preload-file', '--profiling-funcs',
    '--proxy-to-worker', '--shell-file', '--source-map-base',
    '--threadprofiler', '--use-preload-plugins'
}


# this function uses the global 'final' variable, which contains the current
# final output file. if a method alters final, and calls this method, then it
# must modify final globally (i.e. it can't receive final as a param and
# return it)
# TODO: refactor all this, a singleton that abstracts over the final output
#       and saving of intermediates
def save_intermediate(name, suffix='js'):
  if not DEBUG:
    return
  if not final_js:
    logger.debug(f'(not saving intermediate {name} because not generating JS)')
    return
  building.save_intermediate(final_js, f'{name}.{suffix}')


def save_intermediate_with_wasm(name, wasm_binary):
  if not DEBUG:
    return
  save_intermediate(name) # save the js
  building.save_intermediate(wasm_binary, name + '.wasm')


def base64_encode(b):
  b64 = base64.b64encode(b)
  return b64.decode('ascii')


def align_to_wasm_page_boundary(address):
  page_size = webassembly.WASM_PAGE_SIZE
  return ((address + (page_size - 1)) // page_size) * page_size


@unique
class OFormat(Enum):
  # Output a relocatable object file.  We use this
  # today for `-r` and `-shared`.
  OBJECT = auto()
  WASM = auto()
  JS = auto()
  MJS = auto()
  HTML = auto()
  BARE = auto()


@unique
class Mode(Enum):
  PREPROCESS_ONLY = auto()
  PCH = auto()
  COMPILE_ONLY = auto()
  POST_LINK_ONLY = auto()
  COMPILE_AND_LINK = auto()


class EmccState:
  def __init__(self, args):
    self.mode = Mode.COMPILE_AND_LINK
    # Using tuple here to prevent accidental mutation
    self.orig_args = tuple(args)
    self.has_dash_c = False
    self.has_dash_E = False
    self.has_dash_S = False
    self.link_flags = []
    self.lib_dirs = []
    self.forced_stdlibs = []


def add_link_flag(state, i, f):
  if f.startswith('-L'):
    state.lib_dirs.append(f[2:])

  state.link_flags.append((i, f))


class EmccOptions:
  def __init__(self):
    self.output_file = None
    self.post_link = False
    self.executable = False
    self.compiler_wrapper = None
    self.oformat = None
    self.requested_debug = ''
    self.emit_symbol_map = False
    self.use_closure_compiler = None
    self.closure_args = []
    self.js_transform = None
    self.pre_js = [] # before all js
    self.post_js = [] # after all js
    self.extern_pre_js = [] # before all js, external to optimized code
    self.extern_post_js = [] # after all js, external to optimized code
    self.preload_files = []
    self.embed_files = []
    self.exclude_files = []
    self.ignore_dynamic_linking = False
    self.shell_path = utils.path_from_root('src/shell.html')
    self.source_map_base = ''
    self.embind_emit_tsd = ''
    self.emrun = False
    self.cpu_profiler = False
    self.memory_profiler = False
    self.memory_init_file = None
    self.use_preload_cache = False
    self.use_preload_plugins = False
    self.default_object_extension = '.o'
    self.valid_abspaths = []
    # Specifies the line ending format to use for all generated text files.
    # Defaults to using the native EOL on each platform (\r\n on Windows, \n on
    # Linux & MacOS)
    self.output_eol = os.linesep
    self.no_entry = False
    self.shared = False
    self.relocatable = False
    self.reproduce = None


def will_metadce():
  # The metadce JS parsing code does not currently support the JS that gets generated
  # when assertions are enabled.
  if settings.ASSERTIONS:
    return False
  return settings.OPT_LEVEL >= 3 or settings.SHRINK_LEVEL >= 1


def create_reproduce_file(name, args):
  def make_relative(filename):
    filename = os.path.normpath(os.path.abspath(filename))
    filename = os.path.splitdrive(filename)[1]
    filename = filename[1:]
    return filename

  root = unsuffixed_basename(name)
  with tarfile.open(name, 'w') as reproduce_file:
    reproduce_file.add(shared.path_from_root('emscripten-version.txt'), os.path.join(root, 'version.txt'))

    with shared.get_temp_files().get_file(suffix='.tar') as rsp_name:
      with open(rsp_name, 'w') as rsp:
        ignore_next = False
        output_arg = None

        for arg in args:
          ignore = ignore_next
          ignore_next = False
          if arg.startswith('--reproduce='):
            continue

          if arg.startswith('-o='):
            rsp.write('-o\n')
            arg = arg[3:]
            output_arg = True
            ignore = True

          if output_arg:
            # If -o path contains directories, "emcc @response.txt" will likely
            # fail because the archive we are creating doesn't contain empty
            # directories for the output path (-o doesn't create directories).
            # Strip directories to prevent the issue.
            arg = os.path.basename(arg)
            output_arg = False

          if not arg.startswith('-') and not ignore:
            relpath = make_relative(arg)
            rsp.write(relpath + '\n')
            reproduce_file.add(arg, os.path.join(root, relpath))
          else:
            rsp.write(arg + '\n')

          if ignore:
            continue

          if arg in ('-MT', '-MF', '-MJ', '-MQ', '-D', '-U', '-o', '-x',
                     '-Xpreprocessor', '-include', '-imacros', '-idirafter',
                     '-iprefix', '-iwithprefix', '-iwithprefixbefore',
                     '-isysroot', '-imultilib', '-A', '-isystem', '-iquote',
                     '-install_name', '-compatibility_version',
                     '-current_version', '-I', '-L', '-include-pch',
                     '-Xlinker', '-Xclang'):
            ignore_next = True

          if arg == '-o':
            output_arg = True

      reproduce_file.add(rsp_name, os.path.join(root, 'response.txt'))


def setup_environment_settings():
  # Environment setting based on user input
  environments = settings.ENVIRONMENT.split(',')
  if any([x for x in environments if x not in VALID_ENVIRONMENTS]):
    exit_with_error(f'Invalid environment specified in "ENVIRONMENT": {settings.ENVIRONMENT}. Should be one of: {",".join(VALID_ENVIRONMENTS)}')

  settings.ENVIRONMENT_MAY_BE_WEB = not settings.ENVIRONMENT or 'web' in environments
  settings.ENVIRONMENT_MAY_BE_WEBVIEW = not settings.ENVIRONMENT or 'webview' in environments
  settings.ENVIRONMENT_MAY_BE_NODE = not settings.ENVIRONMENT or 'node' in environments
  settings.ENVIRONMENT_MAY_BE_SHELL = not settings.ENVIRONMENT or 'shell' in environments

  # The worker case also includes Node.js workers when pthreads are
  # enabled and Node.js is one of the supported environments for the build to
  # run on. Node.js workers are detected as a combination of
  # ENVIRONMENT_IS_WORKER and ENVIRONMENT_IS_NODE.
  settings.ENVIRONMENT_MAY_BE_WORKER = \
      not settings.ENVIRONMENT or \
      'worker' in environments or \
      (settings.ENVIRONMENT_MAY_BE_NODE and settings.PTHREADS)

  if not settings.ENVIRONMENT_MAY_BE_WORKER and settings.PROXY_TO_WORKER:
    exit_with_error('If you specify --proxy-to-worker and specify a "-sENVIRONMENT=" directive, it must include "worker" as a target! (Try e.g. -sENVIRONMENT=web,worker)')

  if not settings.ENVIRONMENT_MAY_BE_WORKER and settings.SHARED_MEMORY:
    exit_with_error('When building with multithreading enabled and a "-sENVIRONMENT=" directive is specified, it must include "worker" as a target! (Try e.g. -sENVIRONMENT=web,worker)')


def minify_whitespace():
  return settings.OPT_LEVEL >= 2 and settings.DEBUG_LEVEL == 0


def embed_memfile(options):
  return (settings.SINGLE_FILE or
          (settings.WASM2JS and not options.memory_init_file and
           (not settings.MAIN_MODULE and
            not settings.SIDE_MODULE and
            not settings.GENERATE_SOURCE_MAP)))


def expand_byte_size_suffixes(value):
  """Given a string with KB/MB size suffixes, such as "32MB", computes how
  many bytes that is and returns it as an integer.
  """
  value = value.strip()
  match = re.match(r'^(\d+)\s*([kmgt]?b)?$', value, re.I)
  if not match:
    exit_with_error("invalid byte size `%s`.  Valid suffixes are: kb, mb, gb, tb" % value)
  value, suffix = match.groups()
  value = int(value)
  if suffix:
    size_suffixes = {suffix: 1024 ** i for i, suffix in enumerate(['b', 'kb', 'mb', 'gb', 'tb'])}
    value *= size_suffixes[suffix.lower()]
  return value


def default_setting(name, new_default):
  if name not in user_settings:
    setattr(settings, name, new_default)


def apply_user_settings():
  """Take a map of users settings {NAME: VALUE} and apply them to the global
  settings object.
  """

  # Stash a copy of all available incoming APIs before the user can potentially override it
  settings.ALL_INCOMING_MODULE_JS_API = settings.INCOMING_MODULE_JS_API + EXTRA_INCOMING_JS_API

  for key, value in user_settings.items():
    if key in settings.internal_settings:
      exit_with_error('%s is an internal setting and cannot be set from command line', key)

    # map legacy settings which have aliases to the new names
    # but keep the original key so errors are correctly reported via the `setattr` below
    user_key = key
    if key in settings.legacy_settings and key in settings.alt_names:
      key = settings.alt_names[key]

    # In those settings fields that represent amount of memory, translate suffixes to multiples of 1024.
    if key in MEM_SIZE_SETTINGS:
      value = str(expand_byte_size_suffixes(value))

    filename = None
    if value and value[0] == '@':
      filename = removeprefix(value, '@')
      if not os.path.exists(filename):
        exit_with_error('%s: file not found parsing argument: %s=%s' % (filename, key, value))
      value = read_file(filename).strip()
    else:
      value = value.replace('\\', '\\\\')

    expected_type = settings.types.get(key)

    if filename and expected_type == list and value.strip()[0] != '[':
      # Prefer simpler one-line-per value parser
      value = parse_symbol_list_file(value)
    else:
      try:
        value = parse_value(value, expected_type)
      except Exception as e:
        exit_with_error('a problem occurred in evaluating the content after a "-s", specifically "%s=%s": %s', key, value, str(e))

    setattr(settings, user_key, value)

    if key == 'EXPORTED_FUNCTIONS':
      # used for warnings in emscripten.py
      settings.USER_EXPORTED_FUNCTIONS = settings.EXPORTED_FUNCTIONS.copy()

    # TODO(sbc): Remove this legacy way.
    if key == 'WASM_OBJECT_FILES':
      settings.LTO = 0 if value else 'full'


def is_ar_file_with_missing_index(archive_file):
  # We parse the archive header outselves because llvm-nm --print-armap is slower and less
  # reliable.
  # See: https://github.com/emscripten-core/emscripten/issues/10195
  archive_header = b'!<arch>\n'
  file_header_size = 60

  with open(archive_file, 'rb') as f:
    header = f.read(len(archive_header))
    if header != archive_header:
      # This is not even an ar file
      return False
    file_header = f.read(file_header_size)
    if len(file_header) != file_header_size:
      # We don't have any file entires at all so we don't consider the index missing
      return False

  name = file_header[:16].strip()
  # If '/' is the name of the first file we have an index
  return name != b'/'


def ensure_archive_index(archive_file):
  # Fastcomp linking works without archive indexes.
  if not settings.AUTO_ARCHIVE_INDEXES:
    return
  if is_ar_file_with_missing_index(archive_file):
    diagnostics.warning('emcc', '%s: archive is missing an index; Use emar when creating libraries to ensure an index is created', archive_file)
    diagnostics.warning('emcc', '%s: adding index', archive_file)
    run_process([shared.LLVM_RANLIB, archive_file])


def generate_js_sym_info():
  # Runs the js compiler to generate a list of all symbols available in the JS
  # libraries.  This must be done separately for each linker invokation since the
  # list of symbols depends on what settings are used.
  # TODO(sbc): Find a way to optimize this.  Potentially we could add a super-set
  # mode of the js compiler that would generate a list of all possible symbols
  # that could be checked in.
  _, forwarded_data = emscripten.compile_javascript(symbols_only=True)
  # When running in symbols_only mode compiler.js outputs a flat list of C symbols.
  return json.loads(forwarded_data)


@ToolchainProfiler.profile_block('JS symbol generation')
def get_js_sym_info():
  # Avoiding using the cache when generating struct info since
  # this step is performed while the cache is locked.
  if DEBUG or settings.BOOTSTRAPPING_STRUCT_INFO or config.FROZEN_CACHE:
    return generate_js_sym_info()

  # We define a cache hit as when the settings and `--js-library` contents are
  # identical.
  # Ignore certain settings that can are no relevant to library deps.  Here we
  # skip PRE_JS_FILES/POST_JS_FILES which don't effect the library symbol list
  # and can contain full paths to temporary files.
  skip_settings = {'PRE_JS_FILES', 'POST_JS_FILES'}
  input_files = [json.dumps(settings.external_dict(skip_keys=skip_settings), sort_keys=True, indent=2)]
  for jslib in sorted(glob.glob(utils.path_from_root('src') + '/library*.js')):
    input_files.append(read_file(jslib))
  for jslib in settings.JS_LIBRARIES:
    if not os.path.isabs(jslib):
      jslib = utils.path_from_root('src', jslib)
    input_files.append(read_file(jslib))
  content = '\n'.join(input_files)
  content_hash = hashlib.sha1(content.encode('utf-8')).hexdigest()

  def build_symbol_list(filename):
    """Only called when there is no existing symbol list for a given content hash.
    """
    library_syms = generate_js_sym_info()

    write_file(filename, json.dumps(library_syms, separators=(',', ':'), indent=2))

  # We need to use a separate lock here for symbol lists because, unlike with system libraries,
  # it's normally for these file to get pruned as part of normal operation.  This means that it
  # can be deleted between the `cache.get()` then the `read_file`.
  with filelock.FileLock(cache.get_path(cache.get_path('symbol_lists.lock'))):
    filename = cache.get(f'symbol_lists/{content_hash}.json', build_symbol_list)
    library_syms = json.loads(read_file(filename))

    # Limit of the overall size of the cache to 100 files.
    # This code will get test coverage since a full test run of `other` or `core`
    # generates ~1000 unique symbol lists.
    cache_limit = 500
    root = cache.get_path('symbol_lists')
    if len(os.listdir(root)) > cache_limit:
      files = []
      for f in os.listdir(root):
        f = os.path.join(root, f)
        files.append((f, os.path.getmtime(f)))
      files.sort(key=lambda x: x[1])
      # Delete all but the newest N files
      for f, _ in files[:-cache_limit]:
        delete_file(f)

  return library_syms


def filter_link_flags(flags, using_lld):
  def is_supported(f):
    if using_lld:
      for flag, takes_arg in UNSUPPORTED_LLD_FLAGS.items():
        # lld allows various flags to have either a single -foo or double --foo
        if f.startswith(flag) or f.startswith('-' + flag):
          diagnostics.warning('linkflags', 'ignoring unsupported linker flag: `%s`', f)
          # Skip the next argument if this linker flag takes and argument and that
          # argument was not specified as a separately (i.e. it was specified as
          # single arg containing an `=` char.)
          skip_next = takes_arg and '=' not in f
          return False, skip_next
      return True, False
    else:
      if f in SUPPORTED_LINKER_FLAGS:
        return True, False
      # Silently ignore -l/-L flags when not using lld.  If using lld allow
      # them to pass through the linker
      if f.startswith('-l') or f.startswith('-L'):
        return False, False
      diagnostics.warning('linkflags', 'ignoring unsupported linker flag: `%s`', f)
      return False, False

  results = []
  skip_next = False
  for f in flags:
    if skip_next:
      skip_next = False
      continue
    keep, skip_next = is_supported(f[1])
    if keep:
      results.append(f)

  return results


def fix_windows_newlines(text):
  # Avoid duplicating \r\n to \r\r\n when writing out text.
  if WINDOWS:
    text = text.replace('\r\n', '\n')
  return text


def read_js_files(files):
  contents = '\n'.join(read_file(f) for f in files)
  return fix_windows_newlines(contents)


def cxx_to_c_compiler(cxx):
  # Convert C++ compiler name into C compiler name
  dirname, basename = os.path.split(cxx)
  basename = basename.replace('clang++', 'clang').replace('g++', 'gcc').replace('em++', 'emcc')
  return os.path.join(dirname, basename)


def should_run_binaryen_optimizer():
  # run the binaryen optimizer in -O2+. in -O0 we don't need it obviously, while
  # in -O1 we don't run it as the LLVM optimizer has been run, and it does the
  # great majority of the work; not running the binaryen optimizer in that case
  # keeps -O1 mostly-optimized while compiling quickly and without rewriting
  # DWARF etc.
  return settings.OPT_LEVEL >= 2


def get_binaryen_passes():
  passes = []
  optimizing = should_run_binaryen_optimizer()
  # safe heap must run before post-emscripten, so post-emscripten can apply the sbrk ptr
  if settings.SAFE_HEAP:
    passes += ['--safe-heap']
  if settings.MEMORY64 == 2:
    passes += ['--memory64-lowering']
  # sign-ext is enabled by default by llvm.  If the target browser settings don't support
  # this we lower it away here using a binaryen pass.
  if not feature_matrix.caniuse(feature_matrix.Feature.SIGN_EXT):
    logger.debug('lowering sign-ext feature due to incompatiable target browser engines')
    passes += ['--signext-lowering']
  if optimizing:
    passes += ['--post-emscripten']
    if settings.SIDE_MODULE:
      passes += ['--pass-arg=post-emscripten-side-module']
  if optimizing:
    passes += [building.opt_level_to_str(settings.OPT_LEVEL, settings.SHRINK_LEVEL)]
  # when optimizing, use the fact that low memory is never used (1024 is a
  # hardcoded value in the binaryen pass)
  if optimizing and settings.GLOBAL_BASE >= 1024:
    passes += ['--low-memory-unused']
  if settings.AUTODEBUG:
    # adding '--flatten' here may make these even more effective
    passes += ['--instrument-locals']
    passes += ['--log-execution']
    passes += ['--instrument-memory']
    if settings.LEGALIZE_JS_FFI:
      # legalize it again now, as the instrumentation may need it
      passes += ['--legalize-js-interface']
      passes += building.js_legalization_pass_flags()
  if settings.EMULATE_FUNCTION_POINTER_CASTS:
    # note that this pass must run before asyncify, as if it runs afterwards we only
    # generate the  byn$fpcast_emu  functions after asyncify runs, and so we wouldn't
    # be able to further process them.
    passes += ['--fpcast-emu']
  if settings.ASYNCIFY == 1:
    passes += ['--asyncify']
    if settings.MAIN_MODULE or settings.SIDE_MODULE:
      passes += ['--pass-arg=asyncify-relocatable']
    if settings.ASSERTIONS:
      passes += ['--pass-arg=asyncify-asserts']
    if settings.ASYNCIFY_ADVISE:
      passes += ['--pass-arg=asyncify-verbose']
    if settings.ASYNCIFY_IGNORE_INDIRECT:
      passes += ['--pass-arg=asyncify-ignore-indirect']
    passes += ['--pass-arg=asyncify-imports@%s' % ','.join(settings.ASYNCIFY_IMPORTS)]

    # shell escaping can be confusing; try to emit useful warnings
    def check_human_readable_list(items):
      for item in items:
        if item.count('(') != item.count(')'):
          logger.warning('emcc: ASYNCIFY list contains an item without balanced parentheses ("(", ")"):')
          logger.warning('   ' + item)
          logger.warning('This may indicate improper escaping that led to splitting inside your names.')
          logger.warning('Try using a response file. e.g: -sASYNCIFY_ONLY=@funcs.txt. The format is a simple')
          logger.warning('text file, one line per function.')
          break

    if settings.ASYNCIFY_REMOVE:
      check_human_readable_list(settings.ASYNCIFY_REMOVE)
      passes += ['--pass-arg=asyncify-removelist@%s' % ','.join(settings.ASYNCIFY_REMOVE)]
    if settings.ASYNCIFY_ADD:
      check_human_readable_list(settings.ASYNCIFY_ADD)
      passes += ['--pass-arg=asyncify-addlist@%s' % ','.join(settings.ASYNCIFY_ADD)]
    if settings.ASYNCIFY_ONLY:
      check_human_readable_list(settings.ASYNCIFY_ONLY)
      passes += ['--pass-arg=asyncify-onlylist@%s' % ','.join(settings.ASYNCIFY_ONLY)]
  elif settings.ASYNCIFY == 2:
    passes += ['--jspi']
    passes += ['--pass-arg=jspi-imports@%s' % ','.join(settings.ASYNCIFY_IMPORTS)]
    passes += ['--pass-arg=jspi-exports@%s' % ','.join(settings.ASYNCIFY_EXPORTS)]
    if settings.SPLIT_MODULE:
      passes += ['--pass-arg=jspi-split-module']

  if settings.BINARYEN_IGNORE_IMPLICIT_TRAPS:
    passes += ['--ignore-implicit-traps']
  # normally we can assume the memory, if imported, has not been modified
  # beforehand (in fact, in most cases the memory is not even imported anyhow,
  # but it is still safe to pass the flag), and is therefore filled with zeros.
  # the one exception is dynamic linking of a side module: the main module is ok
  # as it is loaded first, but the side module may be assigned memory that was
  # previously used.
  if optimizing and not settings.SIDE_MODULE:
    passes += ['--zero-filled-memory']
  # LLVM output always has immutable initial table contents: the table is
  # fixed and may only be appended to at runtime (that is true even in
  # relocatable mode)
  if optimizing:
    passes += ['--pass-arg=directize-initial-contents-immutable']

  if settings.BINARYEN_EXTRA_PASSES:
    # BINARYEN_EXTRA_PASSES is comma-separated, and we support both '-'-prefixed and
    # unprefixed pass names
    extras = settings.BINARYEN_EXTRA_PASSES.split(',')
    passes += [('--' + p) if p[0] != '-' else p for p in extras if p]

  return passes


def make_js_executable(script):
  src = read_file(script)
  cmd = config.NODE_JS
  if settings.MEMORY64 == 1:
    cmd += shared.node_memory64_flags()
  elif settings.WASM_BIGINT:
    cmd += shared.node_bigint_flags()
  if len(cmd) > 1 or not os.path.isabs(cmd[0]):
    # Using -S (--split-string) here means that arguments to the executable are
    # correctly parsed.  We don't do this by default because old versions of env
    # don't support -S.
    cmd = '/usr/bin/env -S ' + shared.shlex_join(cmd)
  else:
    cmd = shared.shlex_join(cmd)
  logger.debug('adding `#!` to JavaScript file: %s' % cmd)
  # add shebang
  with open(script, 'w') as f:
    f.write('#!%s\n' % cmd)
    f.write(src)
  try:
    os.chmod(script, stat.S_IMODE(os.stat(script).st_mode) | stat.S_IXUSR) # make executable
  except OSError:
    pass # can fail if e.g. writing the executable to /dev/null


def do_split_module(wasm_file, options):
  os.rename(wasm_file, wasm_file + '.orig')
  args = ['--instrument']
  if options.requested_debug:
    # Tell wasm-split to preserve function names.
    args += ['-g']
  building.run_binaryen_command('wasm-split', wasm_file + '.orig', outfile=wasm_file, args=args)


def is_dash_s_for_emcc(args, i):
  # -s OPT=VALUE or -s OPT or -sOPT are all interpreted as emscripten flags.
  # -s by itself is a linker option (alias for --strip-all)
  if args[i] == '-s':
    if len(args) <= i + 1:
      return False
    arg = args[i + 1]
  else:
    arg = removeprefix(args[i], '-s')
  arg = arg.split('=')[0]
  return arg.isidentifier() and arg.isupper()


def filter_out_dynamic_libs(options, inputs):

  # Filters out "fake" dynamic libraries that are really just intermediate object files.
  def check(input_file):
    if get_file_suffix(input_file) in DYNAMICLIB_ENDINGS and not building.is_wasm_dylib(input_file):
      if not options.ignore_dynamic_linking:
        diagnostics.warning('emcc', 'ignoring dynamic library %s because not compiling to JS or HTML, remember to link it when compiling to JS or HTML at the end', os.path.basename(input_file))
      return False
    else:
      return True

  return [f for f in inputs if check(f)]


def filter_out_duplicate_dynamic_libs(inputs):
  seen = set()

  # Filter out duplicate "fake" shared libraries (intermediate object files).
  # See test_core.py:test_redundant_link
  def check(input_file):
    if get_file_suffix(input_file) in DYNAMICLIB_ENDINGS and not building.is_wasm_dylib(input_file):
      abspath = os.path.abspath(input_file)
      if abspath in seen:
        return False
      seen.add(abspath)
    return True

  return [f for f in inputs if check(f)]


def process_dynamic_libs(dylibs, lib_dirs):
  extras = []
  seen = set()
  to_process = dylibs.copy()
  while to_process:
    dylib = to_process.pop()
    dylink = webassembly.parse_dylink_section(dylib)
    for needed in dylink.needed:
      if needed in seen:
        continue
      path = find_library(needed, lib_dirs)
      if path:
        extras.append(path)
        seen.add(needed)
      else:
        exit_with_error(f'{os.path.normpath(dylib)}: shared library dependency not found: `{needed}`')
      to_process.append(path)

  dylibs += extras
  for dylib in dylibs:
    exports = webassembly.get_exports(dylib)
    exports = set(e.name for e in exports)
    # EM_JS function are exports with a special prefix.  We need to strip
    # this prefix to get the actaul symbol name.  For the main module, this
    # is handled by extract_metadata.py.
    exports = [removeprefix(e, '__em_js__') for e in exports]
    settings.SIDE_MODULE_EXPORTS.extend(sorted(exports))

    imports = webassembly.get_imports(dylib)
    imports = [i.field for i in imports if i.kind in (webassembly.ExternType.FUNC, webassembly.ExternType.GLOBAL, webassembly.ExternType.TAG)]
    # For now we ignore `invoke_` functions imported by side modules and rely
    # on the dynamic linker to create them on the fly.
    # TODO(sbc): Integrate with metadata.invokeFuncs that comes from the
    # main module to avoid creating new invoke functions at runtime.
    imports = set(imports)
    imports = set(i for i in imports if not i.startswith('invoke_'))
    strong_imports = sorted(imports.difference(exports))
    logger.debug('Adding symbols requirements from `%s`: %s', dylib, imports)

    mangled_imports = [shared.asmjs_mangle(e) for e in sorted(imports)]
    mangled_strong_imports = [shared.asmjs_mangle(e) for e in strong_imports]
    settings.SIDE_MODULE_IMPORTS.extend(mangled_imports)
    settings.EXPORT_IF_DEFINED.extend(sorted(imports))
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE.extend(sorted(imports))
    building.user_requested_exports.update(mangled_strong_imports)


def unmangle_symbols_from_cmdline(symbols):
  def unmangle(x):
    return x.replace('.', ' ').replace('#', '&').replace('?', ',')

  if type(symbols) is list:
    return [unmangle(x) for x in symbols]
  return unmangle(symbols)


def parse_s_args(args):
  settings_changes = []
  for i in range(len(args)):
    if args[i].startswith('-s'):
      if is_dash_s_for_emcc(args, i):
        if args[i] == '-s':
          key = args[i + 1]
          args[i + 1] = ''
        else:
          key = removeprefix(args[i], '-s')
        args[i] = ''

        # If not = is specified default to 1
        if '=' not in key:
          key += '=1'

        # Special handling of browser version targets. A version -1 means that the specific version
        # is not supported at all. Replace those with INT32_MAX to make it possible to compare e.g.
        # #if MIN_FIREFOX_VERSION < 68
        if re.match(r'MIN_.*_VERSION(=.*)?', key):
          try:
            if int(key.split('=')[1]) < 0:
              key = key.split('=')[0] + '=0x7FFFFFFF'
          except Exception:
            pass

        settings_changes.append(key)

  newargs = [a for a in args if a]
  return (settings_changes, newargs)


def emsdk_cflags(user_args):
  cflags = ['--sysroot=' + cache.get_sysroot(absolute=True)]

  def array_contains_any_of(hay, needles):
    for n in needles:
      if n in hay:
        return True

  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER) or array_contains_any_of(user_args, SIMD_NEON_FLAGS):
    if '-msimd128' not in user_args and '-mrelaxed-simd' not in user_args:
      exit_with_error('Passing any of ' + ', '.join(SIMD_INTEL_FEATURE_TOWER + SIMD_NEON_FLAGS) + ' flags also requires passing -msimd128 (or -mrelaxed-simd)!')
    cflags += ['-D__SSE__=1']

  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER[1:]):
    cflags += ['-D__SSE2__=1']

  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER[2:]):
    cflags += ['-D__SSE3__=1']

  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER[3:]):
    cflags += ['-D__SSSE3__=1']

  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER[4:]):
    cflags += ['-D__SSE4_1__=1']

  # Handle both -msse4.2 and its alias -msse4.
  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER[5:]):
    cflags += ['-D__SSE4_2__=1']

  if array_contains_any_of(user_args, SIMD_INTEL_FEATURE_TOWER[7:]):
    cflags += ['-D__AVX__=1']

  if array_contains_any_of(user_args, SIMD_NEON_FLAGS):
    cflags += ['-D__ARM_NEON__=1']

  if not settings.USE_SDL:
    cflags += ['-Xclang', '-iwithsysroot' + os.path.join('/include', 'fakesdl')]

  return cflags + ['-Xclang', '-iwithsysroot' + os.path.join('/include', 'compat')]


def get_target_flags():
  return ['-target', shared.get_llvm_target()]


def get_clang_flags(user_args):
  flags = get_target_flags()

  # if exception catching is disabled, we can prevent that code from being
  # generated in the frontend
  if settings.DISABLE_EXCEPTION_CATCHING and not settings.WASM_EXCEPTIONS:
    flags.append('-fignore-exceptions')

  if settings.INLINING_LIMIT:
    flags.append('-fno-inline-functions')

  if settings.RELOCATABLE and '-fPIC' not in user_args:
    flags.append('-fPIC')

  # We use default visiibilty=default in emscripten even though the upstream
  # backend defaults visibility=hidden.  This matched the expectations of C/C++
  # code in the wild which expects undecorated symbols to be exported to other
  # DSO's by default.
  if not any(a.startswith('-fvisibility') for a in user_args):
    flags.append('-fvisibility=default')

  if settings.LTO:
    if not any(a.startswith('-flto') for a in user_args):
      flags.append('-flto=' + settings.LTO)
    # setjmp/longjmp handling using Wasm EH
    # For non-LTO, '-mllvm -wasm-enable-eh' added in
    # building.llvm_backend_args() sets this feature in clang. But in LTO, the
    # argument is added to wasm-ld instead, so clang needs to know that EH is
    # enabled so that it can be added to the attributes in LLVM IR.
    if settings.SUPPORT_LONGJMP == 'wasm':
      flags.append('-mexception-handling')

  else:
    # In LTO mode these args get passed instead at link time when the backend runs.
    for a in building.llvm_backend_args():
      flags += ['-mllvm', a]

  return flags


cflags = None


def get_cflags(user_args, is_cxx):
  global cflags
  if cflags:
    return cflags

  # Flags we pass to the compiler when building C/C++ code
  # We add these to the user's flags (newargs), but not when building .s or .S assembly files
  cflags = get_clang_flags(user_args)

  if settings.EMSCRIPTEN_TRACING:
    cflags.append('-D__EMSCRIPTEN_TRACING__=1')

  if settings.SHARED_MEMORY:
    cflags.append('-D__EMSCRIPTEN_SHARED_MEMORY__=1')

  if settings.WASM_WORKERS:
    cflags.append('-D__EMSCRIPTEN_WASM_WORKERS__=1')

  if not settings.STRICT:
    # The preprocessor define EMSCRIPTEN is deprecated. Don't pass it to code
    # in strict mode. Code should use the define __EMSCRIPTEN__ instead.
    cflags.append('-DEMSCRIPTEN')

  # Changes to default clang behavior

  # Implicit functions can cause horribly confusing function pointer type errors, see #2175
  # If your codebase really needs them - very unrecommended! - you can disable the error with
  #   -Wno-error=implicit-function-declaration
  # or disable even a warning about it with
  #   -Wno-implicit-function-declaration
  # This is already an error in C++ so we don't need to inject extra flags.
  if not is_cxx:
    cflags += ['-Werror=implicit-function-declaration']

  ports.add_cflags(cflags, settings)

  if '-nostdinc' in user_args:
    return cflags

  cflags += emsdk_cflags(user_args)
  return cflags


def get_file_suffix(filename):
  """Parses the essential suffix of a filename, discarding Unix-style version
  numbers in the name. For example for 'libz.so.1.2.8' returns '.so'"""
  if filename in SPECIAL_ENDINGLESS_FILENAMES:
    return filename
  while filename:
    filename, suffix = os.path.splitext(filename)
    if not suffix[1:].isdigit():
      return suffix
  return ''


def get_library_basename(filename):
  """Similar to get_file_suffix this strips off all numeric suffixes and then
  then final non-numeric one.  For example for 'libz.so.1.2.8' returns 'libz'"""
  filename = os.path.basename(filename)
  while filename:
    filename, suffix = os.path.splitext(filename)
    # Keep stipping suffixes until we strip a non-numeric one.
    if not suffix[1:].isdigit():
      return filename


def get_secondary_target(target, ext):
  # Depending on the output format emscripten creates zero or more secondary
  # output files (e.g. the .wasm file when creating JS output, or the
  # .js and the .wasm file when creating html output.
  # Thus function names the secondary output files, while ensuring they
  # never collide with the primary one.
  base = unsuffixed(target)
  if get_file_suffix(target) == ext:
    base += '_'
  return base + ext


def in_temp(name):
  temp_dir = shared.get_emscripten_temp_dir()
  return os.path.join(temp_dir, os.path.basename(name))


def dedup_list(lst):
  # Since we require python 3.6, that ordering of dictionaries is guaranteed
  # to be insertion order so we can use 'dict' here but not 'set'.
  return list(dict.fromkeys(lst))


def move_file(src, dst):
  logging.debug('move: %s -> %s', src, dst)
  if os.path.isdir(dst):
    exit_with_error(f'cannot write output file `{dst}`: Is a directory')
  src = os.path.abspath(src)
  dst = os.path.abspath(dst)
  if src == dst:
    return
  if dst == os.devnull:
    return
  shutil.move(src, dst)


# Returns the subresource location for run-time access
def get_subresource_location(path, data_uri=None):
  if data_uri is None:
    data_uri = settings.SINGLE_FILE
  if data_uri:
    # if the path does not exist, then there is no data to encode
    if not os.path.exists(path):
      return ''
    data = base64.b64encode(utils.read_binary(path))
    return 'data:application/octet-stream;base64,' + data.decode('ascii')
  else:
    return os.path.basename(path)


@ToolchainProfiler.profile()
def package_files(options, target):
  rtn = []
  logger.debug('setting up files')
  file_args = ['--from-emcc']
  if options.preload_files:
    file_args.append('--preload')
    file_args += options.preload_files
  if options.embed_files:
    file_args.append('--embed')
    file_args += options.embed_files
  if options.exclude_files:
    file_args.append('--exclude')
    file_args += options.exclude_files
  if options.use_preload_cache:
    file_args.append('--use-preload-cache')
  if settings.LZ4:
    file_args.append('--lz4')
  if options.use_preload_plugins:
    file_args.append('--use-preload-plugins')
  if not settings.ENVIRONMENT_MAY_BE_NODE:
    file_args.append('--no-node')
  if options.embed_files:
    if settings.MEMORY64:
      file_args += ['--wasm64']
    object_file = in_temp('embedded_files.o')
    file_args += ['--obj-output=' + object_file]
    rtn.append(object_file)

  cmd = [shared.FILE_PACKAGER, shared.replace_suffix(target, '.data')] + file_args
  if options.preload_files:
    # Preloading files uses --pre-js code that runs before the module is loaded.
    file_code = shared.check_call(cmd, stdout=PIPE).stdout
    js_manipulation.add_files_pre_js(settings.PRE_JS_FILES, file_code)
  else:
    # Otherwise, we are embedding files, which does not require --pre-js code,
    # and instead relies on a static constrcutor to populate the filesystem.
    shared.check_call(cmd)

  return rtn


run_via_emxx = False


#
# Main run() function
#
def run(args):
  if run_via_emxx:
    clang = shared.CLANG_CXX
  else:
    clang = shared.CLANG_CC

  # Special case the handling of `-v` because it has a special/different meaning
  # when used with no other arguments.  In particular, we must handle this early
  # on, before we inject EMCC_CFLAGS.  This is because tools like cmake and
  # autoconf will run `emcc -v` to determine the compiler version and we don't
  # want that to break for users of EMCC_CFLAGS.
  if len(args) == 2 and args[1] == '-v':
    # autoconf likes to see 'GNU' in the output to enable shared object support
    print(version_string(), file=sys.stderr)
    return shared.check_call([clang, '-v'] + get_target_flags(), check=False).returncode

  # Additional compiler flags that we treat as if they were passed to us on the
  # commandline
  EMCC_CFLAGS = os.environ.get('EMCC_CFLAGS')
  if EMCC_CFLAGS:
    args += shlex.split(EMCC_CFLAGS)

  if DEBUG:
    logger.warning(f'invocation: {shared.shlex_join(args)} (in {os.getcwd()})')

  # Strip args[0] (program name)
  args = args[1:]

  misc_temp_files = shared.get_temp_files()

  # Handle some global flags

  # read response files very early on
  try:
    args = substitute_response_files(args)
  except IOError as e:
    exit_with_error(e)

  if '--help' in args:
    # Documentation for emcc and its options must be updated in:
    #    site/source/docs/tools_reference/emcc.rst
    # This then gets built (via: `make -C site text`) to:
    #    site/build/text/docs/tools_reference/emcc.txt
    # This then needs to be copied to its final home in docs/emcc.txt from where
    # we read it here.  We have CI rules that ensure its always up-to-date.
    print(read_file(utils.path_from_root('docs/emcc.txt')))

    print('''
------------------------------------------------------------------

emcc: supported targets: llvm bitcode, WebAssembly, NOT elf
(autoconf likes to see elf above to enable shared object support)
''')
    return 0

  if '--version' in args:
    print(version_string())
    print('''\
Copyright (C) 2014 the Emscripten authors (see AUTHORS.txt)
This is free and open source software under the MIT license.
There is NO warranty; not even for MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
''')
    return 0

  if '-dumpmachine' in args:
    print(shared.get_llvm_target())
    return 0

  if '-dumpversion' in args: # gcc's doc states "Print the compiler version [...] and don't do anything else."
    print(shared.EMSCRIPTEN_VERSION)
    return 0

  if '--cflags' in args:
    # fake running the command, to see the full args we pass to clang
    args = [x for x in args if x != '--cflags']
    with misc_temp_files.get_file(suffix='.o') as temp_target:
      input_file = 'hello_world.c'
      compiler = shared.EMCC
      if run_via_emxx:
        compiler = shared.EMXX
      cmd = [compiler, utils.path_from_root('test', input_file), '-v', '-c', '-o', temp_target] + args
      proc = run_process(cmd, stderr=PIPE, check=False)
      if proc.returncode != 0:
        print(proc.stderr)
        exit_with_error('error getting cflags')
      lines = [x for x in proc.stderr.splitlines() if clang in x and input_file in x]
      parts = shlex.split(lines[0].replace('\\', '\\\\'))
      parts = [x for x in parts if x not in ['-c', '-o', '-v', '-emit-llvm'] and input_file not in x and temp_target not in x]
      print(shared.shlex_join(parts[1:]))
    return 0

  passthrough_flags = ['-print-search-dirs', '-print-libgcc-file-name']
  if any(a in args for a in passthrough_flags) or any(a.startswith('-print-file-name=') for a in args):
    return run_process([clang] + args + get_cflags(args, run_via_emxx), check=False).returncode

  ## Process argument and setup the compiler
  state = EmccState(args)
  options, newargs = phase_parse_arguments(state)

  shared.check_sanity()

  if 'EMMAKEN_NO_SDK' in os.environ:
    exit_with_error('EMMAKEN_NO_SDK is no longer supported.  The standard -nostdlib and -nostdinc flags should be used instead')

  if 'EMMAKEN_COMPILER' in os.environ:
    exit_with_error('`EMMAKEN_COMPILER` is no longer supported.\n' +
                    'Please use the `LLVM_ROOT` and/or `COMPILER_WRAPPER` config settings instread')

  if 'EMMAKEN_CFLAGS' in os.environ:
    exit_with_error('`EMMAKEN_CFLAGS` is no longer supported, please use `EMCC_CFLAGS` instead')

  if 'EMCC_REPRODUCE' in os.environ:
    options.reproduce = os.environ['EMCC_REPRODUCE']

  # For internal consistency, ensure we don't attempt or read or write any link time
  # settings until we reach the linking phase.
  settings.limit_settings(COMPILE_TIME_SETTINGS)

  newargs, input_files = phase_setup(options, state, newargs)

  if options.reproduce:
    create_reproduce_file(options.reproduce, args)

  if state.mode == Mode.POST_LINK_ONLY:
    settings.limit_settings(None)
    target, wasm_target = phase_linker_setup(options, state, newargs)
    process_libraries(state, [], options)
    if len(input_files) != 1:
      exit_with_error('--post-link requires a single input file')
    phase_post_link(options, state, input_files[0][1], wasm_target, target, {})
    return 0

  ## Compile source code to object files
  linker_inputs = phase_compile_inputs(options, state, newargs, input_files)

  if state.mode != Mode.COMPILE_AND_LINK:
    logger.debug('stopping after compile phase')
    for flag in state.link_flags:
      diagnostics.warning('unused-command-line-argument', "argument unused during compilation: '%s'" % flag[1])
    for f in linker_inputs:
      diagnostics.warning('unused-command-line-argument', "%s: linker input file unused because linking not done" % f[1])

    return 0

  # We have now passed the compile phase, allow reading/writing of all settings.
  settings.limit_settings(None)

  if options.output_file and options.output_file.startswith('-'):
    exit_with_error(f'invalid output filename: `{options.output_file}`')

  target, wasm_target = phase_linker_setup(options, state, newargs)

  # Link object files using wasm-ld or llvm-link (for bitcode linking)
  linker_arguments = phase_calculate_linker_inputs(options, state, linker_inputs)

  # Embed and preload files
  if len(options.preload_files) or len(options.embed_files):
    linker_arguments += package_files(options, target)

  if options.oformat == OFormat.OBJECT:
    logger.debug(f'link_to_object: {linker_arguments} -> {target}')
    building.link_to_object(linker_arguments, target)
    logger.debug('stopping after linking to object file')
    return 0

  js_syms = {}
  if not settings.SIDE_MODULE or settings.ASYNCIFY:
    js_info = get_js_sym_info()
    if not settings.SIDE_MODULE:
      js_syms = js_info['deps']
    if settings.ASYNCIFY:
      settings.ASYNCIFY_IMPORTS += ['env.' + x for x in js_info['asyncFuncs']]

  phase_calculate_system_libraries(state, linker_arguments, newargs)

  phase_link(linker_arguments, wasm_target, js_syms)

  # Special handling for when the user passed '-Wl,--version'.  In this case the linker
  # does not create the output file, but just prints its version and exits with 0.
  if '--version' in linker_arguments:
    return 0

  # TODO(sbc): In theory we should really run the whole pipeline even if the output is
  # /dev/null, but that will take some refactoring
  if target == os.devnull:
    return 0

  # Perform post-link steps (unless we are running bare mode)
  if options.oformat != OFormat.BARE:
    phase_post_link(options, state, wasm_target, wasm_target, target, js_syms)

  return 0


@ToolchainProfiler.profile_block('calculate linker inputs')
def phase_calculate_linker_inputs(options, state, linker_inputs):
  using_lld = not (options.oformat == OFormat.OBJECT and settings.LTO)
  state.link_flags = filter_link_flags(state.link_flags, using_lld)

  # Decide what we will link
  process_libraries(state, linker_inputs, options.embind_emit_tsd)

  linker_args = [val for _, val in sorted(linker_inputs + state.link_flags)]

  # If we are linking to an intermediate object then ignore other
  # "fake" dynamic libraries, since otherwise we will end up with
  # multiple copies in the final executable.
  if options.oformat == OFormat.OBJECT or options.ignore_dynamic_linking:
    linker_args = filter_out_dynamic_libs(options, linker_args)
  else:
    linker_args = filter_out_duplicate_dynamic_libs(linker_args)

  if settings.MAIN_MODULE:
    dylibs = [a for a in linker_args if building.is_wasm_dylib(a)]
    process_dynamic_libs(dylibs, state.lib_dirs)

  return linker_args


def normalize_boolean_setting(name, value):
  # boolean NO_X settings are aliases for X
  # (note that *non*-boolean setting values have special meanings,
  # and we can't just flip them, so leave them as-is to be
  # handled in a special way later)
  if name.startswith('NO_') and value in ('0', '1'):
    name = removeprefix(name, 'NO_')
    value = str(1 - int(value))
  return name, value


@ToolchainProfiler.profile_block('parse arguments')
def phase_parse_arguments(state):
  """The first phase of the compiler.  Parse command line argument and
  populate settings.
  """
  newargs = list(state.orig_args)

  # Scan and strip emscripten specific cmdline warning flags.
  # This needs to run before other cmdline flags have been parsed, so that
  # warnings are properly printed during arg parse.
  newargs = diagnostics.capture_warnings(newargs)

  for i in range(len(newargs)):
    if newargs[i] in ('-l', '-L', '-I'):
      # Scan for individual -l/-L/-I arguments and concatenate the next arg on
      # if there is no suffix
      newargs[i] += newargs[i + 1]
      newargs[i + 1] = ''

  options, settings_changes, user_js_defines, newargs = parse_args(newargs)

  if options.post_link or options.oformat == OFormat.BARE:
    diagnostics.warning('experimental', '--oformat=bare/--post-link are experimental and subject to change.')

  explicit_settings_changes, newargs = parse_s_args(newargs)
  settings_changes += explicit_settings_changes

  for s in settings_changes:
    key, value = s.split('=', 1)
    key, value = normalize_boolean_setting(key, value)
    user_settings[key] = value

  # STRICT is used when applying settings so it needs to be applied first before
  # calling `apply_user_settings`.
  strict_cmdline = user_settings.get('STRICT')
  if strict_cmdline:
    settings.STRICT = int(strict_cmdline)

  # Apply user -jsD settings
  for s in user_js_defines:
    settings[s[0]] = s[1]

  # Apply -s settings in newargs here (after optimization levels, so they can override them)
  apply_user_settings()

  return options, newargs


@ToolchainProfiler.profile_block('setup')
def phase_setup(options, state, newargs):
  """Second phase: configure and setup the compiler based on the specified settings and arguments.
  """

  if settings.RUNTIME_LINKED_LIBS:
    diagnostics.warning('deprecated', 'RUNTIME_LINKED_LIBS is deprecated; you can simply list the libraries directly on the commandline now')
    newargs += settings.RUNTIME_LINKED_LIBS

  if settings.STRICT:
    default_setting('DEFAULT_TO_CXX', 0)

  # Find input files

  # These three arrays are used to store arguments of different types for
  # type-specific processing. In order to shuffle the arguments back together
  # after processing, all of these arrays hold tuples (original_index, value).
  # Note that the index part of the tuple can have a fractional part for input
  # arguments that expand into multiple processed arguments, as in -Wl,-f1,-f2.
  input_files = []

  # find input files with a simple heuristic. we should really analyze
  # based on a full understanding of gcc params, right now we just assume that
  # what is left contains no more |-x OPT| things
  skip = False
  has_header_inputs = False
  for i in range(len(newargs)):
    if skip:
      skip = False
      continue

    arg = newargs[i]
    if arg in ('-MT', '-MF', '-MJ', '-MQ', '-D', '-U', '-o', '-x',
               '-Xpreprocessor', '-include', '-imacros', '-idirafter',
               '-iprefix', '-iwithprefix', '-iwithprefixbefore',
               '-isysroot', '-imultilib', '-A', '-isystem', '-iquote',
               '-install_name', '-compatibility_version',
               '-current_version', '-I', '-L', '-include-pch',
               '-undefined',
               '-Xlinker', '-Xclang', '-z'):
      skip = True

    if not arg.startswith('-'):
      # we already removed -o <target>, so all these should be inputs
      newargs[i] = ''
      # os.devnul should always be reported as existing but there is bug in windows
      # python before 3.8:
      # https://bugs.python.org/issue1311
      if not os.path.exists(arg) and arg != os.devnull:
        exit_with_error('%s: No such file or directory ("%s" was expected to be an input file, based on the commandline arguments provided)', arg, arg)
      file_suffix = get_file_suffix(arg)
      if file_suffix in HEADER_ENDINGS:
        has_header_inputs = True
      if file_suffix in STATICLIB_ENDINGS and not building.is_ar(arg):
        if building.is_bitcode(arg):
          message = f'{arg}: File has a suffix of a static library {STATICLIB_ENDINGS}, but instead is an LLVM bitcode file! When linking LLVM bitcode files use .bc or .o.'
        else:
          message = arg + ': Unknown format, not a static library!'
        exit_with_error(message)
      if file_suffix in DYNAMICLIB_ENDINGS and not building.is_bitcode(arg) and not building.is_wasm(arg):
        # For shared libraries that are neither bitcode nor wasm, assuming its local native
        # library and attempt to find a library by the same name in our own library path.
        # TODO(sbc): Do we really need this feature?  See test_other.py:test_local_link
        libname = removeprefix(get_library_basename(arg), 'lib')
        flag = '-l' + libname
        diagnostics.warning('map-unrecognized-libraries', f'unrecognized file type: `{arg}`.  Mapping to `{flag}` and hoping for the best')
        add_link_flag(state, i, flag)
      else:
        input_files.append((i, arg))
    elif arg.startswith('-L'):
      add_link_flag(state, i, arg)
      newargs[i] = ''
    elif arg.startswith('-l'):
      add_link_flag(state, i, arg)
      newargs[i] = ''
    elif arg == '-z':
      add_link_flag(state, i, newargs[i])
      add_link_flag(state, i + 1, newargs[i + 1])
      newargs[i] = ''
      newargs[i + 1] = ''
    elif arg.startswith('-z'):
      add_link_flag(state, i, newargs[i])
      newargs[i] = ''
    elif arg.startswith('-Wl,'):
      # Multiple comma separated link flags can be specified. Create fake
      # fractional indices for these: -Wl,a,b,c,d at index 4 becomes:
      # (4, a), (4.25, b), (4.5, c), (4.75, d)
      link_flags_to_add = arg.split(',')[1:]
      for flag_index, flag in enumerate(link_flags_to_add):
        add_link_flag(state, i + float(flag_index) / len(link_flags_to_add), flag)
      newargs[i] = ''
    elif arg == '-Xlinker':
      add_link_flag(state, i + 1, newargs[i + 1])
      newargs[i] = ''
      newargs[i + 1] = ''
    elif arg == '-s':
      # -s and some other compiler flags are normally passed onto the linker
      # TODO(sbc): Pass this and other flags through when using lld
      # link_flags.append((i, arg))
      newargs[i] = ''
    elif arg == '-':
      input_files.append((i, arg))
      newargs[i] = ''

  if not input_files and not state.link_flags:
    exit_with_error('no input files')

  newargs = [a for a in newargs if a]

  # SSEx is implemented on top of SIMD128 instruction set, but do not pass SSE flags to LLVM
  # so it won't think about generating native x86 SSE code.
  newargs = [x for x in newargs if x not in SIMD_INTEL_FEATURE_TOWER and x not in SIMD_NEON_FLAGS]

  state.has_dash_c = '-c' in newargs or '--precompile' in newargs
  state.has_dash_S = '-S' in newargs
  state.has_dash_E = '-E' in newargs

  if options.post_link:
    state.mode = Mode.POST_LINK_ONLY
  elif state.has_dash_E or '-M' in newargs or '-MM' in newargs or '-fsyntax-only' in newargs:
    state.mode = Mode.PREPROCESS_ONLY
  elif has_header_inputs:
    state.mode = Mode.PCH
  elif state.has_dash_c or state.has_dash_S:
    state.mode = Mode.COMPILE_ONLY

  if state.mode in (Mode.COMPILE_ONLY, Mode.PREPROCESS_ONLY):
    for key in user_settings:
      if key not in COMPILE_TIME_SETTINGS:
        diagnostics.warning(
            'unused-command-line-argument',
            "linker setting ignored during compilation: '%s'" % key)
    for arg in state.orig_args:
      if arg in LINK_ONLY_FLAGS:
        diagnostics.warning(
            'unused-command-line-argument',
            "linker flag ignored during compilation: '%s'" % arg)
    if state.has_dash_c:
      if '-emit-llvm' in newargs:
        options.default_object_extension = '.bc'
    elif state.has_dash_S:
      if '-emit-llvm' in newargs:
        options.default_object_extension = '.ll'
      else:
        options.default_object_extension = '.s'
    elif '-M' in newargs or '-MM' in newargs:
      options.default_object_extension = '.mout' # not bitcode, not js; but just dependency rule of the input file

    if options.output_file and len(input_files) > 1:
      exit_with_error('cannot specify -o with -c/-S/-E/-M and multiple source files')
  else:
    for arg in state.orig_args:
      if any(arg.startswith(f) for f in COMPILE_ONLY_FLAGS):
        diagnostics.warning(
            'unused-command-line-argument',
            "compiler flag ignored during linking: '%s'" % arg)

  if settings.MAIN_MODULE or settings.SIDE_MODULE:
    settings.RELOCATABLE = 1

  if 'USE_PTHREADS' in user_settings:
    settings.PTHREADS = settings.USE_PTHREADS

  # Pthreads and Wasm Workers require targeting shared Wasm memory (SAB).
  if settings.PTHREADS or settings.WASM_WORKERS:
    settings.SHARED_MEMORY = 1

  if settings.PTHREADS and '-pthread' not in newargs:
    newargs += ['-pthread']
  elif settings.SHARED_MEMORY:
    if '-matomics' not in newargs:
      newargs += ['-matomics']
    if '-mbulk-memory' not in newargs:
      newargs += ['-mbulk-memory']

  if settings.SHARED_MEMORY:
    settings.BULK_MEMORY = 1

  if 'DISABLE_EXCEPTION_CATCHING' in user_settings and 'EXCEPTION_CATCHING_ALLOWED' in user_settings:
    # If we get here then the user specified both DISABLE_EXCEPTION_CATCHING and EXCEPTION_CATCHING_ALLOWED
    # on the command line.  This is no longer valid so report either an error or a warning (for
    # backwards compat with the old `DISABLE_EXCEPTION_CATCHING=2`
    if user_settings['DISABLE_EXCEPTION_CATCHING'] in ('0', '2'):
      diagnostics.warning('deprecated', 'DISABLE_EXCEPTION_CATCHING=X is no longer needed when specifying EXCEPTION_CATCHING_ALLOWED')
    else:
      exit_with_error('DISABLE_EXCEPTION_CATCHING and EXCEPTION_CATCHING_ALLOWED are mutually exclusive')

  if settings.EXCEPTION_CATCHING_ALLOWED:
    settings.DISABLE_EXCEPTION_CATCHING = 0

  if settings.WASM_EXCEPTIONS:
    if user_settings.get('DISABLE_EXCEPTION_CATCHING') == '0':
      exit_with_error('DISABLE_EXCEPTION_CATCHING=0 is not compatible with -fwasm-exceptions')
    if user_settings.get('DISABLE_EXCEPTION_THROWING') == '0':
      exit_with_error('DISABLE_EXCEPTION_THROWING=0 is not compatible with -fwasm-exceptions')
    # -fwasm-exceptions takes care of enabling them, so users aren't supposed to
    # pass them explicitly, regardless of their values
    if 'DISABLE_EXCEPTION_CATCHING' in user_settings or 'DISABLE_EXCEPTION_THROWING' in user_settings:
      diagnostics.warning('emcc', 'You no longer need to pass DISABLE_EXCEPTION_CATCHING or DISABLE_EXCEPTION_THROWING when using Wasm exceptions')
    settings.DISABLE_EXCEPTION_CATCHING = 1
    settings.DISABLE_EXCEPTION_THROWING = 1

    if user_settings.get('ASYNCIFY') == '1':
      diagnostics.warning('emcc', 'ASYNCIFY=1 is not compatible with -fwasm-exceptions. Parts of the program that mix ASYNCIFY and exceptions will not compile.')

    if user_settings.get('SUPPORT_LONGJMP') == 'emscripten':
      exit_with_error('SUPPORT_LONGJMP=emscripten is not compatible with -fwasm-exceptions')

  if settings.DISABLE_EXCEPTION_THROWING and not settings.DISABLE_EXCEPTION_CATCHING:
    exit_with_error("DISABLE_EXCEPTION_THROWING was set (probably from -fno-exceptions) but is not compatible with enabling exception catching (DISABLE_EXCEPTION_CATCHING=0). If you don't want exceptions, set DISABLE_EXCEPTION_CATCHING to 1; if you do want exceptions, don't link with -fno-exceptions")

  if settings.MEMORY64:
    diagnostics.warning('experimental', '-sMEMORY64 is still experimental. Many features may not work.')

  # Wasm SjLj cannot be used with Emscripten EH
  if settings.SUPPORT_LONGJMP == 'wasm':
    # DISABLE_EXCEPTION_THROWING is 0 by default for Emscripten EH throwing, but
    # Wasm SjLj cannot be used with Emscripten EH. We error out if
    # DISABLE_EXCEPTION_THROWING=0 is explicitly requested by the user;
    # otherwise we disable it here.
    if user_settings.get('DISABLE_EXCEPTION_THROWING') == '0':
      exit_with_error('SUPPORT_LONGJMP=wasm cannot be used with DISABLE_EXCEPTION_THROWING=0')
    # We error out for DISABLE_EXCEPTION_CATCHING=0, because it is 1 by default
    # and this can be 0 only if the user specifies so.
    if user_settings.get('DISABLE_EXCEPTION_CATCHING') == '0':
      exit_with_error('SUPPORT_LONGJMP=wasm cannot be used with DISABLE_EXCEPTION_CATCHING=0')
    default_setting('DISABLE_EXCEPTION_THROWING', 1)

  # SUPPORT_LONGJMP=1 means the default SjLj handling mechanism, which is 'wasm'
  # if Wasm EH is used and 'emscripten' otherwise.
  if settings.SUPPORT_LONGJMP == 1:
    if settings.WASM_EXCEPTIONS:
      settings.SUPPORT_LONGJMP = 'wasm'
    else:
      settings.SUPPORT_LONGJMP = 'emscripten'

  return (newargs, input_files)


def setup_pthreads(target):
  if settings.RELOCATABLE:
    # phtreads + dyanmic linking has certain limitations
    if settings.SIDE_MODULE:
      diagnostics.warning('experimental', '-sSIDE_MODULE + pthreads is experimental')
    elif settings.MAIN_MODULE:
      diagnostics.warning('experimental', '-sMAIN_MODULE + pthreads is experimental')
    elif settings.LINKABLE:
      diagnostics.warning('experimental', '-sLINKABLE + pthreads is experimental')
  if settings.ALLOW_MEMORY_GROWTH:
    diagnostics.warning('pthreads-mem-growth', '-pthread + ALLOW_MEMORY_GROWTH may run non-wasm code slowly, see https://github.com/WebAssembly/design/issues/1271')

  default_setting('DEFAULT_PTHREAD_STACK_SIZE', settings.STACK_SIZE)

  # Functions needs to be exported from the module since they are used in worker.js
  settings.REQUIRED_EXPORTS += [
    'emscripten_dispatch_to_thread_',
    '_emscripten_thread_free_data',
    'emscripten_main_runtime_thread_id',
    'emscripten_main_thread_process_queued_calls',
    '_emscripten_run_in_main_runtime_thread_js',
    'emscripten_stack_set_limits',
  ]

  if settings.MAIN_MODULE:
    settings.REQUIRED_EXPORTS += [
      '_emscripten_dlsync_self',
      '_emscripten_dlsync_self_async',
      '_emscripten_proxy_dlsync',
      '_emscripten_proxy_dlsync_async',
      '__dl_seterr',
    ]

  settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += [
    '$exitOnMainThread',
  ]
  # Some symbols are required by worker.js.
  # Because emitDCEGraph only considers the main js file, and not worker.js
  # we have explicitly mark these symbols as user-exported so that they will
  # kept alive through DCE.
  # TODO: Find a less hacky way to do this, perhaps by also scanning worker.js
  # for roots.
  worker_imports = [
    '__emscripten_thread_init',
    '__emscripten_thread_exit',
    '__emscripten_thread_crashed',
    '__emscripten_thread_mailbox_await',
    '__emscripten_tls_init',
    '_pthread_self',
    'checkMailbox',
  ]
  settings.EXPORTED_FUNCTIONS += worker_imports
  building.user_requested_exports.update(worker_imports)

  # set location of worker.js
  settings.PTHREAD_WORKER_FILE = unsuffixed_basename(target) + '.worker.js'

  if settings.MINIMAL_RUNTIME:
    building.user_requested_exports.add('exit')

  # All proxying async backends will need this.
  if settings.WASMFS:
    settings.REQUIRED_EXPORTS += ['emscripten_proxy_finish']
    # TODO: Remove this once we no longer need the heartbeat hack in
    # wasmfs/thread_utils.h
    settings.REQUIRED_EXPORTS += ['emscripten_proxy_execute_queue']

  # pthread stack setup and other necessary utilities
  def include_and_export(name):
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$' + name]
    settings.EXPORTED_FUNCTIONS += [name]

  include_and_export('establishStackSpace')
  include_and_export('invokeEntryPoint')
  include_and_export('PThread')
  if not settings.MINIMAL_RUNTIME:
    # keepRuntimeAlive does not apply to MINIMAL_RUNTIME.
    settings.EXPORTED_RUNTIME_METHODS += ['keepRuntimeAlive', 'ExitStatus', 'wasmMemory']

  if settings.MODULARIZE:
    if not settings.EXPORT_ES6 and settings.EXPORT_NAME == 'Module':
      exit_with_error('pthreads + MODULARIZE currently require you to set -sEXPORT_NAME=Something (see settings.js) to Something != Module, so that the .worker.js file can work')

    # MODULARIZE+PTHREADS mode requires extra exports out to Module so that worker.js
    # can access them:

    # general threading variables:
    settings.EXPORTED_RUNTIME_METHODS += ['PThread']

    # To keep code size to minimum, MINIMAL_RUNTIME does not utilize the global ExitStatus
    # object, only regular runtime has it.
    if not settings.MINIMAL_RUNTIME:
      settings.EXPORTED_RUNTIME_METHODS += ['ExitStatus']


@ToolchainProfiler.profile_block('linker_setup')
def phase_linker_setup(options, state, newargs):
  autoconf = os.environ.get('EMMAKEN_JUST_CONFIGURE') or 'conftest.c' in state.orig_args or 'conftest.cpp' in state.orig_args
  if autoconf:
    # configure tests want a more shell-like style, where we emit return codes on exit()
    settings.EXIT_RUNTIME = 1
    # use node.js raw filesystem access, to behave just like a native executable
    settings.NODERAWFS = 1
    # Add `#!` line to output JS and make it executable.
    options.executable = True

  system_libpath = '-L' + str(cache.get_lib_dir(absolute=True))
  add_link_flag(state, sys.maxsize, system_libpath)

  if settings.OPT_LEVEL >= 1:
    default_setting('ASSERTIONS', 0)

  if options.emrun:
    options.pre_js.append(utils.path_from_root('src/emrun_prejs.js'))
    options.post_js.append(utils.path_from_root('src/emrun_postjs.js'))
    # emrun mode waits on program exit
    settings.EXIT_RUNTIME = 1

  if options.cpu_profiler:
    options.post_js.append(utils.path_from_root('src/cpuprofiler.js'))

  if not settings.RUNTIME_DEBUG:
    settings.RUNTIME_DEBUG = bool(settings.LIBRARY_DEBUG or
                                  settings.GL_DEBUG or
                                  settings.DYLINK_DEBUG or
                                  settings.OPENAL_DEBUG or
                                  settings.SYSCALL_DEBUG or
                                  settings.WEBSOCKET_DEBUG or
                                  settings.SOCKET_DEBUG or
                                  settings.FETCH_DEBUG or
                                  settings.EXCEPTION_DEBUG or
                                  settings.PTHREADS_DEBUG or
                                  settings.ASYNCIFY_DEBUG)

  if options.memory_profiler:
    settings.MEMORYPROFILER = 1

  if settings.PTHREADS_PROFILING:
    if not settings.ASSERTIONS:
      exit_with_error('PTHREADS_PROFILING only works with ASSERTIONS enabled')
    options.post_js.append(utils.path_from_root('src/threadprofiler.js'))

  options.extern_pre_js = read_js_files(options.extern_pre_js)
  options.extern_post_js = read_js_files(options.extern_post_js)

  # TODO: support source maps with js_transform
  if options.js_transform and settings.GENERATE_SOURCE_MAP:
    logger.warning('disabling source maps because a js transform is being done')
    settings.GENERATE_SOURCE_MAP = 0

  # options.output_file is the user-specified one, target is what we will generate
  if options.output_file:
    target = options.output_file
    # check for the existence of the output directory now, to avoid having
    # to do so repeatedly when each of the various output files (.mem, .wasm,
    # etc) are written. This gives a more useful error message than the
    # IOError and python backtrace that users would otherwise see.
    dirname = os.path.dirname(target)
    if dirname and not os.path.isdir(dirname):
      exit_with_error("specified output file (%s) is in a directory that does not exist" % target)
  elif autoconf:
    # Autoconf expects the executable output file to be called `a.out`
    target = 'a.out'
  elif settings.SIDE_MODULE:
    target = 'a.out.wasm'
  else:
    target = 'a.out.js'

  final_suffix = get_file_suffix(target)

  if settings.EXTRA_EXPORTED_RUNTIME_METHODS:
    diagnostics.warning('deprecated', 'EXTRA_EXPORTED_RUNTIME_METHODS is deprecated, please use EXPORTED_RUNTIME_METHODS instead')
    settings.EXPORTED_RUNTIME_METHODS += settings.EXTRA_EXPORTED_RUNTIME_METHODS

  # If no output format was specified we try to deduce the format based on
  # the output filename extension
  if not options.oformat and (options.relocatable or (options.shared and not settings.SIDE_MODULE)):
    # Until we have a better story for actually producing runtime shared libraries
    # we support a compatibility mode where shared libraries are actually just
    # object files linked with `wasm-ld --relocatable` or `llvm-link` in the case
    # of LTO.
    if final_suffix in EXECUTABLE_ENDINGS:
      diagnostics.warning('emcc', '-shared/-r used with executable output suffix. This behaviour is deprecated.  Please remove -shared/-r to build an executable or avoid the executable suffix (%s) when building object files.' % final_suffix)
    else:
      if options.shared:
        diagnostics.warning('emcc', 'linking a library with `-shared` will emit a static object file.  This is a form of emulation to support existing build systems.  If you want to build a runtime shared library use the SIDE_MODULE setting.')
      options.oformat = OFormat.OBJECT

  if not options.oformat:
    if settings.SIDE_MODULE or final_suffix == '.wasm':
      options.oformat = OFormat.WASM
    elif final_suffix == '.mjs':
      options.oformat = OFormat.MJS
    elif final_suffix == '.html':
      options.oformat = OFormat.HTML
    else:
      options.oformat = OFormat.JS

  if options.oformat == OFormat.MJS:
    settings.EXPORT_ES6 = 1
    settings.MODULARIZE = 1

  if options.oformat in (OFormat.WASM, OFormat.BARE):
    # If the user asks directly for a wasm file then this *is* the target
    wasm_target = target
  else:
    # Otherwise the wasm file is produced alongside the final target.
    wasm_target = get_secondary_target(target, '.wasm')

  if settings.SAFE_HEAP not in [0, 1, 2]:
    exit_with_error('emcc: SAFE_HEAP must be 0, 1 or 2')

  if not settings.WASM:
    # When the user requests non-wasm output, we enable wasm2js. that is,
    # we still compile to wasm normally, but we compile the final output
    # to js.
    settings.WASM = 1
    settings.WASM2JS = 1
  if settings.WASM == 2:
    # Requesting both Wasm and Wasm2JS support
    settings.WASM2JS = 1

  if options.oformat == OFormat.WASM and not settings.SIDE_MODULE:
    # if the output is just a wasm file, it will normally be a standalone one,
    # as there is no JS. an exception are side modules, as we can't tell at
    # compile time whether JS will be involved or not - the main module may
    # have JS, and the side module is expected to link against that.
    # we also do not support standalone mode in fastcomp.
    settings.STANDALONE_WASM = 1

  if settings.LZ4:
    settings.EXPORTED_RUNTIME_METHODS += ['LZ4']

  if settings.PURE_WASI:
    settings.STANDALONE_WASM = 1
    settings.WASM_BIGINT = 1

  if options.no_entry:
    settings.EXPECT_MAIN = 0
  elif settings.STANDALONE_WASM:
    if '_main' in settings.EXPORTED_FUNCTIONS:
      # TODO(sbc): Make this into a warning?
      logger.debug('including `_main` in EXPORTED_FUNCTIONS is not necessary in standalone mode')
  else:
    # In normal non-standalone mode we have special handling of `_main` in EXPORTED_FUNCTIONS.
    # 1. If the user specifies exports, but doesn't include `_main` we assume they want to build a
    #    reactor.
    # 2. If the user doesn't export anything we default to exporting `_main` (unless `--no-entry`
    #    is specified (see above).
    if 'EXPORTED_FUNCTIONS' in user_settings:
      if '_main' not in settings.USER_EXPORTED_FUNCTIONS:
        settings.EXPECT_MAIN = 0
    else:
      assert not settings.EXPORTED_FUNCTIONS
      settings.EXPORTED_FUNCTIONS = ['_main']

  if settings.STANDALONE_WASM:
    # In STANDALONE_WASM mode we either build a command or a reactor.
    # See https://github.com/WebAssembly/WASI/blob/main/design/application-abi.md
    # For a command we always want EXIT_RUNTIME=1
    # For a reactor we always want EXIT_RUNTIME=0
    if 'EXIT_RUNTIME' in user_settings:
      exit_with_error('Explicitly setting EXIT_RUNTIME not compatible with STANDALONE_WASM.  EXIT_RUNTIME will always be True for programs (with a main function) and False for reactors (not main function).')
    settings.EXIT_RUNTIME = settings.EXPECT_MAIN
    settings.IGNORE_MISSING_MAIN = 0
    # the wasm must be runnable without the JS, so there cannot be anything that
    # requires JS legalization
    settings.LEGALIZE_JS_FFI = 0
    if 'MEMORY_GROWTH_LINEAR_STEP' in user_settings:
      exit_with_error('MEMORY_GROWTH_LINEAR_STEP is not compatible with STANDALONE_WASM')
    if 'MEMORY_GROWTH_GEOMETRIC_CAP' in user_settings:
      exit_with_error('MEMORY_GROWTH_GEOMETRIC_CAP is not compatible with STANDALONE_WASM')
    if settings.MINIMAL_RUNTIME:
      exit_with_error('MINIMAL_RUNTIME reduces JS size, and is incompatible with STANDALONE_WASM which focuses on ignoring JS anyhow and being 100% wasm')

  # Note the exports the user requested
  building.user_requested_exports.update(settings.EXPORTED_FUNCTIONS)

  if '_main' in settings.EXPORTED_FUNCTIONS:
    settings.EXPORT_IF_DEFINED.append('__main_argc_argv')
  elif settings.ASSERTIONS and not settings.STANDALONE_WASM:
    # In debug builds when `main` is not explicitly requested as an
    # export we still add it to EXPORT_IF_DEFINED so that we can warn
    # users who forget to explicitly export `main`.
    # See other.test_warn_unexported_main.
    # This is not needed in STANDALONE_WASM mode since we export _start
    # (unconditionally) rather than main.
    settings.EXPORT_IF_DEFINED.append('main')

  if settings.ASSERTIONS:
    # Exceptions are thrown with a stack trace by default when ASSERTIONS is
    # set and when building with either -fexceptions or -fwasm-exceptions.
    if 'EXCEPTION_STACK_TRACES' in user_settings and not settings.EXCEPTION_STACK_TRACES:
      exit_with_error('EXCEPTION_STACK_TRACES cannot be disabled when ASSERTIONS are enabled')
    if settings.WASM_EXCEPTIONS or not settings.DISABLE_EXCEPTION_CATCHING:
      settings.EXCEPTION_STACK_TRACES = 1

    # -sASSERTIONS implies basic stack overflow checks, and ASSERTIONS=2
    # implies full stack overflow checks. However, we don't set this default in
    # PURE_WASI, or when we are linking without standard libraries because
    # STACK_OVERFLOW_CHECK depends on emscripten_stack_get_end which is defined
    # in libcompiler-rt.
    if not settings.PURE_WASI and '-nostdlib' not in newargs and '-nodefaultlibs' not in newargs:
      default_setting('STACK_OVERFLOW_CHECK', max(settings.ASSERTIONS, settings.STACK_OVERFLOW_CHECK))

  # For users that opt out of WARN_ON_UNDEFINED_SYMBOLS we assume they also
  # want to opt out of ERROR_ON_UNDEFINED_SYMBOLS.
  if user_settings.get('WARN_ON_UNDEFINED_SYMBOLS') == '0':
    default_setting('ERROR_ON_UNDEFINED_SYMBOLS', 0)

  # It is unlikely that developers targeting "native web" APIs with MINIMAL_RUNTIME need
  # errno support by default.
  if settings.MINIMAL_RUNTIME:
    default_setting('SUPPORT_ERRNO', 0)
    # Require explicit -lfoo.js flags to link with JS libraries.
    default_setting('AUTO_JS_LIBRARIES', 0)
    # When using MINIMAL_RUNTIME, symbols should only be exported if requested.
    default_setting('EXPORT_KEEPALIVE', 0)
    default_setting('USE_GLFW', 0)

  if settings.STRICT_JS and (settings.MODULARIZE or settings.EXPORT_ES6):
    exit_with_error("STRICT_JS doesn't work with MODULARIZE or EXPORT_ES6")

  if settings.STRICT:
    if not settings.MODULARIZE and not settings.EXPORT_ES6:
      default_setting('STRICT_JS', 1)
    default_setting('AUTO_JS_LIBRARIES', 0)
    default_setting('AUTO_NATIVE_LIBRARIES', 0)
    default_setting('AUTO_ARCHIVE_INDEXES', 0)
    default_setting('IGNORE_MISSING_MAIN', 0)
    default_setting('ALLOW_UNIMPLEMENTED_SYSCALLS', 0)

  if 'GLOBAL_BASE' not in user_settings and not settings.SHRINK_LEVEL and not settings.OPT_LEVEL:
    # When optimizing for size it helps to put static data first before
    # the stack (sincs this makes instructions for accessing this data
    # use a smaller LEB encoding).
    # However, for debugability is better to have the stack come first
    # (becuase stack overflows will trap rather than corrupting data).
    settings.STACK_FIRST = True

  # Default to TEXTDECODER=2 (always use TextDecoder to decode UTF-8 strings)
  # in -Oz builds, since custom decoder for UTF-8 takes up space.
  # In pthreads enabled builds, TEXTDECODER==2 may not work, see
  # https://github.com/whatwg/encoding/issues/172
  # When supporting shell environments, do not do this as TextDecoder is not
  # widely supported there.
  if settings.SHRINK_LEVEL >= 2 and not settings.SHARED_MEMORY and \
     not settings.ENVIRONMENT_MAY_BE_SHELL:
    default_setting('TEXTDECODER', 2)

  # If set to 1, we will run the autodebugger (the automatic debugging tool, see
  # tools/autodebugger).  Note that this will disable inclusion of libraries. This
  # is useful because including dlmalloc makes it hard to compare native and js
  # builds
  if os.environ.get('EMCC_AUTODEBUG'):
    settings.AUTODEBUG = 1

  # Use settings

  if settings.DEBUG_LEVEL > 1 and options.use_closure_compiler:
    diagnostics.warning('emcc', 'disabling closure because debug info was requested')
    options.use_closure_compiler = False

  if settings.WASM == 2 and settings.SINGLE_FILE:
    exit_with_error('cannot have both WASM=2 and SINGLE_FILE enabled at the same time')

  if settings.SEPARATE_DWARF and settings.WASM2JS:
    exit_with_error('cannot have both SEPARATE_DWARF and WASM2JS at the same time (as there is no wasm file)')

  if settings.MINIMAL_RUNTIME_STREAMING_WASM_COMPILATION and settings.MINIMAL_RUNTIME_STREAMING_WASM_INSTANTIATION:
    exit_with_error('MINIMAL_RUNTIME_STREAMING_WASM_COMPILATION and MINIMAL_RUNTIME_STREAMING_WASM_INSTANTIATION are mutually exclusive!')

  if options.emrun:
    if settings.MINIMAL_RUNTIME:
      exit_with_error('--emrun is not compatible with MINIMAL_RUNTIME')

  if options.use_closure_compiler:
    settings.USE_CLOSURE_COMPILER = 1

  if 'CLOSURE_WARNINGS' in user_settings:
    if settings.CLOSURE_WARNINGS not in ['quiet', 'warn', 'error']:
      exit_with_error('Invalid option -sCLOSURE_WARNINGS=%s specified! Allowed values are "quiet", "warn" or "error".' % settings.CLOSURE_WARNINGS)

    diagnostics.warning('deprecated', 'CLOSURE_WARNINGS is deprecated, use -Wclosure/-Wno-closure instread')
    closure_warnings = diagnostics.manager.warnings['closure']
    if settings.CLOSURE_WARNINGS == 'error':
      closure_warnings['error'] = True
      closure_warnings['enabled'] = True
    elif settings.CLOSURE_WARNINGS == 'warn':
      closure_warnings['error'] = False
      closure_warnings['enabled'] = True
    elif settings.CLOSURE_WARNINGS == 'quiet':
      closure_warnings['error'] = False
      closure_warnings['enabled'] = False

  if not settings.MINIMAL_RUNTIME:
    if not settings.BOOTSTRAPPING_STRUCT_INFO:
      if settings.DYNCALLS:
        # Include dynCall() function by default in DYNCALLS builds in classic runtime; in MINIMAL_RUNTIME, must add this explicitly.
        settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$dynCall']

      if settings.ASSERTIONS:
        # "checkUnflushedContent()" and "missingLibrarySymbol()" depend on warnOnce
        settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$warnOnce']

      settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$getValue', '$setValue']

    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$ExitStatus']

  if not settings.BOOTSTRAPPING_STRUCT_INFO and settings.SAFE_HEAP:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$getValue_safe', '$setValue_safe']

  if settings.MAIN_MODULE:
    assert not settings.SIDE_MODULE
    if settings.MAIN_MODULE == 1:
      settings.INCLUDE_FULL_LIBRARY = 1
    # Called from preamble.js once the main module is instantiated.
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$loadDylibs']
    settings.REQUIRED_EXPORTS += ['malloc']

  if settings.MAIN_MODULE == 1 or settings.SIDE_MODULE == 1:
    settings.LINKABLE = 1

  if settings.LINKABLE and settings.USER_EXPORTED_FUNCTIONS:
    diagnostics.warning('unused-command-line-argument', 'EXPORTED_FUNCTIONS is not valid with LINKABLE set (normally due to SIDE_MODULE=1/MAIN_MODULE=1) since all functions are exported this mode.  To export only a subset use SIDE_MODULE=2/MAIN_MODULE=2')

  if settings.MAIN_MODULE:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += [
      '$getDylinkMetadata',
      '$mergeLibSymbols',
    ]

  if settings.PTHREADS:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += [
      '$registerTLSInit',
    ]

  if settings.RELOCATABLE:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += [
      '$reportUndefinedSymbols',
      '$relocateExports',
      '$GOTHandler',
      '__heap_base',
      '__stack_pointer',
    ]

    if settings.ASYNCIFY == 1:
      settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += [
        '__asyncify_state',
        '__asyncify_data'
      ]

    # Emscripten EH dependency in library_dylink.js
    if settings.SUPPORT_LONGJMP == 'emscripten' or not settings.DISABLE_EXCEPTION_CATCHING:
      settings.REQUIRED_EXPORTS += ['setThrew']

    if settings.MINIMAL_RUNTIME:
      exit_with_error('MINIMAL_RUNTIME is not compatible with relocatable output')
    if settings.WASM2JS:
      exit_with_error('WASM2JS is not compatible with relocatable output')
    # shared modules need memory utilities to allocate their memory
    settings.ALLOW_TABLE_GROWTH = 1

  # various settings require sbrk() access
  if settings.DETERMINISTIC or \
     settings.EMSCRIPTEN_TRACING or \
     settings.SAFE_HEAP or \
     settings.MEMORYPROFILER:
    settings.REQUIRED_EXPORTS += ['sbrk']

  if settings.MEMORYPROFILER:
    settings.REQUIRED_EXPORTS += ['__heap_base',
                                  'emscripten_stack_get_base',
                                  'emscripten_stack_get_end',
                                  'emscripten_stack_get_current']

  if settings.ASYNCIFY_LAZY_LOAD_CODE:
    settings.ASYNCIFY = 1

  if settings.ASYNCIFY == 1:
    # See: https://github.com/emscripten-core/emscripten/issues/12065
    # See: https://github.com/emscripten-core/emscripten/issues/12066
    settings.DYNCALLS = 1
    settings.REQUIRED_EXPORTS += ['emscripten_stack_get_base',
                                  'emscripten_stack_get_end',
                                  'emscripten_stack_set_limits']

  settings.ASYNCIFY_ADD = unmangle_symbols_from_cmdline(settings.ASYNCIFY_ADD)
  settings.ASYNCIFY_REMOVE = unmangle_symbols_from_cmdline(settings.ASYNCIFY_REMOVE)
  settings.ASYNCIFY_ONLY = unmangle_symbols_from_cmdline(settings.ASYNCIFY_ONLY)

  if settings.EMULATE_FUNCTION_POINTER_CASTS:
    # Emulated casts forces a wasm ABI of (i64, i64, ...) in the table, which
    # means all table functions are illegal for JS to call directly. Use
    # dyncalls which call into the wasm, which then does an indirect call.
    settings.DYNCALLS = 1

  if options.oformat != OFormat.OBJECT and final_suffix in ('.o', '.bc', '.so', '.dylib') and not settings.SIDE_MODULE:
    diagnostics.warning('emcc', 'object file output extension (%s) used for non-object output.  If you meant to build an object file please use `-c, `-r`, or `-shared`' % final_suffix)

  if settings.SUPPORT_BIG_ENDIAN:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += [
      '$LE_HEAP_STORE_U16',
      '$LE_HEAP_STORE_I16',
      '$LE_HEAP_STORE_U32',
      '$LE_HEAP_STORE_I32',
      '$LE_HEAP_STORE_F32',
      '$LE_HEAP_STORE_F64',
      '$LE_HEAP_LOAD_U16',
      '$LE_HEAP_LOAD_I16',
      '$LE_HEAP_LOAD_U32',
      '$LE_HEAP_LOAD_I32',
      '$LE_HEAP_LOAD_F32',
      '$LE_HEAP_LOAD_F64'
    ]

  if settings.RUNTIME_DEBUG or settings.ASSERTIONS or settings.STACK_OVERFLOW_CHECK or settings.PTHREADS_PROFILING or settings.GL_ASSERTIONS:
    # Lots of code in debug/assertion blocks uses ptrToString.
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$ptrToString']

  if settings.STACK_OVERFLOW_CHECK:
    settings.REQUIRED_EXPORTS += [
      'emscripten_stack_get_end',
      'emscripten_stack_get_free',
      'emscripten_stack_get_base',
      'emscripten_stack_get_current',
    ]

    # We call one of these two functions during startup which caches the stack limits
    # in wasm globals allowing get_base/get_free to be super fast.
    # See compiler-rt/stack_limits.S.
    if settings.RELOCATABLE:
      settings.REQUIRED_EXPORTS += ['emscripten_stack_set_limits']
    else:
      settings.REQUIRED_EXPORTS += ['emscripten_stack_init']

  if settings.STACK_OVERFLOW_CHECK >= 2:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$setStackLimits']

  if settings.MODULARIZE:
    if settings.PROXY_TO_WORKER:
      exit_with_error('-sMODULARIZE is not compatible with --proxy-to-worker (if you want to run in a worker with -sMODULARIZE, you likely want to do the worker side setup manually)')
    # in MINIMAL_RUNTIME we may not need to emit the Promise code, as the
    # HTML output creates a singleton instance, and it does so without the
    # Promise. However, in Pthreads mode the Promise is used for worker
    # creation.
    if settings.MINIMAL_RUNTIME and options.oformat == OFormat.HTML and not settings.PTHREADS:
      settings.EXPORT_READY_PROMISE = 0

  if settings.LEGACY_VM_SUPPORT:
    if settings.WASM2JS:
      settings.POLYFILL_OLD_MATH_FUNCTIONS = 1

    # Support all old browser versions
    settings.MIN_FIREFOX_VERSION = 0
    settings.MIN_SAFARI_VERSION = 0
    settings.MIN_IE_VERSION = 0
    settings.MIN_EDGE_VERSION = 0
    settings.MIN_CHROME_VERSION = 0
    settings.MIN_NODE_VERSION = 0

  if settings.MIN_CHROME_VERSION <= 37:
    settings.WORKAROUND_OLD_WEBGL_UNIFORM_UPLOAD_IGNORED_OFFSET_BUG = 1

  # 10.19.0 is the oldest version of node that we do any testing with.
  # Keep this in sync with the test-node-compat in .circleci/config.yml
  # and MINIMUM_NODE_VERSION in tools/shared.py
  if settings.MIN_NODE_VERSION:
    if settings.MIN_NODE_VERSION < 101900:
      exit_with_error('targeting node older than 10.19.00 is not supported')
    if settings.MIN_NODE_VERSION >= 150000:
      default_setting('NODEJS_CATCH_REJECTION', 0)

  # Do not catch rejections or exits in modularize mode, as these options
  # are for use when running emscripten modules standalone
  # see https://github.com/emscripten-core/emscripten/issues/18723#issuecomment-1429236996
  if settings.MODULARIZE:
    default_setting('NODEJS_CATCH_REJECTION', 0)
    default_setting('NODEJS_CATCH_EXIT', 0)
    if settings.NODEJS_CATCH_REJECTION or settings.NODEJS_CATCH_EXIT:
      exit_with_error('Cannot use -sNODEJS_CATCH_REJECTION or -sNODEJS_CATCH_EXIT with -sMODULARIZE')

  setup_environment_settings()

  if options.use_closure_compiler != 0:
    # Emscripten requires certain ES6 constructs by default in library code
    # - https://caniuse.com/let              : EDGE:12 FF:44 CHROME:49 SAFARI:11
    # - https://caniuse.com/const            : EDGE:12 FF:36 CHROME:49 SAFARI:11
    # - https://caniuse.com/arrow-functions: : EDGE:12 FF:22 CHROME:45 SAFARI:10
    # - https://caniuse.com/mdn-javascript_builtins_object_assign:
    #                                          EDGE:12 FF:34 CHROME:45 SAFARI:9
    # Taking the highest requirements gives is our minimum:
    #                             Max Version: EDGE:12 FF:44 CHROME:49 SAFARI:11
    settings.TRANSPILE_TO_ES5 = (settings.MIN_EDGE_VERSION < 12 or
                                 settings.MIN_FIREFOX_VERSION < 44 or
                                 settings.MIN_CHROME_VERSION < 49 or
                                 settings.MIN_SAFARI_VERSION < 110000 or
                                 settings.MIN_IE_VERSION != 0x7FFFFFFF)

    if options.use_closure_compiler is None and settings.TRANSPILE_TO_ES5:
      diagnostics.warning('transpile', 'enabling transpilation via closure due to browser version settings.  This warning can be suppressed by passing `--closure=1` or `--closure=0` to opt into our explicitly.')

  # https://caniuse.com/class: EDGE:13 FF:45 CHROME:49 SAFARI:9
  supports_es6_classes = (settings.MIN_EDGE_VERSION >= 13 and
                          settings.MIN_FIREFOX_VERSION >= 45 and
                          settings.MIN_CHROME_VERSION >= 49 and
                          settings.MIN_SAFARI_VERSION >= 90000 and
                          settings.MIN_IE_VERSION == 0x7FFFFFFF)

  if not settings.DISABLE_EXCEPTION_CATCHING and settings.EXCEPTION_STACK_TRACES and not supports_es6_classes:
    diagnostics.warning('transpile', '-sEXCEPTION_STACK_TRACES requires an engine that support ES6 classes.')
    settings.EXCEPTION_STACK_TRACES = 0

  # Silently drop any individual backwards compatibility emulation flags that are known never to occur on browsers that support WebAssembly.
  if not settings.WASM2JS:
    settings.POLYFILL_OLD_MATH_FUNCTIONS = 0
    settings.WORKAROUND_OLD_WEBGL_UNIFORM_UPLOAD_IGNORED_OFFSET_BUG = 0

  if settings.STB_IMAGE and final_suffix in EXECUTABLE_ENDINGS:
    state.forced_stdlibs.append('libstb_image')
    settings.EXPORTED_FUNCTIONS += ['_stbi_load', '_stbi_load_from_memory', '_stbi_image_free']

  if settings.USE_WEBGL2:
    settings.MAX_WEBGL_VERSION = 2

  # MIN_WEBGL_VERSION=2 implies MAX_WEBGL_VERSION=2
  if settings.MIN_WEBGL_VERSION == 2:
    default_setting('MAX_WEBGL_VERSION', 2)

  if settings.MIN_WEBGL_VERSION > settings.MAX_WEBGL_VERSION:
    exit_with_error('MIN_WEBGL_VERSION must be smaller or equal to MAX_WEBGL_VERSION!')

  if not settings.GL_SUPPORT_SIMPLE_ENABLE_EXTENSIONS and settings.GL_SUPPORT_AUTOMATIC_ENABLE_EXTENSIONS:
    exit_with_error('-sGL_SUPPORT_SIMPLE_ENABLE_EXTENSIONS=0 only makes sense with -sGL_SUPPORT_AUTOMATIC_ENABLE_EXTENSIONS=0!')

  if options.use_preload_plugins or len(options.preload_files) or len(options.embed_files):
    if settings.NODERAWFS:
      exit_with_error('--preload-file and --embed-file cannot be used with NODERAWFS which disables virtual filesystem')
    # if we include any files, or intend to use preload plugins, then we definitely need filesystem support
    settings.FORCE_FILESYSTEM = 1

  if settings.WASMFS:
    if settings.NODERAWFS:
      # wasmfs will be included normally in system_libs.py, but we must include
      # noderawfs in a forced manner so that it is always linked in (the hook it
      # implements can remain unimplemented, so it won't be linked in
      # automatically)
      # TODO: find a better way to do this
      state.forced_stdlibs.append('libwasmfs_noderawfs')
    settings.FILESYSTEM = 1
    settings.SYSCALLS_REQUIRE_FILESYSTEM = 0
    settings.JS_LIBRARIES.append((0, 'library_wasmfs.js'))
    if settings.ASSERTIONS:
      # used in assertion checks for unflushed content
      settings.REQUIRED_EXPORTS += ['wasmfs_flush']
    if settings.FORCE_FILESYSTEM or settings.INCLUDE_FULL_LIBRARY:
      # Add exports for the JS API. Like the old JS FS, WasmFS by default
      # includes just what JS parts it actually needs, and FORCE_FILESYSTEM is
      # required to force all of it to be included if the user wants to use the
      # JS API directly. (INCLUDE_FULL_LIBRARY also causes this code to be
      # included, as the entire JS library can refer to things that require
      # these exports.)
      settings.REQUIRED_EXPORTS += [
        '_wasmfs_read_file',
        '_wasmfs_write_file',
        '_wasmfs_open',
        '_wasmfs_allocate',
        '_wasmfs_close',
        '_wasmfs_write',
        '_wasmfs_pwrite',
        '_wasmfs_rename',
        '_wasmfs_mkdir',
        '_wasmfs_unlink',
        '_wasmfs_chdir',
        '_wasmfs_mknod',
        '_wasmfs_rmdir',
        '_wasmfs_read',
        '_wasmfs_pread',
        '_wasmfs_symlink',
        '_wasmfs_truncate',
        '_wasmfs_ftruncate',
        '_wasmfs_stat',
        '_wasmfs_lstat',
        '_wasmfs_chmod',
        '_wasmfs_fchmod',
        '_wasmfs_lchmod',
        '_wasmfs_utime',
        '_wasmfs_llseek',
        '_wasmfs_identify',
        '_wasmfs_readlink',
        '_wasmfs_readdir_start',
        '_wasmfs_readdir_get',
        '_wasmfs_readdir_finish',
        '_wasmfs_get_cwd',
      ]

  if settings.FETCH and final_suffix in EXECUTABLE_ENDINGS:
    state.forced_stdlibs.append('libfetch')
    settings.JS_LIBRARIES.append((0, 'library_fetch.js'))
    if settings.PTHREADS:
      settings.FETCH_WORKER_FILE = unsuffixed_basename(target) + '.fetch.js'

  if settings.DEMANGLE_SUPPORT:
    settings.REQUIRED_EXPORTS += ['__cxa_demangle', 'free']
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$demangle', '$stackTrace']

  if settings.FULL_ES3:
    settings.FULL_ES2 = 1
    settings.MAX_WEBGL_VERSION = max(2, settings.MAX_WEBGL_VERSION)

  # WASM_SYSTEM_EXPORTS are actually native function but they are allowed to be exported
  # via EXPORTED_RUNTIME_METHODS for backwards compat.
  for sym in settings.WASM_SYSTEM_EXPORTS:
    if sym in settings.EXPORTED_RUNTIME_METHODS:
      settings.REQUIRED_EXPORTS.append(sym)

  settings.REQUIRED_EXPORTS += ['stackSave', 'stackRestore', 'stackAlloc']

  if settings.RELOCATABLE:
    # TODO(https://reviews.llvm.org/D128515): Make this mandatory once
    # llvm change lands
    settings.EXPORT_IF_DEFINED.append('__wasm_apply_data_relocs')

  if settings.SIDE_MODULE and 'GLOBAL_BASE' in user_settings:
    exit_with_error('GLOBAL_BASE is not compatible with SIDE_MODULE')

  if settings.PROXY_TO_WORKER or options.use_preload_plugins:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$Browser']

  if not settings.BOOTSTRAPPING_STRUCT_INFO:
    if settings.DYNAMIC_EXECUTION == 2 and not settings.MINIMAL_RUNTIME:
      # Used by makeEval in the DYNAMIC_EXECUTION == 2 case
      settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$stackTrace']

    if not settings.STANDALONE_WASM and (settings.EXIT_RUNTIME or settings.ASSERTIONS):
      # to flush streams on FS exit, we need to be able to call fflush
      # we only include it if the runtime is exitable, or when ASSERTIONS
      # (ASSERTIONS will check that streams do not need to be flushed,
      # helping people see when they should have enabled EXIT_RUNTIME)
      settings.EXPORT_IF_DEFINED += ['fflush']

    if settings.SUPPORT_ERRNO:
      # so setErrNo JS library function can report errno back to C
      settings.REQUIRED_EXPORTS += ['__errno_location']

  if settings.SAFE_HEAP:
    # SAFE_HEAP check includes calling emscripten_get_sbrk_ptr() from wasm
    settings.REQUIRED_EXPORTS += ['emscripten_get_sbrk_ptr', 'emscripten_stack_get_base']
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$unSign']

  if not settings.DECLARE_ASM_MODULE_EXPORTS:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$exportAsmFunctions']

  if settings.ALLOW_MEMORY_GROWTH:
    # Setting ALLOW_MEMORY_GROWTH turns off ABORTING_MALLOC, as in that mode we default to
    # the behavior of trying to grow and returning 0 from malloc on failure, like
    # a standard system would. However, if the user sets the flag it
    # overrides that.
    default_setting('ABORTING_MALLOC', 0)

  if settings.PTHREADS:
    setup_pthreads(target)
    settings.JS_LIBRARIES.append((0, 'library_pthread.js'))
    if settings.PROXY_TO_PTHREAD:
      settings.PTHREAD_POOL_SIZE_STRICT = 0
      settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$runtimeKeepalivePush']
  else:
    if settings.PROXY_TO_PTHREAD:
      exit_with_error('-sPROXY_TO_PTHREAD requires -pthread to work!')
    settings.JS_LIBRARIES.append((0, 'library_pthread_stub.js'))

  if settings.MEMORY64:
    if settings.ASYNCIFY == 1 and settings.MEMORY64 == 1:
      exit_with_error('MEMORY64=1 is not compatible with ASYNCIFY')
    # Any "pointers" passed to JS will now be i64's, in both modes.
    settings.WASM_BIGINT = 1

  if settings.WASM_WORKERS:
    # TODO: After #15982 is resolved, these dependencies can be declared in library_wasm_worker.js
    #       instead of having to record them here.
    wasm_worker_imports = ['_emscripten_wasm_worker_initialize', '___set_thread_state']
    settings.EXPORTED_FUNCTIONS += wasm_worker_imports
    building.user_requested_exports.update(wasm_worker_imports)
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$_wasmWorkerInitializeRuntime']
    # set location of Wasm Worker bootstrap JS file
    if settings.WASM_WORKERS == 1:
      settings.WASM_WORKER_FILE = unsuffixed(os.path.basename(target)) + '.ww.js'
    settings.JS_LIBRARIES.append((0, shared.path_from_root('src', 'library_wasm_worker.js')))

  # Set min browser versions based on certain settings such as WASM_BIGINT,
  # PTHREADS, AUDIO_WORKLET
  # Such setting must be set before this point
  feature_matrix.apply_min_browser_versions()

  # TODO(sbc): Find make a generic way to expose the feature matrix to JS
  # compiler rather then adding them all ad-hoc as internal settings
  settings.SUPPORTS_GLOBALTHIS = feature_matrix.caniuse(feature_matrix.Feature.GLOBALTHIS)
  settings.SUPPORTS_PROMISE_ANY = feature_matrix.caniuse(feature_matrix.Feature.PROMISE_ANY)
  if not settings.BULK_MEMORY:
    settings.BULK_MEMORY = feature_matrix.caniuse(feature_matrix.Feature.BULK_MEMORY)

  if settings.AUDIO_WORKLET:
    if settings.AUDIO_WORKLET == 1:
      settings.AUDIO_WORKLET_FILE = unsuffixed(os.path.basename(target)) + '.aw.js'
    settings.JS_LIBRARIES.append((0, shared.path_from_root('src', 'library_webaudio.js')))
    if not settings.MINIMAL_RUNTIME:
      # MINIMAL_RUNTIME exports these manually, since this export mechanism is placed
      # in global scope that is not suitable for MINIMAL_RUNTIME loader.
      settings.EXPORTED_RUNTIME_METHODS += ['stackSave', 'stackAlloc', 'stackRestore']

  if settings.FORCE_FILESYSTEM and not settings.MINIMAL_RUNTIME:
    # when the filesystem is forced, we export by default methods that filesystem usage
    # may need, including filesystem usage from standalone file packager output (i.e.
    # file packages not built together with emcc, but that are loaded at runtime
    # separately, and they need emcc's output to contain the support they need)
    settings.EXPORTED_RUNTIME_METHODS += [
      'FS_createPath',
      'FS_createDataFile',
      'FS_createPreloadedFile',
      'FS_unlink'
    ]
    if not settings.WASMFS:
      # The old FS has some functionality that WasmFS lacks.
      settings.EXPORTED_RUNTIME_METHODS += [
        'FS_createLazyFile',
        'FS_createDevice'
      ]

    settings.EXPORTED_RUNTIME_METHODS += [
      'addRunDependency',
      'removeRunDependency',
    ]

  if options.embind_emit_tsd:
    # TODO: Remove after #19759 is resolved.
    settings.REQUIRED_EXPORTS += ['free']

  def check_memory_setting(setting):
    if settings[setting] % webassembly.WASM_PAGE_SIZE != 0:
      exit_with_error(f'{setting} must be a multiple of WebAssembly page size (64KiB), was {settings[setting]}')
    if settings[setting] >= 2**53:
      exit_with_error(f'{setting} must be smaller than 2^53 bytes due to JS Numbers (doubles) being used to hold pointer addresses in JS side')

  check_memory_setting('INITIAL_MEMORY')
  check_memory_setting('MAXIMUM_MEMORY')
  if settings.INITIAL_MEMORY < settings.STACK_SIZE:
    exit_with_error(f'INITIAL_MEMORY must be larger than STACK_SIZE, was {settings.INITIAL_MEMORY} (STACK_SIZE={settings.STACK_SIZE})')
  if settings.MEMORY_GROWTH_LINEAR_STEP != -1:
    check_memory_setting('MEMORY_GROWTH_LINEAR_STEP')

  if settings.ALLOW_MEMORY_GROWTH and settings.MAXIMUM_MEMORY < settings.INITIAL_MEMORY:
    exit_with_error('MAXIMUM_MEMORY must be larger then INITIAL_MEMORY')

  if 'MAXIMUM_MEMORY' in user_settings and not settings.ALLOW_MEMORY_GROWTH:
    diagnostics.warning('unused-command-line-argument', 'MAXIMUM_MEMORY is only meaningful with ALLOW_MEMORY_GROWTH')

  if settings.EXPORT_ES6:
    if not settings.MODULARIZE:
      # EXPORT_ES6 requires output to be a module
      if 'MODULARIZE' in user_settings:
        exit_with_error('EXPORT_ES6 requires MODULARIZE to be set')
      settings.MODULARIZE = 1
    if shared.target_environment_may_be('node') and not settings.USE_ES6_IMPORT_META:
      # EXPORT_ES6 + ENVIRONMENT=*node* requires the use of import.meta.url
      if 'USE_ES6_IMPORT_META' in user_settings:
        exit_with_error('EXPORT_ES6 and ENVIRONMENT=*node* requires USE_ES6_IMPORT_META to be set')
      settings.USE_ES6_IMPORT_META = 1

  if settings.MODULARIZE and not settings.DECLARE_ASM_MODULE_EXPORTS:
    # When MODULARIZE option is used, currently requires declaring all module exports
    # individually - TODO: this could be optimized
    exit_with_error('DECLARE_ASM_MODULE_EXPORTS=0 is not compatible with MODULARIZE')

  # When not declaring wasm module exports in outer scope one by one, disable minifying
  # wasm module export names so that the names can be passed directly to the outer scope.
  # Also, if using library_exports.js API, disable minification so that the feature can work.
  if not settings.DECLARE_ASM_MODULE_EXPORTS or '-lexports.js' in [x for _, x in state.link_flags]:
    settings.MINIFY_WASM_EXPORT_NAMES = 0

  if '-lembind' in [x for _, x in state.link_flags]:
    settings.EMBIND = 1

  # Enable minification of wasm imports and exports when appropriate, if we
  # are emitting an optimized JS+wasm combo (then the JS knows how to load the minified names).
  # Things that process the JS after this operation would be done must disable this.
  # For example, ASYNCIFY_LAZY_LOAD_CODE needs to identify import names.
  # ASYNCIFY=2 does not support this optimization yet as it has a hardcoded
  # check for 'main' as an export name. TODO
  if will_metadce() and \
      settings.OPT_LEVEL >= 2 and \
      settings.DEBUG_LEVEL <= 2 and \
      options.oformat not in (OFormat.WASM, OFormat.BARE) and \
      settings.ASYNCIFY != 2 and \
      not settings.LINKABLE and \
      not settings.STANDALONE_WASM and \
      not settings.AUTODEBUG and \
      not settings.ASSERTIONS and \
      not settings.RELOCATABLE and \
      not settings.ASYNCIFY_LAZY_LOAD_CODE and \
          settings.MINIFY_WASM_EXPORT_NAMES:
    settings.MINIFY_WASM_IMPORTS_AND_EXPORTS = 1
    settings.MINIFY_WASM_IMPORTED_MODULES = 1

  if settings.MINIMAL_RUNTIME:
    # Minimal runtime uses a different default shell file
    if options.shell_path == utils.path_from_root('src/shell.html'):
      options.shell_path = utils.path_from_root('src/shell_minimal_runtime.html')

    if settings.ASSERTIONS:
      # In ASSERTIONS-builds, functions UTF8ArrayToString() and stringToUTF8Array() (which are not JS library functions), both
      # use warnOnce(), which in MINIMAL_RUNTIME is a JS library function, so explicitly have to mark dependency to warnOnce()
      # in that case. If string functions are turned to library functions in the future, then JS dependency tracking can be
      # used and this special directive can be dropped.
      settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$warnOnce']

  if settings.MODULARIZE and not (settings.EXPORT_ES6 and not settings.SINGLE_FILE) and \
     settings.EXPORT_NAME == 'Module' and options.oformat == OFormat.HTML and \
     (options.shell_path == utils.path_from_root('src/shell.html') or options.shell_path == utils.path_from_root('src/shell_minimal.html')):
    exit_with_error(f'Due to collision in variable name "Module", the shell file "{options.shell_path}" is not compatible with build options "-sMODULARIZE -sEXPORT_NAME=Module". Either provide your own shell file, change the name of the export to something else to avoid the name collision. (see https://github.com/emscripten-core/emscripten/issues/7950 for details)')

  # TODO(sbc): Remove WASM2JS here once the size regression it would introduce has been fixed.
  if settings.SHARED_MEMORY or settings.RELOCATABLE or settings.ASYNCIFY_LAZY_LOAD_CODE or settings.WASM2JS:
    settings.IMPORTED_MEMORY = 1

  if settings.WASM_BIGINT:
    settings.LEGALIZE_JS_FFI = 0

  if settings.SINGLE_FILE:
    settings.GENERATE_SOURCE_MAP = 0

  if settings.EVAL_CTORS:
    if settings.WASM2JS:
      # code size/memory and correctness issues TODO
      exit_with_error('EVAL_CTORS is not compatible with wasm2js yet')
    elif settings.RELOCATABLE:
      exit_with_error('EVAL_CTORS is not compatible with relocatable yet (movable segments)')
    elif settings.ASYNCIFY:
      # In Asyncify exports can be called more than once, and this seems to not
      # work properly yet (see test_emscripten_scan_registers).
      exit_with_error('EVAL_CTORS is not compatible with asyncify yet')

  if options.use_closure_compiler == 2 and not settings.WASM2JS:
    exit_with_error('closure compiler mode 2 assumes the code is asm.js, so not meaningful for wasm')

  if settings.WASM2JS:
    if options.memory_init_file is None:
      options.memory_init_file = settings.OPT_LEVEL >= 2
    settings.MAYBE_WASM2JS = 1
    # when using wasm2js, if the memory segments are in the wasm then they
    # end up converted by wasm2js into base64 encoded JS. alternatively, we
    # can use a .mem file like asm.js used to.
    # generally we follow what the options tell us to do (which is to use
    # a .mem file in most cases, since it is binary & compact). however, for
    # shared memory builds we must keep the memory segments in the wasm as
    # they will be passive segments which the .mem format cannot handle.
    settings.MEM_INIT_IN_WASM = not options.memory_init_file or settings.SINGLE_FILE or settings.SHARED_MEMORY
  elif options.memory_init_file:
    diagnostics.warning('unsupported', '--memory-init-file is only supported with -sWASM=0')

  if settings.AUTODEBUG:
    settings.REQUIRED_EXPORTS += ['setTempRet0']

  if settings.LEGALIZE_JS_FFI:
    settings.REQUIRED_EXPORTS += ['__get_temp_ret', '__set_temp_ret']

  if settings.SPLIT_MODULE and settings.ASYNCIFY == 2:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['_load_secondary_module']

  # wasm side modules have suffix .wasm
  if settings.SIDE_MODULE and shared.suffix(target) == '.js':
    diagnostics.warning('emcc', 'output suffix .js requested, but wasm side modules are just wasm files; emitting only a .wasm, no .js')

  sanitize = set()

  for arg in newargs:
    if arg.startswith('-fsanitize='):
      sanitize.update(arg.split('=', 1)[1].split(','))
    elif arg.startswith('-fno-sanitize='):
      sanitize.difference_update(arg.split('=', 1)[1].split(','))

  if sanitize:
    settings.USE_OFFSET_CONVERTER = 1
    settings.REQUIRED_EXPORTS += [
        'memalign',
        'emscripten_builtin_memalign',
        'emscripten_builtin_malloc',
        'emscripten_builtin_free',
    ]

  if ('leak' in sanitize or 'address' in sanitize) and not settings.ALLOW_MEMORY_GROWTH:
    # Increase the minimum memory requirements to account for extra memory
    # that the sanitizers might need (in addition to the shadow memory
    # requirements handled below).
    # These values are designed be an over-estimate of the actual requirements and
    # are based on experimentation with different tests/programs under asan and
    # lsan.
    settings.INITIAL_MEMORY += 50 * 1024 * 1024
    if settings.PTHREADS:
      settings.INITIAL_MEMORY += 50 * 1024 * 1024

  if settings.USE_OFFSET_CONVERTER:
    if settings.WASM2JS:
      exit_with_error('wasm2js is not compatible with USE_OFFSET_CONVERTER (see #14630)')
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE.append('$UTF8ArrayToString')

  if sanitize & UBSAN_SANITIZERS:
    if '-fsanitize-minimal-runtime' in newargs:
      settings.UBSAN_RUNTIME = 1
    else:
      settings.UBSAN_RUNTIME = 2

  if 'leak' in sanitize:
    settings.USE_LSAN = 1
    default_setting('EXIT_RUNTIME', 1)

  if 'address' in sanitize:
    settings.USE_ASAN = 1
    default_setting('EXIT_RUNTIME', 1)
    if not settings.UBSAN_RUNTIME:
      settings.UBSAN_RUNTIME = 2

    # helper functions for JS to call into C to do memory operations. these
    # let us sanitize memory access from the JS side, by calling into C where
    # it has been instrumented.
    ASAN_C_HELPERS = [
      '_asan_c_load_1', '_asan_c_load_1u',
      '_asan_c_load_2', '_asan_c_load_2u',
      '_asan_c_load_4', '_asan_c_load_4u',
      '_asan_c_load_f', '_asan_c_load_d',
      '_asan_c_store_1', '_asan_c_store_1u',
      '_asan_c_store_2', '_asan_c_store_2u',
      '_asan_c_store_4', '_asan_c_store_4u',
      '_asan_c_store_f', '_asan_c_store_d',
    ]

    settings.REQUIRED_EXPORTS += ASAN_C_HELPERS

    if settings.ASYNCIFY and not settings.ASYNCIFY_ONLY:
      # we do not want asyncify to instrument these helpers - they just access
      # memory as small getters/setters, so they cannot pause anyhow, and also
      # we access them in the runtime as we prepare to rewind, which would hit
      # an asyncify assertion, if asyncify instrumented them.
      #
      # note that if ASYNCIFY_ONLY was set by the user then we do not need to
      # do anything (as the user's list won't contain these functions), and if
      # we did add them, the pass would assert on incompatible lists, hence the
      # condition in the above if.
      settings.ASYNCIFY_REMOVE += ASAN_C_HELPERS

    if settings.ASAN_SHADOW_SIZE != -1:
      diagnostics.warning('emcc', 'ASAN_SHADOW_SIZE is ignored and will be removed in a future release')

    if 'GLOBAL_BASE' in user_settings:
      exit_with_error("ASan does not support custom GLOBAL_BASE")

    # Increase the TOTAL_MEMORY and shift GLOBAL_BASE to account for
    # the ASan shadow region which starts at address zero.
    # The shadow region is 1/8th the size of the total memory and is
    # itself part of the total memory.
    # We use the following variables in this calculation:
    # - user_mem : memory usable/visible by the user program.
    # - shadow_size : memory used by asan for shadow memory.
    # - total_mem : the sum of the above. this is the size of the wasm memory (and must be aligned to WASM_PAGE_SIZE)
    user_mem = settings.INITIAL_MEMORY
    if settings.ALLOW_MEMORY_GROWTH:
      user_mem = settings.MAXIMUM_MEMORY

    # Given the know value of user memory size we can work backwards
    # to find the total memory and the shadow size based on the fact
    # that the user memory is 7/8ths of the total memory.
    # (i.e. user_mem == total_mem * 7 / 8
    total_mem = user_mem * 8 / 7

    # But we might need to re-align to wasm page size
    total_mem = int(align_to_wasm_page_boundary(total_mem))

    # The shadow size is 1/8th the resulting rounded up size
    shadow_size = total_mem // 8

    # We start our global data after the shadow memory.
    # We don't need to worry about alignment here.  wasm-ld will take care of that.
    settings.GLOBAL_BASE = shadow_size
    settings.STACK_FIRST = False

    if not settings.ALLOW_MEMORY_GROWTH:
      settings.INITIAL_MEMORY = total_mem
    else:
      settings.INITIAL_MEMORY += align_to_wasm_page_boundary(shadow_size)

    if settings.SAFE_HEAP:
      # SAFE_HEAP instruments ASan's shadow memory accesses.
      # Since the shadow memory starts at 0, the act of accessing the shadow memory is detected
      # by SAFE_HEAP as a null pointer dereference.
      exit_with_error('ASan does not work with SAFE_HEAP')

  if settings.USE_ASAN or settings.SAFE_HEAP:
    # ASan and SAFE_HEAP check address 0 themselves
    settings.CHECK_NULL_WRITES = 0

  if sanitize and settings.GENERATE_SOURCE_MAP:
    settings.LOAD_SOURCE_MAP = 1

  if settings.MINIMAL_RUNTIME:
    if settings.EXIT_RUNTIME:
      settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['proc_exit', '$callRuntimeCallbacks']
  else:
    # MINIMAL_RUNTIME only needs callRuntimeCallbacks in certain cases, but the normal runtime
    # always does.
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$callRuntimeCallbacks']

  if settings.EXIT_RUNTIME and not settings.STANDALONE_WASM:
    # Internal function implemented in musl that calls any functions registered
    # via `atexit` et al.  With STANDALONE_WASM this is all taken care of via
    # _start and exit handling in musl, but with the normal emscripten ABI we
    # need to be able to call these explicitly.
    settings.REQUIRED_EXPORTS += ['__funcs_on_exit']

  # Some settings require malloc/free to be exported explictly.
  # In most cases, the inclustion of native symbols like malloc and free
  # is taken care of by wasm-ld use its normal symbol resolution process.
  # However, when JS symbols are exported explictly via
  # DEFAULT_LIBRARY_FUNCS_TO_INCLUDE and they depend on native symbols
  # we need to explictly require those exports.
  if settings.BUILD_AS_WORKER or \
     settings.ASYNCIFY or \
     settings.WASMFS or \
     settings.FORCE_FILESYSTEM or \
     options.memory_profiler or \
     sanitize:
    settings.REQUIRED_EXPORTS += ['malloc', 'free']

  if not settings.DISABLE_EXCEPTION_CATCHING:
    settings.REQUIRED_EXPORTS += [
      # For normal builds the entries in deps_info.py are enough to include
      # these symbols whenever __cxa_find_matching_catch_* functions are
      # found.  However, under LTO these symbols don't exist prior to linking
      # so we include then unconditionally when exceptions are enabled.
      '__cxa_is_pointer_type',
      '__cxa_can_catch',

      # __cxa_begin_catch depends on this but we can't use deps info in this
      # case because that only works for user-level code, and __cxa_begin_catch
      # can be used by the standard library.
      '__cxa_increment_exception_refcount',
      # Same for __cxa_end_catch
      '__cxa_decrement_exception_refcount',

      # Emscripten exception handling can generate invoke calls, and they call
      # setThrew(). We cannot handle this using deps_info as the invokes are not
      # emitted because of library function usage, but by codegen itself.
      'setThrew',
      '__cxa_free_exception',
    ]

  if settings.ASYNCIFY:
    if not settings.ASYNCIFY_IGNORE_INDIRECT:
      # if we are not ignoring indirect calls, then we must treat invoke_* as if
      # they are indirect calls, since that is what they do - we can't see their
      # targets statically.
      settings.ASYNCIFY_IMPORTS += ['invoke_*']
    # add the default imports
    settings.ASYNCIFY_IMPORTS += DEFAULT_ASYNCIFY_IMPORTS
    # add the default exports (only used for ASYNCIFY == 2)
    settings.ASYNCIFY_EXPORTS += DEFAULT_ASYNCIFY_EXPORTS

    # return the full import name, including module. The name may
    # already have a module prefix; if not, we assume it is "env".
    def get_full_import_name(name):
      if '.' in name:
        return name
      return 'env.' + name

    settings.ASYNCIFY_IMPORTS = [get_full_import_name(i) for i in settings.ASYNCIFY_IMPORTS]

    if settings.ASYNCIFY == 2:
      diagnostics.warning('experimental', '-sASYNCIFY=2 (JSPI) is still experimental')

  if settings.WASM2JS:
    if settings.GENERATE_SOURCE_MAP:
      exit_with_error('wasm2js does not support source maps yet (debug in wasm for now)')
    if settings.WASM_BIGINT:
      exit_with_error('wasm2js does not support WASM_BIGINT')
    if settings.MEMORY64:
      exit_with_error('wasm2js does not support MEMORY64')

  if settings.NODE_CODE_CACHING:
    if settings.WASM_ASYNC_COMPILATION:
      exit_with_error('NODE_CODE_CACHING requires sync compilation (WASM_ASYNC_COMPILATION=0)')
    if not shared.target_environment_may_be('node'):
      exit_with_error('NODE_CODE_CACHING only works in node, but target environments do not include it')
    if settings.SINGLE_FILE:
      exit_with_error('NODE_CODE_CACHING saves a file on the side and is not compatible with SINGLE_FILE')

  if not js_manipulation.isidentifier(settings.EXPORT_NAME):
    exit_with_error(f'EXPORT_NAME is not a valid JS identifier: `{settings.EXPORT_NAME}`')

  if settings.EMSCRIPTEN_TRACING and settings.ALLOW_MEMORY_GROWTH:
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['emscripten_trace_report_memory_layout']
    settings.REQUIRED_EXPORTS += ['emscripten_stack_get_current',
                                  'emscripten_stack_get_base',
                                  'emscripten_stack_get_end']

  # check if we can address the 2GB mark and higher: either if we start at
  # 2GB, or if we allow growth to either any amount or to 2GB or more.
  if not settings.MEMORY64 and (settings.INITIAL_MEMORY > 2 * 1024 * 1024 * 1024 or
     (settings.ALLOW_MEMORY_GROWTH and
      (settings.MAXIMUM_MEMORY < 0 or
       settings.MAXIMUM_MEMORY > 2 * 1024 * 1024 * 1024))):
    settings.CAN_ADDRESS_2GB = 1

  settings.EMSCRIPTEN_VERSION = shared.EMSCRIPTEN_VERSION
  settings.SOURCE_MAP_BASE = options.source_map_base or ''

  settings.LINK_AS_CXX = (run_via_emxx or settings.DEFAULT_TO_CXX) and '-nostdlib++' not in newargs

  # WASMFS itself is written in C++, and needs C++ standard libraries
  if settings.WASMFS:
    settings.LINK_AS_CXX = True

  # Some settings make no sense when not linking as C++
  if not settings.LINK_AS_CXX:
    cxx_only_settings = [
      'DEMANGLE_SUPPORT',
      'EXCEPTION_DEBUG',
      'DISABLE_EXCEPTION_CATCHING',
      'EXCEPTION_CATCHING_ALLOWED',
      'DISABLE_EXCEPTION_THROWING',
    ]
    for setting in cxx_only_settings:
      if setting in user_settings:
        diagnostics.warning('linkflags', 'setting `%s` is not meaningful unless linking as C++', setting)

  if settings.WASM_EXCEPTIONS:
    settings.REQUIRED_EXPORTS += ['__trap']

  if settings.EXCEPTION_STACK_TRACES:
    # If the user explicitly gave EXCEPTION_STACK_TRACES=1 without enabling EH,
    # errors out.
    if settings.DISABLE_EXCEPTION_CATCHING and not settings.WASM_EXCEPTIONS:
      exit_with_error('EXCEPTION_STACK_TRACES requires either of -fexceptions or -fwasm-exceptions')
    # EXCEPTION_STACK_TRACES implies EXPORT_EXCEPTION_HANDLING_HELPERS
    settings.EXPORT_EXCEPTION_HANDLING_HELPERS = True

  # Make `getExceptionMessage` and other necessary functions available for use.
  if settings.EXPORT_EXCEPTION_HANDLING_HELPERS:
    # If the user explicitly gave EXPORT_EXCEPTION_HANDLING_HELPERS=1 without
    # enabling EH, errors out.
    if settings.DISABLE_EXCEPTION_CATCHING and not settings.WASM_EXCEPTIONS:
      exit_with_error('EXPORT_EXCEPTION_HANDLING_HELPERS requires either of -fexceptions or -fwasm-exceptions')
    # We also export refcount increasing and decreasing functions because if you
    # catch an exception, be it an Emscripten exception or a Wasm exception, in
    # JS, you may need to manipulate the refcount manually not to leak memory.
    # What you need to do is different depending on the kind of EH you use
    # (https://github.com/emscripten-core/emscripten/issues/17115).
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE += ['$getExceptionMessage', '$incrementExceptionRefcount', '$decrementExceptionRefcount']
    settings.EXPORTED_FUNCTIONS += ['getExceptionMessage', '___get_exception_message', '_free']
    if settings.WASM_EXCEPTIONS:
      settings.EXPORTED_FUNCTIONS += ['___cpp_exception', '___cxa_increment_exception_refcount', '___cxa_decrement_exception_refcount', '___thrown_object_from_unwind_exception']

  if settings.SIDE_MODULE:
    # For side modules, we ignore all REQUIRED_EXPORTS that might have been added above.
    # They all come from either libc or compiler-rt.  The exception is __wasm_call_ctors
    # which is a per-module export.
    settings.REQUIRED_EXPORTS.clear()

  if not settings.STANDALONE_WASM:
    # in standalone mode, crt1 will call the constructors from inside the wasm
    settings.REQUIRED_EXPORTS.append('__wasm_call_ctors')

  settings.PRE_JS_FILES = [os.path.abspath(f) for f in options.pre_js]
  settings.POST_JS_FILES = [os.path.abspath(f) for f in options.post_js]

  return target, wasm_target


@ToolchainProfiler.profile_block('compile inputs')
def phase_compile_inputs(options, state, newargs, input_files):
  def is_link_flag(flag):
    if flag in ('-nostdlib', '-nostartfiles', '-nolibc', '-nodefaultlibs'):
      return True
    return flag.startswith(('-l', '-L', '-Wl,'))

  CXX = [shared.CLANG_CXX]
  CC = [shared.CLANG_CC]
  if config.COMPILER_WRAPPER:
    logger.debug('using compiler wrapper: %s', config.COMPILER_WRAPPER)
    CXX.insert(0, config.COMPILER_WRAPPER)
    CC.insert(0, config.COMPILER_WRAPPER)

  compile_args = [a for a in newargs if a and not is_link_flag(a)]
  system_libs.ensure_sysroot()

  def get_language_mode(args):
    return_next = False
    for item in args:
      if return_next:
        return item
      if item == '-x':
        return_next = True
        continue
      if item.startswith('-x'):
        return removeprefix(item, '-x')
    return ''

  language_mode = get_language_mode(newargs)

  def use_cxx(src):
    if 'c++' in language_mode or run_via_emxx:
      return True
    suffix = shared.suffix(src)
    # Next consider the filename
    if suffix in C_ENDINGS + OBJC_ENDINGS:
      return False
    if suffix in CXX_ENDINGS:
      return True
    # Finally fall back to the default
    if settings.DEFAULT_TO_CXX:
      # Default to using C++ even when run as `emcc`.
      # This means that emcc will act as a C++ linker when no source files are
      # specified.
      # This differs to clang and gcc where the default is always C unless run as
      # clang++/g++.
      return True
    return False

  def get_compiler(src_file):
    if use_cxx(src_file):
      return CXX
    return CC

  def get_clang_command(src_file):
    return get_compiler(src_file) + get_cflags(state.orig_args, use_cxx(src_file)) + compile_args + [src_file]

  def get_clang_command_preprocessed(src_file):
    return get_compiler(src_file) + get_clang_flags(state.orig_args) + compile_args + [src_file]

  def get_clang_command_asm(src_file):
    return get_compiler(src_file) + get_target_flags() + compile_args + [src_file]

  # preprocessor-only (-E) support
  if state.mode == Mode.PREPROCESS_ONLY:
    for input_file in [x[1] for x in input_files]:
      cmd = get_clang_command(input_file)
      if options.output_file:
        cmd += ['-o', options.output_file]
      # Do not compile, but just output the result from preprocessing stage or
      # output the dependency rule. Warning: clang and gcc behave differently
      # with -MF! (clang seems to not recognize it)
      logger.debug(('just preprocessor ' if state.has_dash_E else 'just dependencies: ') + ' '.join(cmd))
      shared.check_call(cmd)
    return []

  # Precompiled headers support
  if state.mode == Mode.PCH:
    headers = [header for _, header in input_files]
    for header in headers:
      if not shared.suffix(header) in HEADER_ENDINGS:
        exit_with_error(f'cannot mix precompiled headers with non-header inputs: {headers} : {header}')
      cmd = get_clang_command(header)
      if options.output_file:
        cmd += ['-o', options.output_file]
      logger.debug(f"running (for precompiled headers): {cmd[0]} {' '.join(cmd[1:])}")
      shared.check_call(cmd)
      return []

  linker_inputs = []
  seen_names = {}

  def uniquename(name):
    if name not in seen_names:
      seen_names[name] = str(len(seen_names))
    return unsuffixed(name) + '_' + seen_names[name] + shared.suffix(name)

  def get_object_filename(input_file):
    if state.mode == Mode.COMPILE_ONLY:
      # In compile-only mode we don't use any temp file.  The object files
      # are written directly to their final output locations.
      if options.output_file:
        assert len(input_files) == 1
        if get_file_suffix(options.output_file) == '.bc' and not settings.LTO and '-emit-llvm' not in state.orig_args:
          diagnostics.warning('emcc', '.bc output file suffix used without -flto or -emit-llvm.  Consider using .o extension since emcc will output an object file, not a bitcode file')
        return options.output_file
      else:
        return unsuffixed_basename(input_file) + options.default_object_extension
    else:
      return in_temp(unsuffixed(uniquename(input_file)) + options.default_object_extension)

  def compile_source_file(i, input_file):
    logger.debug(f'compiling source file: {input_file}')
    output_file = get_object_filename(input_file)
    if state.mode not in (Mode.COMPILE_ONLY, Mode.PREPROCESS_ONLY):
      linker_inputs.append((i, output_file))
    if get_file_suffix(input_file) in ASSEMBLY_ENDINGS:
      cmd = get_clang_command_asm(input_file)
    elif get_file_suffix(input_file) in PREPROCESSED_ENDINGS:
      cmd = get_clang_command_preprocessed(input_file)
    else:
      cmd = get_clang_command(input_file)
      if get_file_suffix(input_file) in ['.pcm']:
        cmd = [c for c in cmd if not c.startswith('-fprebuilt-module-path=')]
    if not state.has_dash_c:
      cmd += ['-c']
    cmd += ['-o', output_file]
    if state.mode == Mode.COMPILE_AND_LINK and '-gsplit-dwarf' in newargs:
      # When running in COMPILE_AND_LINK mode we compile to temporary location
      # but we want the `.dwo` file to be generated in the current working directory,
      # like it is under clang.  We could avoid this hack if we use the clang driver
      # to generate the temporary files, but that would also involve using the clang
      # driver to perform linking which would be big change.
      cmd += ['-Xclang', '-split-dwarf-file', '-Xclang', unsuffixed_basename(input_file) + '.dwo']
      cmd += ['-Xclang', '-split-dwarf-output', '-Xclang', unsuffixed_basename(input_file) + '.dwo']
    shared.check_call(cmd)
    if output_file not in ('-', os.devnull):
      assert os.path.exists(output_file)

  # First, generate LLVM bitcode. For each input file, we get base.o with bitcode
  for i, input_file in input_files:
    file_suffix = get_file_suffix(input_file)
    if file_suffix in SOURCE_ENDINGS + ASSEMBLY_ENDINGS or (state.has_dash_c and file_suffix == '.bc'):
      compile_source_file(i, input_file)
    elif file_suffix in DYNAMICLIB_ENDINGS:
      logger.debug(f'using shared library: {input_file}')
      linker_inputs.append((i, input_file))
    elif building.is_ar(input_file):
      logger.debug(f'using static library: {input_file}')
      linker_inputs.append((i, input_file))
    elif language_mode:
      compile_source_file(i, input_file)
    elif input_file == '-':
      exit_with_error('-E or -x required when input is from standard input')
    else:
      # Default to assuming the inputs are object files and pass them to the linker
      logger.debug(f'using object file: {input_file}')
      linker_inputs.append((i, input_file))

  return linker_inputs


@ToolchainProfiler.profile_block('calculate system libraries')
def phase_calculate_system_libraries(state, linker_arguments, newargs):
  extra_files_to_link = []
  # Link in ports and system libraries, if necessary
  if not settings.SIDE_MODULE:
    # Ports are always linked into the main module, never the side module.
    extra_files_to_link += ports.get_libs(settings)
  extra_files_to_link += system_libs.calculate(newargs, forced=state.forced_stdlibs)
  linker_arguments.extend(extra_files_to_link)


@ToolchainProfiler.profile_block('link')
def phase_link(linker_arguments, wasm_target, js_syms):
  logger.debug(f'linking: {linker_arguments}')

  # Make a final pass over settings.EXPORTED_FUNCTIONS to remove any
  # duplication between functions added by the driver/libraries and function
  # specified by the user
  settings.EXPORTED_FUNCTIONS = dedup_list(settings.EXPORTED_FUNCTIONS)
  settings.REQUIRED_EXPORTS = dedup_list(settings.REQUIRED_EXPORTS)
  settings.EXPORT_IF_DEFINED = dedup_list(settings.EXPORT_IF_DEFINED)

  building.link_lld(linker_arguments, wasm_target, external_symbols=js_syms)


@ToolchainProfiler.profile_block('post link')
def phase_post_link(options, state, in_wasm, wasm_target, target, js_syms):
  global final_js

  target_basename = unsuffixed_basename(target)

  if options.oformat != OFormat.WASM:
    final_js = in_temp(target_basename + '.js')

  settings.TARGET_BASENAME = unsuffixed_basename(target)

  if options.oformat in (OFormat.JS, OFormat.MJS):
    state.js_target = target
  else:
    state.js_target = get_secondary_target(target, '.js')

  settings.TARGET_JS_NAME = os.path.basename(state.js_target)

  if settings.MEM_INIT_IN_WASM:
    memfile = None
  else:
    memfile = shared.replace_or_append_suffix(target, '.mem')

  phase_emscript(options, in_wasm, wasm_target, memfile, js_syms)

  if options.js_transform:
    phase_source_transforms(options)

  if memfile and not settings.MINIMAL_RUNTIME:
    # MINIMAL_RUNTIME doesn't use `var memoryInitializer` but instead expects Module['mem'] to
    # be loaded before the module.  See src/postamble_minimal.js.
    phase_memory_initializer(memfile)

  phase_binaryen(target, options, wasm_target)

  # If we are not emitting any JS then we are all done now
  if options.oformat != OFormat.WASM:
    phase_final_emitting(options, state, target, wasm_target, memfile)


@ToolchainProfiler.profile_block('emscript')
def phase_emscript(options, in_wasm, wasm_target, memfile, js_syms):
  # Emscripten
  logger.debug('emscript')

  if embed_memfile(options):
    settings.SUPPORT_BASE64_EMBEDDING = 1
    # _read in shell.js depends on intArrayToString when SUPPORT_BASE64_EMBEDDING is set
    settings.DEFAULT_LIBRARY_FUNCS_TO_INCLUDE.append('$intArrayToString')

  emscripten.run(in_wasm, wasm_target, final_js, memfile, js_syms)
  save_intermediate('original')


@ToolchainProfiler.profile_block('source transforms')
def phase_source_transforms(options):
  # Apply a source code transformation, if requested
  global final_js
  safe_copy(final_js, final_js + '.tr.js')
  final_js += '.tr.js'
  posix = not shared.WINDOWS
  logger.debug('applying transform: %s', options.js_transform)
  shared.check_call(building.remove_quotes(shlex.split(options.js_transform, posix=posix) + [os.path.abspath(final_js)]))
  save_intermediate('transformed')


@ToolchainProfiler.profile_block('memory initializer')
def phase_memory_initializer(memfile):
  # For the wasm backend, we don't have any memory info in JS. All we need to do
  # is set the memory initializer url.
  global final_js

  src = read_file(final_js)
  src = do_replace(src, '<<< MEM_INITIALIZER >>>', '"%s"' % os.path.basename(memfile))
  write_file(final_js + '.mem.js', src)
  final_js += '.mem.js'


def create_worker_file(input_file, target_dir, output_file):
  output_file = os.path.join(target_dir, output_file)
  input_file = utils.path_from_root(input_file)
  contents = shared.read_and_preprocess(input_file, expand_macros=True)
  write_file(output_file, contents)

  # Minify the worker JS files file in optimized builds
  if (settings.OPT_LEVEL >= 1 or settings.SHRINK_LEVEL >= 1) and not settings.DEBUG_LEVEL:
    contents = building.acorn_optimizer(output_file, ['minifyWhitespace'], return_output=True)
    write_file(output_file, contents)


@ToolchainProfiler.profile_block('final emitting')
def phase_final_emitting(options, state, target, wasm_target, memfile):
  global final_js

  target_dir = os.path.dirname(os.path.abspath(target))
  if settings.PTHREADS:
    create_worker_file('src/worker.js', target_dir, settings.PTHREAD_WORKER_FILE)

  # Deploy the Wasm Worker bootstrap file as an output file (*.ww.js)
  if settings.WASM_WORKERS == 1:
    create_worker_file('src/wasm_worker.js', target_dir, settings.WASM_WORKER_FILE)

  # Deploy the Audio Worklet module bootstrap file (*.aw.js)
  if settings.AUDIO_WORKLET == 1:
    create_worker_file('src/audio_worklet.js', target_dir, settings.AUDIO_WORKLET_FILE)

  if settings.MODULARIZE:
    modularize()
  elif settings.USE_CLOSURE_COMPILER:
    module_export_name_substitution()

  # Run a final optimization pass to clean up items that were not possible to
  # optimize by Closure, or unoptimalities that were left behind by processing
  # steps that occurred after Closure.
  if settings.MINIMAL_RUNTIME == 2 and settings.USE_CLOSURE_COMPILER and settings.DEBUG_LEVEL == 0:
    shared.run_js_tool(utils.path_from_root('tools/unsafe_optimizations.js'), [final_js, '-o', final_js], cwd=utils.path_from_root('.'))
    # Finally, rerun Closure compile with simple optimizations. It will be able
    # to further minify the code. (n.b. it would not be safe to run in advanced
    # mode)
    final_js = building.closure_compiler(final_js, pretty=False, advanced=False, extra_closure_args=options.closure_args)

  # Unmangle previously mangled `import.meta` and `await import` references in
  # both main code and libraries.
  # See also: `preprocess` in parseTools.js.
  if settings.EXPORT_ES6 and settings.USE_ES6_IMPORT_META:
    src = read_file(final_js)
    final_js += '.esmeta.js'
    write_file(final_js, src
               .replace('EMSCRIPTEN$IMPORT$META', 'import.meta')
               .replace('EMSCRIPTEN$AWAIT$IMPORT', 'await import'))
    shared.get_temp_files().note(final_js)
    save_intermediate('es6-module')

  # Apply pre and postjs files
  if options.extern_pre_js or options.extern_post_js:
    logger.debug('applying extern pre/postjses')
    src = read_file(final_js)
    final_js += '.epp.js'
    with open(final_js, 'w', encoding='utf-8') as f:
      f.write(options.extern_pre_js)
      f.write(src)
      f.write(options.extern_post_js)
    save_intermediate('extern-pre-post')

  js_manipulation.handle_license(final_js)

  js_target = state.js_target

  # The JS is now final. Move it to its final location
  move_file(final_js, js_target)

  target_basename = unsuffixed_basename(target)

  # If we were asked to also generate HTML, do that
  if options.oformat == OFormat.HTML:
    generate_html(target, options, js_target, target_basename,
                  wasm_target, memfile)
  elif settings.PROXY_TO_WORKER:
    generate_worker_js(target, js_target, target_basename)

  if embed_memfile(options) and memfile:
    delete_file(memfile)

  if settings.SPLIT_MODULE:
    diagnostics.warning('experimental', 'The SPLIT_MODULE setting is experimental and subject to change')
    do_split_module(wasm_target, options)

  if not settings.SINGLE_FILE:
    tools.line_endings.convert_line_endings_in_file(js_target, os.linesep, options.output_eol)

  if options.executable:
    make_js_executable(js_target)

  if options.embind_emit_tsd:
    out = shared.run_js_tool(js_target, [], stdout=PIPE)
    write_file(options.embind_emit_tsd, out)


def version_string():
  # if the emscripten folder is not a git repo, don't run git show - that can
  # look up and find the revision in a parent directory that is a git repo
  revision_suffix = ''
  if os.path.exists(utils.path_from_root('.git')):
    git_rev = run_process(
      ['git', 'rev-parse', 'HEAD'],
      stdout=PIPE, stderr=PIPE, cwd=utils.path_from_root()).stdout.strip()
    revision_suffix = ' (%s)' % git_rev
  elif os.path.exists(utils.path_from_root('emscripten-revision.txt')):
    rev = read_file(utils.path_from_root('emscripten-revision.txt')).strip()
    revision_suffix = ' (%s)' % rev
  return f'emcc (Emscripten gcc/clang-like replacement + linker emulating GNU ld) {shared.EMSCRIPTEN_VERSION}{revision_suffix}'


def parse_args(newargs):
  options = EmccOptions()
  settings_changes = []
  user_js_defines = []
  should_exit = False
  skip = False

  for i in range(len(newargs)):
    if skip:
      skip = False
      continue

    # Support legacy '--bind' flag, by mapping to `-lembind` which now
    # has the same effect
    if newargs[i] == '--bind':
      newargs[i] = '-lembind'

    arg = newargs[i]
    arg_value = None

    def check_flag(value):
      # Check for and consume a flag
      if arg == value:
        newargs[i] = ''
        return True
      return False

    def check_arg(name):
      nonlocal arg_value
      if arg.startswith(name) and '=' in arg:
        arg_value = arg.split('=', 1)[1]
        newargs[i] = ''
        return True
      if arg == name:
        if len(newargs) <= i + 1:
          exit_with_error("option '%s' requires an argument" % arg)
        arg_value = newargs[i + 1]
        newargs[i] = ''
        newargs[i + 1] = ''
        return True
      return False

    def consume_arg():
      nonlocal arg_value
      assert arg_value is not None
      rtn = arg_value
      arg_value = None
      return rtn

    def consume_arg_file():
      name = consume_arg()
      if not os.path.isfile(name):
        exit_with_error("'%s': file not found: '%s'" % (arg, name))
      return name

    if arg.startswith('-O'):
      # Let -O default to -O2, which is what gcc does.
      requested_level = removeprefix(arg, '-O') or '2'
      if requested_level == 's':
        requested_level = 2
        settings.SHRINK_LEVEL = 1
      elif requested_level == 'z':
        requested_level = 2
        settings.SHRINK_LEVEL = 2
      elif requested_level == 'g':
        requested_level = 1
        settings.SHRINK_LEVEL = 0
        settings.DEBUG_LEVEL = max(settings.DEBUG_LEVEL, 1)
      else:
        settings.SHRINK_LEVEL = 0
      settings.OPT_LEVEL = validate_arg_level(requested_level, 3, 'invalid optimization level: ' + arg, clamp=True)
    elif check_arg('--js-opts'):
      logger.warning('--js-opts ignored when using llvm backend')
      consume_arg()
    elif check_arg('--llvm-opts'):
      diagnostics.warning('deprecated', '--llvm-opts is deprecated.  All non-emcc args are passed through to clang.')
    elif arg.startswith('-flto'):
      if '=' in arg:
        settings.LTO = arg.split('=')[1]
      else:
        settings.LTO = 'full'
    elif check_arg('--llvm-lto'):
      logger.warning('--llvm-lto ignored when using llvm backend')
      consume_arg()
    elif check_arg('--closure-args'):
      args = consume_arg()
      options.closure_args += shlex.split(args)
    elif check_arg('--closure'):
      options.use_closure_compiler = int(consume_arg())
    elif check_arg('--js-transform'):
      options.js_transform = consume_arg()
    elif check_arg('--reproduce'):
      options.reproduce = consume_arg()
    elif check_arg('--pre-js'):
      options.pre_js.append(consume_arg_file())
    elif check_arg('--post-js'):
      options.post_js.append(consume_arg_file())
    elif check_arg('--extern-pre-js'):
      options.extern_pre_js.append(consume_arg_file())
    elif check_arg('--extern-post-js'):
      options.extern_post_js.append(consume_arg_file())
    elif check_arg('--compiler-wrapper'):
      config.COMPILER_WRAPPER = consume_arg()
    elif check_flag('--post-link'):
      options.post_link = True
    elif check_arg('--oformat'):
      formats = [f.lower() for f in OFormat.__members__]
      fmt = consume_arg()
      if fmt not in formats:
        exit_with_error('invalid output format: `%s` (must be one of %s)' % (fmt, formats))
      options.oformat = getattr(OFormat, fmt.upper())
    elif check_arg('--minify'):
      arg = consume_arg()
      if arg != '0':
        exit_with_error('0 is the only supported option for --minify; 1 has been deprecated')
      settings.DEBUG_LEVEL = max(1, settings.DEBUG_LEVEL)
    elif arg.startswith('-g'):
      options.requested_debug = arg
      requested_level = removeprefix(arg, '-g') or '3'
      if is_int(requested_level):
        # the -gX value is the debug level (-g1, -g2, etc.)
        settings.DEBUG_LEVEL = validate_arg_level(requested_level, 4, 'invalid debug level: ' + arg)
        # if we don't need to preserve LLVM debug info, do not keep this flag
        # for clang
        if settings.DEBUG_LEVEL < 3:
          newargs[i] = '-g0'
        else:
          # for 3+, report -g3 to clang as -g4 etc. are not accepted
          newargs[i] = '-g3'
          if settings.DEBUG_LEVEL == 3:
            settings.GENERATE_DWARF = 1
          if settings.DEBUG_LEVEL == 4:
            settings.GENERATE_SOURCE_MAP = 1
            diagnostics.warning('deprecated', 'please replace -g4 with -gsource-map')
      else:
        if requested_level.startswith('force_dwarf'):
          exit_with_error('gforce_dwarf was a temporary option and is no longer necessary (use -g)')
        elif requested_level.startswith('separate-dwarf'):
          # emit full DWARF but also emit it in a file on the side
          newargs[i] = '-g'
          # if a file is provided, use that; otherwise use the default location
          # (note that we do not know the default location until all args have
          # been parsed, so just note True for now).
          if requested_level != 'separate-dwarf':
            if not requested_level.startswith('separate-dwarf=') or requested_level.count('=') != 1:
              exit_with_error('invalid -gseparate-dwarf=FILENAME notation')
            settings.SEPARATE_DWARF = requested_level.split('=')[1]
          else:
            settings.SEPARATE_DWARF = True
        elif requested_level == 'source-map':
          settings.GENERATE_SOURCE_MAP = 1
          newargs[i] = '-g'
        # a non-integer level can be something like -gline-tables-only. keep
        # the flag for the clang frontend to emit the appropriate DWARF info.
        # set the emscripten debug level to 3 so that we do not remove that
        # debug info during link (during compile, this does not make a
        # difference).
        settings.DEBUG_LEVEL = 3
    elif check_flag('-profiling') or check_flag('--profiling'):
      settings.DEBUG_LEVEL = max(settings.DEBUG_LEVEL, 2)
    elif check_flag('-profiling-funcs') or check_flag('--profiling-funcs'):
      settings.EMIT_NAME_SECTION = 1
    elif newargs[i] == '--tracing' or newargs[i] == '--memoryprofiler':
      if newargs[i] == '--memoryprofiler':
        options.memory_profiler = True
      newargs[i] = ''
      settings_changes.append('EMSCRIPTEN_TRACING=1')
      settings.JS_LIBRARIES.append((0, 'library_trace.js'))
    elif check_flag('--emit-symbol-map'):
      options.emit_symbol_map = True
      settings.EMIT_SYMBOL_MAP = 1
    elif check_arg('--embed-file'):
      options.embed_files.append(consume_arg())
    elif check_arg('--preload-file'):
      options.preload_files.append(consume_arg())
    elif check_arg('--exclude-file'):
      options.exclude_files.append(consume_arg())
    elif check_flag('--use-preload-cache'):
      options.use_preload_cache = True
    elif check_flag('--no-heap-copy'):
      diagnostics.warning('legacy-settings', 'ignoring legacy flag --no-heap-copy (that is the only mode supported now)')
    elif check_flag('--use-preload-plugins'):
      options.use_preload_plugins = True
    elif check_flag('--ignore-dynamic-linking'):
      options.ignore_dynamic_linking = True
    elif arg == '-v':
      shared.PRINT_STAGES = True
    elif check_arg('--shell-file'):
      options.shell_path = consume_arg_file()
    elif check_arg('--source-map-base'):
      options.source_map_base = consume_arg()
    elif check_arg('--embind-emit-tsd'):
      options.embind_emit_tsd = consume_arg()
      settings.INVOKE_RUN = False
    elif check_flag('--no-entry'):
      options.no_entry = True
    elif check_arg('--js-library'):
      settings.JS_LIBRARIES.append((i + 1, os.path.abspath(consume_arg_file())))
    elif check_flag('--remove-duplicates'):
      diagnostics.warning('legacy-settings', '--remove-duplicates is deprecated as it is no longer needed. If you cannot link without it, file a bug with a testcase')
    elif check_flag('--jcache'):
      logger.error('jcache is no longer supported')
    elif check_arg('--cache'):
      config.CACHE = os.path.normpath(consume_arg())
      cache.setup()
      # Ensure child processes share the same cache (e.g. when using emcc to compiler system
      # libraries)
      os.environ['EM_CACHE'] = config.CACHE
    elif check_flag('--clear-cache'):
      logger.info('clearing cache as requested by --clear-cache: `%s`', cache.cachedir)
      cache.erase()
      shared.perform_sanity_checks() # this is a good time for a sanity check
      should_exit = True
    elif check_flag('--clear-ports'):
      logger.info('clearing ports and cache as requested by --clear-ports')
      ports.clear()
      cache.erase()
      shared.perform_sanity_checks() # this is a good time for a sanity check
      should_exit = True
    elif check_flag('--check'):
      print(version_string(), file=sys.stderr)
      shared.check_sanity(force=True)
      should_exit = True
    elif check_flag('--show-ports'):
      ports.show_ports()
      should_exit = True
    elif check_arg('--memory-init-file'):
      options.memory_init_file = int(consume_arg())
    elif check_flag('--proxy-to-worker'):
      settings_changes.append('PROXY_TO_WORKER=1')
    elif check_arg('--valid-abspath'):
      options.valid_abspaths.append(consume_arg())
    elif check_flag('--separate-asm'):
      exit_with_error('cannot --separate-asm with the wasm backend, since not emitting asm.js')
    elif arg.startswith(('-I', '-L')):
      path_name = arg[2:]
      if os.path.isabs(path_name) and not is_valid_abspath(options, path_name):
        # Of course an absolute path to a non-system-specific library or header
        # is fine, and you can ignore this warning. The danger are system headers
        # that are e.g. x86 specific and non-portable. The emscripten bundled
        # headers are modified to be portable, local system ones are generally not.
        diagnostics.warning(
            'absolute-paths', f'-I or -L of an absolute path "{arg}" '
            'encountered. If this is to a local system header/library, it may '
            'cause problems (local system files make sense for compiling natively '
            'on your system, but not necessarily to JavaScript).')
    elif check_flag('--emrun'):
      options.emrun = True
    elif check_flag('--cpuprofiler'):
      options.cpu_profiler = True
    elif check_flag('--threadprofiler'):
      settings_changes.append('PTHREADS_PROFILING=1')
    elif arg == '-fno-exceptions':
      settings.DISABLE_EXCEPTION_CATCHING = 1
      settings.DISABLE_EXCEPTION_THROWING = 1
      settings.WASM_EXCEPTIONS = 0
    elif arg == '-mbulk-memory':
      settings.BULK_MEMORY = 1
    elif arg == '-mno-bulk-memory':
      settings.BULK_MEMORY = 0
    elif arg == '-fexceptions':
      # TODO Currently -fexceptions only means Emscripten EH. Switch to wasm
      # exception handling by default when -fexceptions is given when wasm
      # exception handling becomes stable.
      settings.DISABLE_EXCEPTION_THROWING = 0
      settings.DISABLE_EXCEPTION_CATCHING = 0
    elif arg == '-fwasm-exceptions':
      settings.WASM_EXCEPTIONS = 1
    elif arg == '-fignore-exceptions':
      settings.DISABLE_EXCEPTION_CATCHING = 1
    elif check_arg('--default-obj-ext'):
      options.default_object_extension = consume_arg()
      if not options.default_object_extension.startswith('.'):
        options.default_object_extension = '.' + options.default_object_extension
    elif arg.startswith('-fsanitize=cfi'):
      exit_with_error('emscripten does not currently support -fsanitize=cfi')
    elif check_arg('--output_eol'):
      style = consume_arg()
      if style.lower() == 'windows':
        options.output_eol = '\r\n'
      elif style.lower() == 'linux':
        options.output_eol = '\n'
      else:
        exit_with_error(f'Invalid value "{style}" to --output_eol!')
    # Record PTHREADS setting because it controls whether --shared-memory is passed to lld
    elif arg == '-pthread':
      settings.PTHREADS = 1
      # Also set the legacy setting name, in case use JS code depends on it.
      settings.USE_PTHREADS = 1
    elif arg == '-pthreads':
      exit_with_error('unrecognized command-line option `-pthreads`; did you mean `-pthread`?')
    elif arg in ('-fno-diagnostics-color', '-fdiagnostics-color=never'):
      colored_logger.disable()
      diagnostics.color_enabled = False
    elif arg == '-fno-rtti':
      settings.USE_RTTI = 0
    elif arg == '-frtti':
      settings.USE_RTTI = 1
    elif arg.startswith('-jsD'):
      key = removeprefix(arg, '-jsD')
      if '=' in key:
        key, value = key.split('=')
      else:
        value = '1'
      if key in settings.keys():
        exit_with_error(f'{arg}: cannot change built-in settings values with a -jsD directive. Pass -s{key}={value} instead!')
      user_js_defines += [(key, value)]
      newargs[i] = ''
    elif check_flag('-shared'):
      options.shared = True
    elif check_flag('-r'):
      options.relocatable = True
    elif check_arg('-o'):
      options.output_file = consume_arg()
    elif arg.startswith('-o'):
      options.output_file = removeprefix(arg, '-o')
      newargs[i] = ''
    elif arg == '-mllvm':
      # Ignore the next argument rather than trying to parse it.  This is needed
      # because llvm args could, for example, start with `-o` and we don't want
      # to confuse that with a normal `-o` flag.
      skip = True

  if should_exit:
    sys.exit(0)

  newargs = [a for a in newargs if a]
  return options, settings_changes, user_js_defines, newargs


@ToolchainProfiler.profile_block('binaryen')
def phase_binaryen(target, options, wasm_target):
  global final_js
  logger.debug('using binaryen')
  # whether we need to emit -g (function name debug info) in the final wasm
  debug_info = settings.DEBUG_LEVEL >= 2 or settings.EMIT_NAME_SECTION
  # whether we need to emit -g in the intermediate binaryen invocations (but not
  # necessarily at the very end). this is necessary if we depend on debug info
  # during compilation, even if we do not emit it at the end.
  # we track the number of causes for needing intermdiate debug info so
  # that we can stop emitting it when possible - in particular, that is
  # important so that we stop emitting it before the end, and it is not in the
  # final binary (if it shouldn't be)
  intermediate_debug_info = 0
  if debug_info:
    intermediate_debug_info += 1
  if options.emit_symbol_map:
    intermediate_debug_info += 1
  if settings.ASYNCIFY == 1:
    intermediate_debug_info += 1
  # note that wasm-ld can strip DWARF info for us too (--strip-debug), but it
  # also strips the Names section. so to emit just the Names section we don't
  # tell wasm-ld to strip anything, and we do it here.
  strip_debug = settings.DEBUG_LEVEL < 3
  strip_producers = not settings.EMIT_PRODUCERS_SECTION
  # run wasm-opt if we have work for it: either passes, or if we are using
  # source maps (which requires some extra processing to keep the source map
  # but remove DWARF)
  passes = get_binaryen_passes()
  if passes or settings.GENERATE_SOURCE_MAP:
    # if we need to strip certain sections, and we have wasm-opt passes
    # to run anyhow, do it with them.
    if strip_debug:
      passes += ['--strip-debug']
    if strip_producers:
      passes += ['--strip-producers']
    # if asyncify is used, we will use it in the next stage, and so if it is
    # the only reason we need intermediate debug info, we can stop keeping it
    if settings.ASYNCIFY == 1:
      intermediate_debug_info -= 1
    # currently binaryen's DWARF support will limit some optimizations; warn on
    # that. see https://github.com/emscripten-core/emscripten/issues/15269
    dwarf_info = settings.DEBUG_LEVEL >= 3
    if dwarf_info:
      diagnostics.warning('limited-postlink-optimizations', 'running limited binaryen optimizations because DWARF info requested (or indirectly required)')
    with ToolchainProfiler.profile_block('wasm_opt'):
      building.run_wasm_opt(wasm_target,
                            wasm_target,
                            args=passes,
                            debug=intermediate_debug_info)
      building.save_intermediate(wasm_target, 'byn.wasm')
  elif strip_debug or strip_producers:
    # we are not running wasm-opt. if we need to strip certain sections
    # then do so using llvm-objcopy which is fast and does not rewrite the
    # code (which is better for debug info)
    sections = ['producers'] if strip_producers else []
    with ToolchainProfiler.profile_block('strip_producers'):
      building.strip(wasm_target, wasm_target, debug=strip_debug, sections=sections)
      building.save_intermediate(wasm_target, 'strip.wasm')

  if settings.EVAL_CTORS:
    with ToolchainProfiler.profile_block('eval_ctors'):
      building.eval_ctors(final_js, wasm_target, debug_info=intermediate_debug_info)
      building.save_intermediate(wasm_target, 'ctors.wasm')

  # after generating the wasm, do some final operations

  if final_js:
    if settings.SUPPORT_BIG_ENDIAN:
      with ToolchainProfiler.profile_block('little_endian_heap'):
        final_js = building.little_endian_heap(final_js)

    # >=2GB heap support requires pointers in JS to be unsigned. rather than
    # require all pointers to be unsigned by default, which increases code size
    # a little, keep them signed, and just unsign them here if we need that.
    if settings.CAN_ADDRESS_2GB:
      with ToolchainProfiler.profile_block('use_unsigned_pointers_in_js'):
        final_js = building.use_unsigned_pointers_in_js(final_js)

    # pthreads memory growth requires some additional JS fixups.
    # note that we must do this after handling of unsigned pointers. unsigning
    # adds some >>> 0 things, while growth will replace a HEAP8 with a call to
    # a method to get the heap, and that call would not be recognized by the
    # unsigning pass
    if settings.PTHREADS and settings.ALLOW_MEMORY_GROWTH:
      with ToolchainProfiler.profile_block('apply_wasm_memory_growth'):
        final_js = building.apply_wasm_memory_growth(final_js)

    if settings.USE_ASAN:
      final_js = building.instrument_js_for_asan(final_js)

    if settings.SAFE_HEAP:
      final_js = building.instrument_js_for_safe_heap(final_js)

    if settings.OPT_LEVEL >= 2 and settings.DEBUG_LEVEL <= 2:
      # minify the JS. Do not minify whitespace if Closure is used, so that
      # Closure can print out readable error messages (Closure will then
      # minify whitespace afterwards)
      with ToolchainProfiler.profile_block('minify_wasm'):
        save_intermediate_with_wasm('preclean', wasm_target)
        final_js = building.minify_wasm_js(js_file=final_js,
                                           wasm_file=wasm_target,
                                           expensive_optimizations=will_metadce(),
                                           minify_whitespace=minify_whitespace() and not options.use_closure_compiler,
                                           debug_info=intermediate_debug_info)
        save_intermediate_with_wasm('postclean', wasm_target)

  if settings.ASYNCIFY_LAZY_LOAD_CODE:
    with ToolchainProfiler.profile_block('asyncify_lazy_load_code'):
      building.asyncify_lazy_load_code(wasm_target, debug=intermediate_debug_info)

  if final_js and (options.use_closure_compiler or settings.TRANSPILE_TO_ES5):
    if options.use_closure_compiler:
      with ToolchainProfiler.profile_block('closure_compile'):
        final_js = building.closure_compiler(final_js, pretty=not minify_whitespace(),
                                             extra_closure_args=options.closure_args)
    else:
      with ToolchainProfiler.profile_block('closure_transpile'):
        final_js = building.closure_transpile(final_js, pretty=not minify_whitespace())
    save_intermediate_with_wasm('closure', wasm_target)

  symbols_file = None
  if options.emit_symbol_map:
    symbols_file = shared.replace_or_append_suffix(target, '.symbols')

  if settings.WASM2JS:
    symbols_file_js = None
    if settings.WASM == 2:
      # With normal wasm2js mode this file gets included as part of the
      # preamble, but with WASM=2 its a separate file.
      wasm2js_polyfill = read_and_preprocess(utils.path_from_root('src/wasm2js.js'), expand_macros=True)
      wasm2js_template = wasm_target + '.js'
      write_file(wasm2js_template, wasm2js_polyfill)
      # generate secondary file for JS symbols
      if options.emit_symbol_map:
        symbols_file_js = shared.replace_or_append_suffix(wasm2js_template, '.symbols')
    else:
      wasm2js_template = final_js
      if options.emit_symbol_map:
        symbols_file_js = shared.replace_or_append_suffix(target, '.symbols')

    wasm2js = building.wasm2js(wasm2js_template,
                               wasm_target,
                               opt_level=settings.OPT_LEVEL,
                               minify_whitespace=minify_whitespace(),
                               use_closure_compiler=options.use_closure_compiler,
                               debug_info=debug_info,
                               symbols_file=symbols_file,
                               symbols_file_js=symbols_file_js)

    shared.get_temp_files().note(wasm2js)

    if settings.WASM == 2:
      safe_copy(wasm2js, wasm2js_template)

    if settings.WASM != 2:
      final_js = wasm2js
      # if we only target JS, we don't need the wasm any more
      delete_file(wasm_target)

    save_intermediate('wasm2js')

  # emit the final symbols, either in the binary or in a symbol map.
  # this will also remove debug info if we only kept it around in the intermediate invocations.
  # note that if we aren't emitting a binary (like in wasm2js) then we don't
  # have anything to do here.
  if options.emit_symbol_map:
    intermediate_debug_info -= 1
    if os.path.exists(wasm_target):
      building.handle_final_wasm_symbols(wasm_file=wasm_target, symbols_file=symbols_file, debug_info=intermediate_debug_info)
      save_intermediate_with_wasm('symbolmap', wasm_target)

  if settings.DEBUG_LEVEL >= 3 and settings.SEPARATE_DWARF and os.path.exists(wasm_target):
    building.emit_debug_on_side(wasm_target)

  # we have finished emitting the wasm, and so intermediate debug info will
  # definitely no longer be used tracking it.
  if debug_info:
    intermediate_debug_info -= 1
  assert intermediate_debug_info == 0
  # strip debug info if it was not already stripped by the last command
  if not debug_info and building.binaryen_kept_debug_info and \
     building.os.path.exists(wasm_target):
    with ToolchainProfiler.profile_block('strip_with_wasm_opt'):
      building.run_wasm_opt(wasm_target, wasm_target)

  # replace placeholder strings with correct subresource locations
  if final_js and settings.SINGLE_FILE and not settings.WASM2JS:
    js = read_file(final_js)

    if settings.MINIMAL_RUNTIME:
      js = do_replace(js, '<<< WASM_BINARY_DATA >>>', base64_encode(read_binary(wasm_target)))
    else:
      js = do_replace(js, '<<< WASM_BINARY_FILE >>>', get_subresource_location(wasm_target))
    delete_file(wasm_target)
    write_file(final_js, js)


def node_es6_imports():
  if not settings.EXPORT_ES6 or not shared.target_environment_may_be('node'):
    return ''

  # Multi-environment builds uses `await import` in `shell.js`
  if shared.target_environment_may_be('web'):
    return ''

  # Use static import declaration if we only target Node.js
  return '''
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
'''


def modularize():
  global final_js
  logger.debug(f'Modularizing, assigning to var {settings.EXPORT_NAME}')
  src = read_file(final_js)

  # Multi-environment ES6 builds require an async function
  async_emit = ''
  if settings.EXPORT_ES6 and \
     shared.target_environment_may_be('node') and \
     shared.target_environment_may_be('web'):
    async_emit = 'async '

  # Return the incoming `moduleArg`.  This is is equeivielt to the `Module` var within the
  # generated code but its not run through closure minifiection so we can reference it in
  # the the return statement.
  return_value = 'moduleArg'
  if settings.WASM_ASYNC_COMPILATION:
    return_value += '.ready'
  if not settings.EXPORT_READY_PROMISE:
    return_value = '{}'

  # TODO: Remove when https://bugs.webkit.org/show_bug.cgi?id=223533 is resolved.
  if async_emit != '' and settings.EXPORT_NAME == 'config':
    diagnostics.warning('emcc', 'EXPORT_NAME should not be named "config" when targeting Safari')

  src = '''
%(maybe_async)sfunction(moduleArg = {}) {

%(src)s

  return %(return_value)s
}
%(capture_module_function_for_audio_worklet)s
''' % {
    'maybe_async': async_emit,
    'src': src,
    'return_value': return_value,
    # Given the async nature of how the Module function and Module object come into existence in AudioWorkletGlobalScope,
    # store the Module function under a different variable name so that AudioWorkletGlobalScope will be able to reference
    # it without aliasing/conflicting with the Module variable name.
    'capture_module_function_for_audio_worklet': 'globalThis.AudioWorkletModule = Module;' if settings.AUDIO_WORKLET and settings.MODULARIZE else ''
  }

  if settings.MINIMAL_RUNTIME and not settings.PTHREADS:
    # Single threaded MINIMAL_RUNTIME programs do not need access to
    # document.currentScript, so a simple export declaration is enough.
    src = 'var %s=%s' % (settings.EXPORT_NAME, src)
  else:
    script_url_node = ''
    # When MODULARIZE this JS may be executed later,
    # after document.currentScript is gone, so we save it.
    # In EXPORT_ES6 + PTHREADS the 'thread' is actually an ES6 module webworker running in strict mode,
    # so doesn't have access to 'document'. In this case use 'import.meta' instead.
    if settings.EXPORT_ES6 and settings.USE_ES6_IMPORT_META:
      script_url = 'import.meta.url'
    else:
      script_url = "typeof document !== 'undefined' && document.currentScript ? document.currentScript.src : undefined"
      if shared.target_environment_may_be('node'):
        script_url_node = "if (typeof __filename !== 'undefined') _scriptDir = _scriptDir || __filename;"
    src = '''%(node_imports)s
var %(EXPORT_NAME)s = (() => {
  var _scriptDir = %(script_url)s;
  %(script_url_node)s
  return (%(src)s);
})();
''' % {
      'node_imports': node_es6_imports(),
      'EXPORT_NAME': settings.EXPORT_NAME,
      'script_url': script_url,
      'script_url_node': script_url_node,
      'src': src
    }

  final_js += '.modular.js'
  with open(final_js, 'w', encoding='utf-8') as f:
    f.write(src)

    # Export using a UMD style export, or ES6 exports if selected
    if settings.EXPORT_ES6:
      f.write('export default %s;' % settings.EXPORT_NAME)
    elif not settings.MINIMAL_RUNTIME:
      f.write('''\
if (typeof exports === 'object' && typeof module === 'object')
  module.exports = %(EXPORT_NAME)s;
else if (typeof define === 'function' && define['amd'])
  define([], () => %(EXPORT_NAME)s);
''' % {'EXPORT_NAME': settings.EXPORT_NAME})

  shared.get_temp_files().note(final_js)
  save_intermediate('modularized')


def module_export_name_substitution():
  assert not settings.MODULARIZE
  global final_js
  logger.debug(f'Private module export name substitution with {settings.EXPORT_NAME}')
  src = read_file(final_js)
  final_js += '.module_export_name_substitution.js'
  if settings.MINIMAL_RUNTIME and not settings.ENVIRONMENT_MAY_BE_NODE and not settings.ENVIRONMENT_MAY_BE_SHELL and not settings.AUDIO_WORKLET:
    # On the web, with MINIMAL_RUNTIME, the Module object is always provided
    # via the shell html in order to provide the .asm.js/.wasm content.
    replacement = settings.EXPORT_NAME
  else:
    replacement = "typeof %(EXPORT_NAME)s !== 'undefined' ? %(EXPORT_NAME)s : {}" % {"EXPORT_NAME": settings.EXPORT_NAME}
  new_src = re.sub(r'{\s*[\'"]?__EMSCRIPTEN_PRIVATE_MODULE_EXPORT_NAME_SUBSTITUTION__[\'"]?:\s*1\s*}', replacement, src)
  assert new_src != src, 'Unable to find Closure syntax __EMSCRIPTEN_PRIVATE_MODULE_EXPORT_NAME_SUBSTITUTION__ in source!'
  write_file(final_js, new_src)
  shared.get_temp_files().note(final_js)
  save_intermediate('module_export_name_substitution')


def generate_traditional_runtime_html(target, options, js_target, target_basename,
                                      wasm_target, memfile):
  script = ScriptSource()

  shell = read_and_preprocess(options.shell_path)
  assert '{{{ SCRIPT }}}' in shell, 'HTML shell must contain  {{{ SCRIPT }}}  , see src/shell.html for an example'
  base_js_target = os.path.basename(js_target)

  if settings.PROXY_TO_WORKER:
    proxy_worker_filename = (settings.PROXY_TO_WORKER_FILENAME or target_basename) + '.js'
    worker_js = worker_js_script(proxy_worker_filename)
    script.inline = ('''
  var filename = '%s';
  if ((',' + window.location.search.substr(1) + ',').indexOf(',noProxy,') < 0) {
    console.log('running code in a web worker');
''' % get_subresource_location(proxy_worker_filename)) + worker_js + '''
  } else {
    console.log('running code on the main thread');
    var fileBytes = tryParseAsDataURI(filename);
    var script = document.createElement('script');
    if (fileBytes) {
      script.innerHTML = intArrayToString(fileBytes);
    } else {
      script.src = filename;
    }
    document.body.appendChild(script);
  }
'''
  else:
    # Normal code generation path
    script.src = base_js_target

  if not settings.SINGLE_FILE:
    if memfile and not settings.MINIMAL_RUNTIME:
      # start to load the memory init file in the HTML, in parallel with the JS
      script.un_src()
      script.inline = ('''
          var memoryInitializer = '%s';
          memoryInitializer = Module['locateFile'] ? Module['locateFile'](memoryInitializer, '') : memoryInitializer;
          Module['memoryInitializerRequestURL'] = memoryInitializer;
          var meminitXHR = Module['memoryInitializerRequest'] = new XMLHttpRequest();
          meminitXHR.open('GET', memoryInitializer, true);
          meminitXHR.responseType = 'arraybuffer';
          meminitXHR.send(null);
''' % get_subresource_location(memfile)) + script.inline

    if not settings.WASM_ASYNC_COMPILATION:
      # We need to load the wasm file before anything else, it has to be synchronously ready TODO: optimize
      script.un_src()
      script.inline = '''
          var wasmURL = '%s';
          var wasmXHR = new XMLHttpRequest();
          wasmXHR.open('GET', wasmURL, true);
          wasmXHR.responseType = 'arraybuffer';
          wasmXHR.onload = function() {
            if (wasmXHR.status === 200 || wasmXHR.status === 0) {
              Module.wasmBinary = wasmXHR.response;
            } else {
              var wasmURLBytes = tryParseAsDataURI(wasmURL);
              if (wasmURLBytes) {
                Module.wasmBinary = wasmURLBytes.buffer;
              }
            }
%s
          };
          wasmXHR.send(null);
''' % (get_subresource_location(wasm_target), script.inline)

    if settings.WASM == 2:
      # If target browser does not support WebAssembly, we need to load the .wasm.js file before the main .js file.
      script.un_src()
      script.inline = '''
          function loadMainJs() {
%s
          }
          if (!window.WebAssembly || location.search.indexOf('_rwasm=0') > 0) {
            // Current browser does not support WebAssembly, load the .wasm.js JavaScript fallback
            // before the main JS runtime.
            var wasm2js = document.createElement('script');
            wasm2js.src = '%s';
            wasm2js.onload = loadMainJs;
            document.body.appendChild(wasm2js);
          } else {
            // Current browser supports Wasm, proceed with loading the main JS runtime.
            loadMainJs();
          }
''' % (script.inline, get_subresource_location(wasm_target) + '.js')

  # when script.inline isn't empty, add required helper functions such as tryParseAsDataURI
  if script.inline:
    for filename in ('arrayUtils.js', 'base64Utils.js', 'URIUtils.js'):
      content = read_and_preprocess(utils.path_from_root('src', filename))
      script.inline = content + script.inline

    script.inline = 'var ASSERTIONS = %s;\n%s' % (settings.ASSERTIONS, script.inline)

  # inline script for SINGLE_FILE output
  if settings.SINGLE_FILE:
    js_contents = script.inline or ''
    if script.src:
      js_contents += read_file(js_target)
    delete_file(js_target)
    script.src = None
    script.inline = js_contents

  html_contents = do_replace(shell, '{{{ SCRIPT }}}', script.replacement())
  html_contents = tools.line_endings.convert_line_endings(html_contents, '\n', options.output_eol)

  try:
    # Force UTF-8 output for consistency across platforms and with the web.
    utils.write_binary(target, html_contents.encode('utf-8'))
  except OSError as e:
    exit_with_error(f'cannot write output file: {e}')


def minify_html(filename):
  if settings.DEBUG_LEVEL >= 2:
    return

  opts = []
  # -g1 and greater retain whitespace and comments in source
  if settings.DEBUG_LEVEL == 0:
    opts += ['--collapse-whitespace',
             '--collapse-inline-tag-whitespace',
             '--remove-comments',
             '--remove-tag-whitespace',
             '--sort-attributes',
             '--sort-class-name']
  # -g2 and greater do not minify HTML at all
  if settings.DEBUG_LEVEL <= 1:
    opts += ['--decode-entities',
             '--collapse-boolean-attributes',
             '--remove-attribute-quotes',
             '--remove-redundant-attributes',
             '--remove-script-type-attributes',
             '--remove-style-link-type-attributes',
             '--use-short-doctype',
             '--minify-css', 'true',
             '--minify-js', 'true']

  # html-minifier also has the following options, but they look unsafe for use:
  # '--remove-optional-tags': removes e.g. <head></head> and <body></body> tags from the page.
  #                           (Breaks at least browser.test_sdl2glshader)
  # '--remove-empty-attributes': removes all attributes with whitespace-only values.
  #                              (Breaks at least browser.test_asmfs_hello_file)
  # '--remove-empty-elements': removes all elements with empty contents.
  #                            (Breaks at least browser.test_asm_swapping)

  logger.debug(f'minifying HTML file {filename}')
  size_before = os.path.getsize(filename)
  start_time = time.time()
  shared.check_call(shared.get_npm_cmd('html-minifier-terser') + [filename, '-o', filename] + opts, env=shared.env_with_node_in_path())

  elapsed_time = time.time() - start_time
  size_after = os.path.getsize(filename)
  delta = size_after - size_before
  logger.debug(f'HTML minification took {elapsed_time:.2f} seconds, and shrunk size of {filename} from {size_before} to {size_after} bytes, delta={delta} ({delta * 100.0 / size_before:+.2f}%)')


def generate_html(target, options, js_target, target_basename,
                  wasm_target, memfile):
  logger.debug('generating HTML')

  if settings.EXPORT_NAME != 'Module' and \
     not settings.MINIMAL_RUNTIME and \
     options.shell_path == utils.path_from_root('src/shell.html'):
    # the minimal runtime shell HTML is designed to support changing the export
    # name, but the normal one does not support that currently
    exit_with_error('Customizing EXPORT_NAME requires that the HTML be customized to use that name (see https://github.com/emscripten-core/emscripten/issues/10086)')

  if settings.MINIMAL_RUNTIME:
    generate_minimal_runtime_html(target, options, js_target, target_basename)
  else:
    generate_traditional_runtime_html(target, options, js_target, target_basename,
                                      wasm_target, memfile)

  if settings.MINIFY_HTML and (settings.OPT_LEVEL >= 1 or settings.SHRINK_LEVEL >= 1):
    minify_html(target)


def generate_worker_js(target, js_target, target_basename):
  if settings.SINGLE_FILE:
    # compiler output is embedded as base64
    proxy_worker_filename = get_subresource_location(js_target)
  else:
    # compiler output goes in .worker.js file
    move_file(js_target, shared.replace_suffix(js_target, '.worker.js'))
    worker_target_basename = target_basename + '.worker'
    proxy_worker_filename = (settings.PROXY_TO_WORKER_FILENAME or worker_target_basename) + '.js'

  target_contents = worker_js_script(proxy_worker_filename)
  write_file(target, target_contents)


def worker_js_script(proxy_worker_filename):
  web_gl_client_src = read_file(utils.path_from_root('src/webGLClient.js'))
  proxy_client_src = shared.read_and_preprocess(utils.path_from_root('src/proxyClient.js'), expand_macros=True)
  if not os.path.dirname(proxy_worker_filename):
    proxy_worker_filename = './' + proxy_worker_filename
  proxy_client_src = do_replace(proxy_client_src, '<<< filename >>>', proxy_worker_filename)
  return web_gl_client_src + '\n' + proxy_client_src


def find_library(lib, lib_dirs):
  for lib_dir in lib_dirs:
    path = os.path.join(lib_dir, lib)
    if os.path.isfile(path):
      logger.debug('found library "%s" at %s', lib, path)
      return path
  return None


def process_libraries(state, linker_inputs, embind_emit_tsd):
  new_flags = []
  libraries = []
  suffixes = STATICLIB_ENDINGS + DYNAMICLIB_ENDINGS
  system_libs_map = system_libs.Library.get_usable_variations()

  # Find library files
  for i, flag in state.link_flags:
    if not flag.startswith('-l'):
      new_flags.append((i, flag))
      continue
    lib = removeprefix(flag, '-l')

    logger.debug('looking for library "%s"', lib)

    js_libs, native_lib = building.map_to_js_libs(lib, embind_emit_tsd)
    if js_libs is not None:
      libraries += [(i, js_lib) for js_lib in js_libs]
      # If native_lib is returned then include it in the link
      # via forced_stdlibs.
      if native_lib:
        state.forced_stdlibs.append(native_lib)
      continue

    # We don't need to resolve system libraries to absolute paths here, we can just
    # let wasm-ld handle that.  However, we do want to map to the correct variant.
    # For example we map `-lc` to `-lc-mt` if we are building with threading support.
    if 'lib' + lib in system_libs_map:
      lib = system_libs_map['lib' + lib].get_link_flag()
      new_flags.append((i, lib))
      continue

    if building.map_and_apply_to_settings(lib):
      continue

    path = None
    for suff in suffixes:
      name = 'lib' + lib + suff
      path = find_library(name, state.lib_dirs)
      if path:
        break

    if path:
      linker_inputs.append((i, path))
      continue

    new_flags.append((i, flag))

  settings.JS_LIBRARIES += libraries

  # At this point processing JS_LIBRARIES is finished, no more items will be added to it.
  # Sort the input list from (order, lib_name) pairs to a flat array in the right order.
  settings.JS_LIBRARIES.sort(key=lambda lib: lib[0])
  settings.JS_LIBRARIES = [lib[1] for lib in settings.JS_LIBRARIES]
  state.link_flags = new_flags

  for _, f in linker_inputs:
    if building.is_ar(f):
      ensure_archive_index(f)


class ScriptSource:
  def __init__(self):
    self.src = None # if set, we have a script to load with a src attribute
    self.inline = None # if set, we have the contents of a script to write inline in a script

  def un_src(self):
    """Use this if you want to modify the script and need it to be inline."""
    if self.src is None:
      return
    quoted_src = quote(self.src)
    if settings.EXPORT_ES6:
      self.inline = f'''
        import("./{quoted_src}").then(exports => exports.default(Module))
      '''
    else:
      self.inline = f'''
            var script = document.createElement('script');
            script.src = "{quoted_src}";
            document.body.appendChild(script);
      '''
    self.src = None

  def replacement(self):
    """Returns the script tag to replace the {{{ SCRIPT }}} tag in the target"""
    assert (self.src or self.inline) and not (self.src and self.inline)
    if self.src:
      quoted_src = quote(self.src)
      if settings.EXPORT_ES6:
        return f'''
        <script type="module">
          import initModule from "./{quoted_src}";
          initModule(Module);
        </script>
        '''
      else:
        return f'<script async type="text/javascript" src="{quoted_src}"></script>'
    else:
      return '<script>\n%s\n</script>' % self.inline


def is_valid_abspath(options, path_name):
  # Any path that is underneath the emscripten repository root must be ok.
  if utils.path_from_root().replace('\\', '/') in path_name.replace('\\', '/'):
    return True

  def in_directory(root, child):
    # make both path absolute
    root = os.path.realpath(root)
    child = os.path.realpath(child)

    # return true, if the common prefix of both is equal to directory
    # e.g. /a/b/c/d.rst and directory is /a/b, the common prefix is /a/b
    return os.path.commonprefix([root, child]) == root

  for valid_abspath in options.valid_abspaths:
    if in_directory(valid_abspath, path_name):
      return True
  return False


def parse_symbol_list_file(contents):
  """Parse contents of one-symbol-per-line response file.  This format can by used
  with, for example, -sEXPORTED_FUNCTIONS=@filename and avoids the need for any
  kind of quoting or escaping.
  """
  values = contents.splitlines()
  return [v.strip() for v in values]


def parse_value(text, expected_type):
  # Note that using response files can introduce whitespace, if the file
  # has a newline at the end. For that reason, we rstrip() in relevant
  # places here.
  def parse_string_value(text):
    first = text[0]
    if first == "'" or first == '"':
      text = text.rstrip()
      assert text[-1] == text[0] and len(text) > 1, 'unclosed opened quoted string. expected final character to be "%s" and length to be greater than 1 in "%s"' % (text[0], text)
      return text[1:-1]
    return text

  def parse_string_list_members(text):
    sep = ','
    values = text.split(sep)
    result = []
    index = 0
    while True:
      current = values[index].lstrip() # Cannot safely rstrip for cases like: "HERE-> ,"
      if not len(current):
        exit_with_error('string array should not contain an empty value')
      first = current[0]
      if not (first == "'" or first == '"'):
        result.append(current.rstrip())
      else:
        start = index
        while True: # Continue until closing quote found
          if index >= len(values):
            exit_with_error("unclosed quoted string. expected final character to be '%s' in '%s'" % (first, values[start]))
          new = values[index].rstrip()
          if new and new[-1] == first:
            if start == index:
              result.append(current.rstrip()[1:-1])
            else:
              result.append((current + sep + new)[1:-1])
            break
          else:
            current += sep + values[index]
            index += 1

      index += 1
      if index >= len(values):
        break
    return result

  def parse_string_list(text):
    text = text.rstrip()
    if text and text[0] == '[':
      if text[-1] != ']':
        exit_with_error('unclosed opened string list. expected final character to be "]" in "%s"' % (text))
      text = text[1:-1]
    if text.strip() == "":
      return []
    return parse_string_list_members(text)

  if expected_type == list or (text and text[0] == '['):
    # if json parsing fails, we fall back to our own parser, which can handle a few
    # simpler syntaxes
    try:
      return json.loads(text)
    except ValueError:
      return parse_string_list(text)

  if expected_type == float:
    try:
      return float(text)
    except ValueError:
      pass

  try:
    if text.startswith('0x'):
      base = 16
    else:
      base = 10
    return int(text, base)
  except ValueError:
    return parse_string_value(text)


def validate_arg_level(level_string, max_level, err_msg, clamp=False):
  try:
    level = int(level_string)
  except ValueError:
    exit_with_error(err_msg)
  if clamp:
    if level > max_level:
      logger.warning("optimization level '-O" + level_string + "' is not supported; using '-O" + str(max_level) + "' instead")
      level = max_level
  if not 0 <= level <= max_level:
    exit_with_error(err_msg)
  return level


def is_int(s):
  try:
    int(s)
    return True
  except ValueError:
    return False


@ToolchainProfiler.profile()
def main(args):
  start_time = time.time()
  ret = run(args)
  logger.debug('total time: %.2f seconds', (time.time() - start_time))
  return ret


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except KeyboardInterrupt:
    logger.debug('KeyboardInterrupt')
    sys.exit(1)
