# Copyright 2013 The Emscripten Authors.  All rights reserved.
# Emscripten is available under two separate licenses, the MIT license and the
# University of Illinois/NCSA Open Source License.  Both these licenses can be
# found in the LICENSE file.

import hashlib
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import unittest
from pathlib import Path
from functools import wraps

if __name__ == '__main__':
  raise Exception('do not run this file directly; do something like: test/runner')

from tools.shared import PIPE
from tools.shared import EMCC, EMAR, FILE_PACKAGER
from tools.utils import WINDOWS, MACOS, write_file, delete_file
from tools import shared, building, config, webassembly
import common
from common import RunnerCore, path_from_root, requires_native_clang, test_file, create_file
from common import skip_if, needs_dylink, no_windows, no_mac, is_slow_test, parameterized
from common import env_modify, with_env_modify, disabled, node_pthreads, also_with_wasm_bigint
from common import read_file, read_binary, requires_v8, requires_node, requires_node_canary, compiler_for, crossplatform
from common import with_both_sjlj, also_with_standalone_wasm, can_do_standalone
from common import NON_ZERO, WEBIDL_BINDER, EMBUILDER, PYTHON
import clang_native

# decorators for limiting which modes a test can run in

logger = logging.getLogger("test_core")


def wasm_simd(f):
  @wraps(f)
  def decorated(self, *args, **kwargs):
    self.require_simd()
    if self.get_setting('MEMORY64') == 2:
      self.skipTest('https://github.com/WebAssembly/binaryen/issues/4638')
    if not self.is_wasm():
      self.skipTest('wasm2js only supports MVP for now')
    if '-O3' in self.emcc_args:
      self.skipTest('SIMD tests are too slow with -O3 in the new LLVM pass manager, https://github.com/emscripten-core/emscripten/issues/13427')
    self.emcc_args.append('-msimd128')
    self.emcc_args.append('-fno-lax-vector-conversions')
    self.v8_args.append('--experimental-wasm-simd')
    f(self, *args, **kwargs)
  return decorated


def wasm_relaxed_simd(f):
  def decorated(self):
    if self.get_setting('MEMORY64') == 2:
      self.skipTest('https://github.com/WebAssembly/binaryen/issues/4638')
    # We don't actually run any tests yet, so don't require any engines.
    if not self.is_wasm():
      self.skipTest('wasm2js only supports MVP for now')
    self.emcc_args.append('-mrelaxed-simd')
    f(self)
  return decorated


def needs_non_trapping_float_to_int(f):
  def decorated(self):
    if not self.is_wasm():
      self.skipTest('wasm2js only supports MVP for now')
    f(self)
  return decorated


# without EMTEST_ALL_ENGINES set we only run tests in a single VM by
# default. in some tests we know that cross-VM differences may happen and
# so are worth testing, and they should be marked with this decorator
def all_engines(f):
  def decorated(self):
    old = self.use_all_engines
    self.use_all_engines = True
    self.set_setting('ENVIRONMENT', 'web,node,shell')
    try:
      f(self)
    finally:
      self.use_all_engines = old
  return decorated


# Tests exception handling / setjmp/longjmp handling in Emscripten EH/SjLj mode
# and if possible, new wasm EH/SjLj mode. This tests two combinations:
# - Emscripten EH + Emscripten SjLj
# - Wasm EH + Wasm SjLj
def with_both_eh_sjlj(f):
  assert callable(f)

  def metafunc(self, is_native):
    if is_native:
      # Wasm EH is currently supported only in wasm backend and V8
      if not self.is_wasm():
        self.skipTest('wasm2js does not support wasm EH/SjLj')
      self.require_wasm_eh()
      # FIXME Temporarily disabled. Enable this later when the bug is fixed.
      if '-fsanitize=address' in self.emcc_args:
        self.skipTest('Wasm EH does not work with asan yet')
      self.emcc_args.append('-fwasm-exceptions')
      self.set_setting('SUPPORT_LONGJMP', 'wasm')
      f(self)
    else:
      self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
      self.set_setting('SUPPORT_LONGJMP', 'emscripten')
      # DISABLE_EXCEPTION_CATCHING=0 exports __cxa_can_catch and
      # __cxa_is_pointer_type, so if we don't build in C++ mode, wasm-ld will
      # error out because libc++abi is not included. See
      # https://github.com/emscripten-core/emscripten/pull/14192 for details.
      self.set_setting('DEFAULT_TO_CXX')
      f(self)

  metafunc._parameterize = {'': (False,),
                            'wasm': (True,)}
  return metafunc


def no_wasm2js(note=''):
  assert not callable(note)

  def decorated(f):
    return skip_if(f, 'is_wasm2js', note)
  return decorated


def no_wasm64(note=''):
  assert not callable(note)

  def decorated(f):
    return skip_if(f, 'is_wasm64', note)
  return decorated


def also_with_noderawfs(func):
  assert callable(func)

  def metafunc(self, rawfs):
    if rawfs:
      self.require_node()
      self.emcc_args += ['-DNODERAWFS']
      self.set_setting('NODERAWFS')
    func(self)

  metafunc._parameterize = {'': (False,),
                            'rawfs': (True,)}
  return metafunc


def also_with_wasmfs(func):
  def decorated(self):
    func(self)
    if self.get_setting('WASMFS'):
      # Nothing more to test.
      return
    print('wasmfs')
    if self.get_setting('STANDALONE_WASM'):
      self.skipTest("test currently cannot run both with WASMFS and STANDALONE_WASM")
    self.set_setting('WASMFS')
    self.emcc_args = self.emcc_args.copy() + ['-DWASMFS']
    func(self)
  return decorated


# Similar to also_with_wasmfs, but also enables the full JS API
def also_with_wasmfs_js(func):
  def decorated(self):
    func(self)
    print('wasmfs')
    if self.get_setting('STANDALONE_WASM'):
      self.skipTest("test currently cannot run both with WASMFS and STANDALONE_WASM")
    self.set_setting('WASMFS')
    self.set_setting('FORCE_FILESYSTEM')
    self.emcc_args = self.emcc_args.copy() + ['-DWASMFS']
    func(self)
  return decorated


def with_asyncify_and_jspi(f):
  assert callable(f)

  def metafunc(self, jspi):
    if jspi:
      self.set_setting('ASYNCIFY', 2)
      self.require_jspi()
      if not self.is_wasm():
        self.skipTest('wasm2js does not support WebAssembly.Suspender yet')
      # emcc warns about stack switching being experimental, and we build with
      # warnings-as-errors, so disable that warning
      self.emcc_args += ['-Wno-experimental']
      f(self)
    else:
      self.set_setting('ASYNCIFY')
      f(self)

  metafunc._parameterize = {'': (False,),
                            'jspi': (True,)}
  return metafunc


def no_optimize(note=''):
  assert not callable(note)

  def decorator(func):
    assert callable(func)

    def decorated(self):
      if self.is_optimizing():
        self.skipTest(note)
      func(self)
    return decorated
  return decorator


def needs_make(note=''):
  assert not callable(note)
  if WINDOWS:
    return unittest.skip('Tool not available on Windows bots (%s)' % note)
  return lambda f: f


def no_asan(note):
  assert not callable(note)

  def decorator(f):
    assert callable(f)

    @wraps(f)
    def decorated(self, *args, **kwargs):
      if '-fsanitize=address' in self.emcc_args:
        self.skipTest(note)
      f(self, *args, **kwargs)
    return decorated
  return decorator


def no_lsan(note):
  assert not callable(note)

  def decorator(f):
    assert callable(f)

    @wraps(f)
    def decorated(self, *args, **kwargs):
      if '-fsanitize=leak' in self.emcc_args:
        self.skipTest(note)
      f(self, *args, **kwargs)
    return decorated
  return decorator


def no_ubsan(note):
  assert not callable(note)

  def decorator(f):
    assert callable(f)

    @wraps(f)
    def decorated(self, *args, **kwargs):
      if '-fsanitize=undefined' in self.emcc_args:
        self.skipTest(note)
      f(self, *args, **kwargs)
    return decorated
  return decorator


def no_sanitize(note):
  assert not callable(note)

  def decorator(f):
    assert callable(f)

    @wraps(f)
    def decorated(self, *args, **kwargs):
      if any(a.startswith('-fsanitize=') for a in self.emcc_args):
        self.skipTest(note)
      f(self, *args, **kwargs)
    return decorated
  return decorator


def no_wasmfs(note):
  assert not callable(note)

  def decorator(f):
    assert callable(f)

    @wraps(f)
    def decorated(self, *args, **kwargs):
      if self.get_setting('WASMFS'):
        self.skipTest(note)
      f(self, *args, **kwargs)
    return decorated
  return decorator


def make_no_decorator_for_setting(name):
  def outer_decorator(note):
    assert not callable(note)

    def decorator(f):
      assert callable(f)

      @wraps(f)
      def decorated(self, *args, **kwargs):
        if (name + '=1') in self.emcc_args or self.get_setting(name):
          self.skipTest(note)
        f(self, *args, **kwargs)
      return decorated
    return decorator
  return outer_decorator


no_minimal_runtime = make_no_decorator_for_setting('MINIMAL_RUNTIME')
no_safe_heap = make_no_decorator_for_setting('SAFE_HEAP')


def is_sanitizing(args):
  return '-fsanitize=' in str(args)


class TestCoreBase(RunnerCore):
  def is_wasm2js(self):
    return self.get_setting('WASM') == 0

  def is_wasm64(self):
    return self.get_setting('MEMORY64')

  # A simple check whether the compiler arguments cause optimization.
  def is_optimizing(self):
    return '-O' in str(self.emcc_args) and '-O0' not in self.emcc_args

  def should_use_closure(self):
    # Don't run closure in all test modes, just a couple, since it slows
    # the tests down quite a bit.
    required = ('-O2', '-Os')
    prohibited = ('-g', '--profiling')
    return all(f not in self.emcc_args for f in prohibited) and any(f in self.emcc_args for f in required)

  # Use closure in some tests for some additional coverage
  def maybe_closure(self):
    if '--closure=1' not in self.emcc_args and self.should_use_closure():
      self.emcc_args += ['--closure=1']
      logger.debug('using closure compiler..')
      return True
    return False

  def assertStartswith(self, output, prefix):
    self.assertEqual(prefix, output[:len(prefix)])

  def verify_in_strict_mode(self, filename):
    js = read_file(filename)
    filename += '.strict.js'
    write_file(filename, '"use strict";\n' + js)
    self.run_js(filename)

  def do_core_test(self, testname, **kwargs):
    self.do_run_in_out_file_test(Path('core', testname), **kwargs)

  def get_bullet_library(self, use_cmake):
    if use_cmake:
      configure_commands = ['cmake', '.']
      configure_args = ['-DBUILD_DEMOS=OFF', '-DBUILD_EXTRAS=OFF', '-DUSE_GLUT=OFF',
                        '-DCMAKE_CXX_STANDARD=14']
      # Depending on whether 'configure' or 'cmake' is used to build, Bullet
      # places output files in different directory structures.
      generated_libs = [Path('src/BulletDynamics/libBulletDynamics.a'),
                        Path('src/BulletCollision/libBulletCollision.a'),
                        Path('src/LinearMath/libLinearMath.a')]
    else:
      configure_commands = ['sh', './configure']
      # Force a nondefault --host= so that the configure script will interpret
      # that we are doing cross-compilation
      # and skip attempting to run the generated executable with './a.out',
      # which would fail since we are building a .js file.
      configure_args = ['--disable-shared', '--host=i686-pc-linux-gnu',
                        '--disable-demos', '--disable-dependency-tracking']
      generated_libs = [Path('src/.libs/libBulletDynamics.a'),
                        Path('src/.libs/libBulletCollision.a'),
                        Path('src/.libs/libLinearMath.a')]

    return self.get_library('third_party/bullet', generated_libs,
                            configure=configure_commands,
                            configure_args=configure_args,
                            cache_name_extra=configure_commands[0])

  @also_with_standalone_wasm()
  @also_with_wasmfs
  def test_hello_world(self):
    self.do_core_test('test_hello_world.c')

  def test_wasm_synchronous_compilation(self):
    self.set_setting('STRICT_JS')
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    self.do_core_test('test_hello_world.c')

  @also_with_standalone_wasm()
  def test_hello_argc(self):
    self.do_core_test('test_hello_argc.c', args=['hello', 'world'])

  @node_pthreads
  def test_hello_argc_pthreads(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_hello_argc.c', args=['hello', 'world'])

  @also_with_wasmfs
  def test_intvars(self):
    self.do_core_test('test_intvars.cpp')

  @also_with_wasmfs
  def test_sintvars(self):
    self.do_core_test('test_sintvars.c')

  def test_int53(self):
    if common.EMTEST_REBASELINE:
      self.run_process([EMCC, test_file('core/test_int53.c'), '-o', 'a.js', '-DGENERATE_ANSWERS'] + self.emcc_args)
      ret = self.run_process(config.NODE_JS + ['a.js'], stdout=PIPE).stdout
      write_file(test_file('core/test_int53.out'), ret)
    else:
      self.do_core_test('test_int53.c', interleaved_output=False)

  def test_int53_convertI32PairToI53Checked(self):
    if common.EMTEST_REBASELINE:
      self.run_process([EMCC, test_file('core/test_convertI32PairToI53Checked.cpp'), '-o', 'a.js', '-DGENERATE_ANSWERS'] + self.emcc_args)
      ret = self.run_process(config.NODE_JS + ['a.js'], stdout=PIPE).stdout
      write_file(test_file('core/test_convertI32PairToI53Checked.out'), ret)
    else:
      self.do_core_test('test_convertI32PairToI53Checked.cpp', interleaved_output=False)

  def test_i64(self):
    self.do_core_test('test_i64.c')

  def test_i64_2(self):
    self.do_core_test('test_i64_2.cpp')

  def test_i64_3(self):
    self.do_core_test('test_i64_3.cpp')

  def test_i64_4(self):
    # stuff that also needs sign corrections

    self.do_core_test('test_i64_4.c')

  def test_i64_b(self):
    self.do_core_test('test_i64_b.cpp')

  def test_i64_cmp(self):
    self.do_core_test('test_i64_cmp.cpp')

  def test_i64_cmp2(self):
    self.do_core_test('test_i64_cmp2.c')

  def test_i64_double(self):
    self.do_core_test('test_i64_double.cpp')

  def test_i64_umul(self):
    self.do_core_test('test_i64_umul.c')

  @also_with_standalone_wasm()
  @no_ubsan('contains UB')
  def test_i64_precise(self):
    self.do_core_test('test_i64_precise.c')

  def test_i64_precise_needed(self):
    self.do_core_test('test_i64_precise_needed.c')

  def test_i64_llabs(self):
    self.do_core_test('test_i64_llabs.c')

  def test_i64_zextneg(self):
    self.do_core_test('test_i64_zextneg.c')

  def test_i64_7z(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_i64_7z.c', args=['hallo'])

  def test_i64_i16(self):
    self.do_core_test('test_i64_i16.c')

  def test_i64_qdouble(self):
    self.do_core_test('test_i64_qdouble.c')

  def test_i64_varargs(self):
    self.do_core_test('test_i64_varargs.c', args='waka fleefl asdfasdfasdfasdf'.split())

  @no_wasm2js('wasm_bigint')
  @requires_node
  def test_i64_invoke_bigint(self):
    self.set_setting('WASM_BIGINT')
    self.emcc_args += ['-fexceptions']
    self.node_args += shared.node_bigint_flags()
    self.do_core_test('test_i64_invoke_bigint.cpp')

  def test_vararg_copy(self):
    self.do_run_in_out_file_test('va_arg/test_va_copy.c')

  def test_llvm_fabs(self):
    self.do_core_test('test_llvm_fabs.c')

  def test_double_varargs(self):
    self.do_core_test('test_double_varargs.c')

  def test_trivial_struct_varargs(self):
    self.do_core_test('test_trivial_struct_varargs.c')

  def test_struct_varargs(self):
    self.do_core_test('test_struct_varargs.c')

  def test_zero_struct_varargs(self):
    self.do_core_test('test_zero_struct_varargs.c')

  def zzztest_nested_struct_varargs(self):
    self.do_core_test('test_nested_struct_varargs.c')

  def test_i32_mul_precise(self):
    self.do_core_test('test_i32_mul_precise.c')

  def test_i16_emcc_intrinsic(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_i16_emcc_intrinsic.c')

  def test_double_i64_conversion(self):
    self.do_core_test('test_double_i64_conversion.c')

  def test_float32_precise(self):
    self.do_core_test('test_float32_precise.c')

  def test_negative_zero(self):
    self.do_core_test('test_negative_zero.c')

  def test_literal_negative_zero(self):
    self.do_core_test('test_literal_negative_zero.c')

  @also_with_standalone_wasm()
  def test_bswap64(self):
    self.do_core_test('test_bswap64.cpp')

  def test_sha1(self):
    self.do_runf(test_file('sha1.c'), 'SHA1=15dd99a1991e0b3826fede3deffc1feba42278e6')

  def test_core_types(self):
    self.do_runf(test_file('core/test_core_types.c'))

  def test_cube2md5(self):
    self.emcc_args += ['--embed-file', 'cube2md5.txt']
    shutil.copyfile(test_file('cube2md5.txt'), 'cube2md5.txt')
    self.do_run_from_file(test_file('cube2md5.cpp'), test_file('cube2md5.ok'), assert_returncode=NON_ZERO)

  @also_with_standalone_wasm()
  @needs_make('make')
  def test_cube2hash(self):
    # A good test of i64 math
    self.do_run('// empty file', 'Usage: hashstring <seed>',
                libraries=self.get_library('third_party/cube2hash', ['libcube2hash.a'], configure=None),
                includes=[test_file('third_party/cube2hash')], assert_returncode=NON_ZERO)

    for text, output in [('fleefl', '892BDB6FD3F62E863D63DA55851700FDE3ACF30204798CE9'),
                         ('fleefl2', 'AA2CC5F96FC9D540CA24FDAF1F71E2942753DB83E8A81B61'),
                         ('64bitisslow', '64D8470573635EC354FEE7B7F87C566FCAF1EFB491041670')]:
      self.do_run('src.js', 'hash value: ' + output, args=[text], no_build=True)

  def test_unaligned(self):
    self.skipTest('LLVM marks the reads of s as fully aligned, making this test invalid')
    src = r'''
      #include <stdio.h>

      struct S {
        double x;
        int y;
      };

      int main() {
        // the 64-bit value here will not be 8-byte aligned
        S s0[3] = { {0x12a751f430142, 22}, {0x17a5c85bad144, 98}, {1, 1}};
        char buffer[10*sizeof(S)];
        int b = int(buffer);
        S *s = (S*)(b + 4-b%8);
        s[0] = s0[0];
        s[1] = s0[1];
        s[2] = s0[2];

        printf("*%d : %d : %d\n", sizeof(S), ((unsigned long)&s[0]) % 8 != ((unsigned long)&s[1]) % 8,
                                             ((unsigned long)&s[1]) - ((unsigned long)&s[0]));
        s[0].x++;
        s[0].y++;
        s[1].x++;
        s[1].y++;
        printf("%.1f,%d,%.1f,%d\n", s[0].x, s[0].y, s[1].x, s[1].y);
        return 0;
      }
      '''

    # TODO: A version of this with int64s as well

    self.do_run(src, '*12 : 1 : 12\n328157500735811.0,23,416012775903557.0,99\n')

    return # TODO: continue to the next part here

    # Test for undefined behavior in C. This is not legitimate code, but does exist

    src = r'''
      #include <stdio.h>

      int main()
      {
        int x[10];
        char *p = (char*)&x[0];
        p++;
        short *q = (short*)p;
        *q = 300;
        printf("*%d:%ld*\n", *q, ((long)q)%2);
        int *r = (int*)p;
        *r = 515559;
        printf("*%d*\n", *r);
        long long *t = (long long*)p;
        *t = 42949672960;
        printf("*%lld*\n", *t);
        return 0;
      }
    '''

    try:
      self.do_run(src, '*300:1*\n*515559*\n*42949672960*\n')
    except Exception as e:
      assert 'must be aligned' in str(e), e # expected to fail without emulation

  def test_align64(self):
    src = r'''
      #include <stdio.h>

      // inspired by poppler

      enum Type {
        A = 10,
        B = 20
      };

      struct Object {
        Type type;
        union {
          int intg;
          double real;
          char *name;
        };
      };

      struct Principal {
        double x;
        Object a;
        double y;
      };

      int main(int argc, char **argv) {
        int base = argc-1;
        Object o[10];
        printf("%zu,%zu\n", sizeof(Object), sizeof(Principal));
        printf("%ld,%ld,%ld,%ld\n", (long)&o[base].type - (long)o, (long)&o[base].intg - (long)o, (long)&o[base].real - (long)o, (long)&o[base].name - (long)o);
        printf("%ld,%ld,%ld,%ld\n", (long)&o[base+1].type - (long)o, (long)&o[base+1].intg - (long)o, (long)&o[base+1].real - (long)o, (long)&o[base+1].name - (long)o);
        Principal p, q;
        p.x = p.y = q.x = q.y = 0;
        p.a.type = A;
        p.a.real = 123.456;
        *(&q.a) = p.a;
        printf("%.2f,%d,%.2f,%.2f : %.2f,%d,%.2f,%.2f\n", p.x, p.a.type, p.a.real, p.y, q.x, q.a.type, q.a.real, q.y);
        return 0;
      }
    '''

    self.do_run(src, '''16,32
0,8,8,8
16,24,24,24
0.00,10,123.46,0.00 : 0.00,10,123.46,0.00
''')

  @no_asan('asan errors on corner cases we check')
  @no_lsan('lsan errors on corner cases we check')
  def test_aligned_alloc(self):
    self.do_runf(test_file('test_aligned_alloc.c'), '',
                 emcc_args=['-Wno-non-power-of-two-alignment'])

  def test_unsigned(self):
    src = '''
      #include <stdio.h>
      const signed char cvals[2] = { -1, -2 }; // compiler can store this is a string, so -1 becomes \\FF, and needs re-signing
      int main()
      {
        {
          unsigned char x = 200;
          printf("*%d*\\n", x);
          unsigned char y = -22;
          printf("*%d*\\n", y);
        }

        int varey = 100;
        unsigned int MAXEY = -1, MAXEY2 = -77;
        printf("*%u,%d,%u*\\n", MAXEY, varey >= MAXEY, MAXEY2); // 100 >= -1? not in unsigned!

        int y = cvals[0];
        printf("*%d,%d,%d,%d*\\n", cvals[0], cvals[0] < 0, y, y < 0);
        y = cvals[1];
        printf("*%d,%d,%d,%d*\\n", cvals[1], cvals[1] < 0, y, y < 0);

        // zext issue - see mathop in jsifier
        unsigned char x8 = -10;
        unsigned long hold = 0;
        hold += x8;
        int y32 = hold+50;
        printf("*%lu,%d*\\n", hold, y32);

        // Comparisons
        x8 = 0;
        for (int i = 0; i < 254; i++) x8++; // make it an actual 254 in JS - not a -2
        printf("*%d,%d*\\n", x8+1 == 0xff, x8+1 != 0xff); // 0xff may be '-1' in the bitcode

        return 0;
      }
    '''
    self.do_run(src, '*4294967295,0,4294967219*\n*-1,1,-1,1*\n*-2,1,-2,1*\n*246,296*\n*1,0*')

    self.emcc_args.append('-Wno-constant-conversion')
    src = '''
      #include <stdio.h>
      int main()
      {
        {
          unsigned char x;
          unsigned char *y = &x;
          *y = -1;
          printf("*%d*\\n", x);
        }
        {
          unsigned short x;
          unsigned short *y = &x;
          *y = -1;
          printf("*%d*\\n", x);
        }
        /*{ // This case is not checked. The hint for unsignedness is just the %u in printf, and we do not analyze that
          unsigned int x;
          unsigned int *y = &x;
          *y = -1;
          printf("*%u*\\n", x);
        }*/
        {
          char x;
          char *y = &x;
          *y = 255;
          printf("*%d*\\n", x);
        }
        {
          char x;
          char *y = &x;
          *y = 65535;
          printf("*%d*\\n", x);
        }
        {
          char x;
          char *y = &x;
          *y = 0xffffffff;
          printf("*%d*\\n", x);
        }
        return 0;
      }
    '''
    self.do_run(src, '*255*\n*65535*\n*-1*\n*-1*\n*-1*')

  def test_bitfields(self):
    self.do_core_test('test_bitfields.c')

  def test_floatvars(self):
    self.do_core_test('test_floatvars.cpp')

  def test_closebitcasts(self):
    self.do_core_test('closebitcasts.c')

  def test_fast_math(self):
    self.emcc_args += ['-ffast-math']
    self.do_core_test('test_fast_math.c', args=['5', '6', '8'])

  def test_zerodiv(self):
    self.do_core_test('test_zerodiv.c')

  def test_zero_multiplication(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_zero_multiplication.c')

  def test_isnan(self):
    self.do_core_test('test_isnan.c')

  def test_globaldoubles(self):
    self.do_core_test('test_globaldoubles.c')

  def test_math(self):
    self.do_core_test('test_math.c')

  def test_erf(self):
    self.do_core_test('test_erf.c')

  def test_math_hyperbolic(self):
    self.do_core_test('test_math_hyperbolic.c')

  def test_math_lgamma(self):
    self.do_run_in_out_file_test('math/lgamma.c', assert_returncode=NON_ZERO)

  def test_math_fmodf(self):
    self.do_run_in_out_file_test('math/fmodf.c')

  def test_frexp(self):
    self.do_core_test('test_frexp.c')

  def test_rounding(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_rounding.c')

  def test_fcvt(self):
    self.do_core_test('test_fcvt.cpp')

  def test_llrint(self):
    self.do_core_test('test_llrint.c')

  def test_getgep(self):
    # Generated code includes getelementptr (getelementptr, 0, 1), i.e., GEP as the first param to GEP
    self.do_core_test('test_getgep.c')

  def test_multiply_defined_symbols(self):
    create_file('a1.c', 'int f() { return 1; }')
    create_file('a2.c', 'void x() {}')
    create_file('b1.c', 'int f() { return 2; }')
    create_file('b2.c', 'void y() {}')
    create_file('main.c', r'''
      #include <stdio.h>
      int f();
      int main() {
        printf("result: %d\n", f());
        return 0;
      }
    ''')

    self.emcc('a1.c', ['-c'])
    self.emcc('a2.c', ['-c'])
    self.emcc('b1.c', ['-c'])
    self.emcc('b2.c', ['-c'])
    self.emcc('main.c', ['-c'])

    building.emar('cr', 'liba.a', ['a1.o', 'a2.o'])
    building.emar('cr', 'libb.a', ['b1.o', 'b2.o'])

    # Add -Wno-deprecated to avoid warning about bitcode linking in the LTO
    # version of this test.
    self.run_process([EMCC, '-r', '-o', 'all.o', 'main.o', 'liba.a', 'libb.a',
                      '-Wno-deprecated'] + self.get_emcc_args())

    self.emcc('all.o', output_filename='all.js')
    self.do_run('all.js', 'result: 1', no_build=True)

  def test_if(self):
    self.do_core_test('test_if.c')

  def test_if_else(self):
    self.do_core_test('test_if_else.c')

  def test_loop(self):
    self.do_core_test('test_loop.c')

  def test_stack(self):
    self.set_setting('INLINING_LIMIT')
    # some extra coverage in all test suites for stack checks
    self.set_setting('STACK_OVERFLOW_CHECK', 2)

    self.do_core_test('test_stack.c')

  def test_stack_align(self):
    src = test_file('core/test_stack_align.cpp')

    def test():
      self.do_runf(src, ['''align 4: 0
align 8: 0
align 16: 0
align 32: 0
base align: 0, 0, 0, 0'''])

    test()

  @no_asan('stack size is too low for asan to work properly')
  def test_stack_placement(self):
    self.set_setting('STACK_SIZE', 1024)
    self.do_core_test('test_stack_placement.c')
    self.set_setting('GLOBAL_BASE', '100kb')
    self.do_core_test('test_stack_placement.c')

  @no_sanitize('sanitizers do not yet support dynamic linking')
  @no_wasm2js('MAIN_MODULE support')
  def test_stack_placement_pic(self):
    self.set_setting('STACK_SIZE', 1024)
    self.set_setting('MAIN_MODULE')
    self.do_core_test('test_stack_placement.c')
    self.set_setting('GLOBAL_BASE', '100kb')
    self.do_core_test('test_stack_placement.c')

  def test_strings(self):
    self.do_core_test('test_strings.c', args=['wowie', 'too', '74'])

  def test_strcmp_uni(self):
    self.do_core_test('test_strcmp_uni.c')

  def test_strndup(self):
    self.do_core_test('test_strndup.c')

  def test_errar(self):
    self.do_core_test('test_errar.c')

  def test_mainenv(self):
    self.do_core_test('test_mainenv.c')

  def test_funcs(self):
    self.do_core_test('test_funcs.c')

  def test_structs(self):
    self.do_core_test('test_structs.c')

  gen_struct_src = '''
        #include <stdio.h>
        #include <stdlib.h>
        #include "emscripten.h"

        struct S
        {
          int x, y;
        };
        int main()
        {
          S* a = {{gen_struct}};
          a->x = 51; a->y = 62;
          printf("*%d,%d*\\n", a->x, a->y);
          {{del_struct}}(a);
          return 0;
        }
  '''

  def test_mallocstruct(self):
    self.do_run(self.gen_struct_src.replace('{{gen_struct}}', '(S*)malloc(sizeof(S))').replace('{{del_struct}}', 'free'), '*51,62*')

  @no_asan('ASan does not support custom memory allocators')
  @no_lsan('LSan does not support custom memory allocators')
  @parameterized({
    'normal': [],
    'memvalidate': ['-DEMMALLOC_MEMVALIDATE'],
    'memvalidate_verbose': ['-DEMMALLOC_MEMVALIDATE', '-DEMMALLOC_VERBOSE', '-DRANDOM_ITERS=130'],
  })
  def test_emmalloc(self, *args):
    if '-DEMMALLOC_VERBOSE' in args and self.is_wasm64():
      self.skipTest('EMMALLOC_VERBOSE is not compatible with wasm64')
    # in newer clang+llvm, the internal calls to malloc in emmalloc may be optimized under
    # the assumption that they are external, so like in system_libs.py where we build
    # malloc, we need to disable builtin here too
    self.set_setting('MALLOC', 'none')
    self.emcc_args += [
      '-fno-builtin',
      path_from_root('system/lib/libc/sbrk.c'),
      path_from_root('system/lib/emmalloc.c')
    ]
    self.emcc_args += args
    self.do_run_in_out_file_test(test_file('core/test_emmalloc.c'))

  @no_asan('ASan does not support custom memory allocators')
  @no_lsan('LSan does not support custom memory allocators')
  def test_emmalloc_usable_size(self, *args):
    self.set_setting('MALLOC', 'emmalloc')
    self.do_core_test('test_malloc_usable_size.c', regex=True)

  @no_optimize('output is sensitive to optimization flags, so only test unoptimized builds')
  @no_asan('ASan does not support custom memory allocators')
  @no_lsan('LSan does not support custom memory allocators')
  @no_ubsan('UBSan changes memory consumption')
  def test_emmalloc_memory_statistics(self, *args):
    if self.is_wasm64():
      out_suffix = '64'
    else:
      out_suffix = ''

    self.set_setting('MALLOC', 'emmalloc')
    self.emcc_args += ['-sINITIAL_MEMORY=128MB', '-g'] + list(args)

    self.do_core_test('test_emmalloc_memory_statistics.cpp', out_suffix=out_suffix)

  @no_optimize('output is sensitive to optimization flags, so only test unoptimized builds')
  @no_wasm64('output is sensitive to absolute data size')
  @no_asan('ASan does not support custom memory allocators')
  @no_lsan('LSan does not support custom memory allocators')
  def test_emmalloc_trim(self, *args):
    self.set_setting('MALLOC', 'emmalloc')
    self.emcc_args += ['-sINITIAL_MEMORY=128MB', '-sALLOW_MEMORY_GROWTH', '-sMAXIMUM_MEMORY=2147418112'] + list(args)

    self.do_core_test('test_emmalloc_trim.cpp')

  # Test case against https://github.com/emscripten-core/emscripten/issues/10363
  def test_emmalloc_memalign_corruption(self, *args):
    self.set_setting('MALLOC', 'emmalloc')
    self.do_core_test('emmalloc_memalign_corruption.cpp')

  def test_newstruct(self):
    self.do_run(self.gen_struct_src.replace('{{gen_struct}}', 'new S').replace('{{del_struct}}', 'delete'), '*51,62*')

  def test_addr_of_stacked(self):
    self.do_core_test('test_addr_of_stacked.c')

  def test_globals(self):
    self.do_core_test('test_globals.c')

  def test_linked_list(self):
    self.do_core_test('test_linked_list.c')

  def test_sup(self):
    self.do_run_in_out_file_test(test_file('core/test_sup.cpp'))

  @also_with_standalone_wasm()
  def test_assert(self):
    self.do_core_test('test_assert.cpp', assert_returncode=NON_ZERO)

  def test_wcslen(self):
    self.do_core_test('test_wcslen.c')

  def test_regex(self):
    self.do_core_test('test_regex.c')

  @crossplatform
  @also_with_standalone_wasm(impure=True)
  def test_longjmp_standalone(self):
    self.do_core_test('test_longjmp.c')

  @with_both_sjlj
  def test_longjmp(self):
    self.do_core_test('test_longjmp.c')

  def test_longjmp_with_and_without_exceptions(self):
    # Emscripten SjLj with and without Emscripten EH support
    self.set_setting('SUPPORT_LONGJMP', 'emscripten')
    self.set_setting('DEFAULT_TO_CXX') # See comments on @with_both_eh_sjlj
    for disable_catching in [0, 1]:
      self.set_setting('DISABLE_EXCEPTION_CATCHING', disable_catching)
      self.do_core_test('test_longjmp.c')
    # Wasm SjLj with and without Wasm EH support
    self.clear_setting('DISABLE_EXCEPTION_CATCHING')
    self.set_setting('SUPPORT_LONGJMP', 'wasm')
    if not self.is_wasm():
      self.skipTest('wasm2js does not support wasm EH/SjLj')
    self.require_wasm_eh()
    # FIXME Temporarily disabled. Enable this later when the bug is fixed.
    if '-fsanitize=address' in self.emcc_args:
      self.skipTest('Wasm EH does not work with asan yet')
    self.emcc_args.append('-fwasm-exceptions')
    for arg in ['-fwasm-exceptions', '-fno-exceptions']:
      self.do_core_test('test_longjmp.c', emcc_args=[arg])

  @with_both_sjlj
  def test_longjmp2(self):
    self.do_core_test('test_longjmp2.c')

  @needs_dylink
  @with_both_sjlj
  def test_longjmp2_main_module(self):
    # Test for binaryen regression:
    # https://github.com/WebAssembly/binaryen/issues/2180
    self.set_setting('MAIN_MODULE')
    self.do_core_test('test_longjmp2.c')

  @with_both_sjlj
  def test_longjmp3(self):
    self.do_core_test('test_longjmp3.c')

  @with_both_sjlj
  def test_longjmp4(self):
    self.do_core_test('test_longjmp4.c')

  @with_both_sjlj
  def test_longjmp_funcptr(self):
    self.do_core_test('test_longjmp_funcptr.c')

  @with_both_sjlj
  def test_longjmp_repeat(self):
    self.do_core_test('test_longjmp_repeat.c')

  @with_both_sjlj
  def test_longjmp_stacked(self):
    self.do_core_test('test_longjmp_stacked.c', assert_returncode=NON_ZERO)

  @with_both_sjlj
  def test_longjmp_exc(self):
    self.do_core_test('test_longjmp_exc.c', assert_returncode=NON_ZERO)

  def test_longjmp_throw(self):
    for disable_throw in [0, 1]:
      print(disable_throw)
      self.set_setting('DISABLE_EXCEPTION_CATCHING', disable_throw)
      self.do_core_test('test_longjmp_throw.cpp')

  @with_both_sjlj
  def test_longjmp_unwind(self):
    self.do_core_test('test_longjmp_unwind.c', assert_returncode=NON_ZERO)

  @with_both_sjlj
  def test_longjmp_i64(self):
    self.emcc_args += ['-g']
    self.do_core_test('test_longjmp_i64.c', assert_returncode=NON_ZERO)

  @with_both_sjlj
  def test_siglongjmp(self):
    self.do_core_test('test_siglongjmp.c')

  @with_both_sjlj
  def test_setjmp_many(self):
    src = r'''
      #include <stdio.h>
      #include <setjmp.h>

      int main(int argc, char** argv) {
        jmp_buf buf;
        for (int i = 0; i < NUM; i++) printf("%d\n", setjmp(buf));
        if (argc-- == 1131) longjmp(buf, 11);
        return 0;
      }
    '''
    for num in [1, 5, 20, 1000]:
      print('NUM=%d' % num)
      self.do_run(src.replace('NUM', str(num)), '0\n' * num)

  @with_both_sjlj
  def test_setjmp_many_2(self):
    src = r'''
#include <setjmp.h>
#include <stdio.h>

jmp_buf env;

void luaWork(int d){
    int x;
    printf("d is at %d\n", d);

    longjmp(env, 1);
}

int main()
{
    const int ITERATIONS=25;
    for(int i = 0; i < ITERATIONS; i++){
        if(!setjmp(env)){
            luaWork(i);
        }
    }
    return 0;
}
'''

    self.do_run(src, r'''d is at 24''')

  @with_both_sjlj
  def test_setjmp_noleak(self):
    self.do_runf(test_file('core/test_setjmp_noleak.c'), 'ok.')

  @with_both_sjlj
  def test_setjmp_within_loop(self):
    self.do_core_test('test_setjmp_within_loop.c')

  @with_both_eh_sjlj
  def test_exceptions(self):
    self.set_setting('EXCEPTION_DEBUG')
    self.maybe_closure()
    self.do_run_from_file(test_file('core/test_exceptions.cpp'), test_file('core/test_exceptions_caught.out'))

  def test_exceptions_with_and_without_longjmp(self):
    self.set_setting('EXCEPTION_DEBUG')
    self.maybe_closure()
    # Emscripten EH with and without Emscripten SjLj support
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
    for support_longjmp in [0, 'emscripten']:
      self.set_setting('SUPPORT_LONGJMP', support_longjmp)
      self.do_run_from_file(test_file('core/test_exceptions.cpp'), test_file('core/test_exceptions_caught.out'))
    # Wasm EH with and without Wasm SjLj support
    self.clear_setting('DISABLE_EXCEPTION_CATCHING')
    if not self.is_wasm():
      self.skipTest('wasm2js does not support wasm EH/SjLj')
    self.require_wasm_eh()
    # FIXME Temporarily disabled. Enable this later when the bug is fixed.
    if '-fsanitize=address' in self.emcc_args:
      self.skipTest('Wasm EH does not work with asan yet')
    self.emcc_args.append('-fwasm-exceptions')
    for support_longjmp in [0, 'wasm']:
      self.set_setting('SUPPORT_LONGJMP', support_longjmp)
      self.do_run_from_file(test_file('core/test_exceptions.cpp'), test_file('core/test_exceptions_caught.out'))

  def test_exceptions_off(self):
    self.set_setting('DISABLE_EXCEPTION_CATCHING')
    for support_longjmp in [0, 1]:
      self.set_setting('SUPPORT_LONGJMP', support_longjmp)
      self.do_runf(test_file('core/test_exceptions.cpp'), assert_returncode=NON_ZERO)

  @no_wasmfs('https://github.com/emscripten-core/emscripten/issues/16816')
  @no_asan('TODO: ASan support in minimal runtime')
  def test_exceptions_minimal_runtime(self):
    self.set_setting('EXIT_RUNTIME')
    self.maybe_closure()
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    for support_longjmp in [0, 1]:
      self.set_setting('SUPPORT_LONGJMP', support_longjmp)

      self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
      self.do_run_from_file(test_file('core/test_exceptions.cpp'), test_file('core/test_exceptions_caught.out'))

      self.set_setting('EXCEPTION_DEBUG')
      self.set_setting('DISABLE_EXCEPTION_CATCHING')
      self.do_run_from_file(test_file('core/test_exceptions.cpp'), test_file('core/test_exceptions_uncaught.out'), assert_returncode=NON_ZERO)

  @with_both_eh_sjlj
  def test_exceptions_custom(self):
    self.set_setting('EXCEPTION_DEBUG')
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.maybe_closure()
    src = '''
    #include <iostream>

    class MyException {
    public:
        MyException(){ std::cout << "Construct..."; }
        MyException( const MyException & ) { std::cout << "Copy..."; }
        ~MyException(){ std::cout << "Destruct..."; }
    };

    int function() {
        std::cout << "Throw...";
        throw MyException();
    }

    int function2() {
        return function();
    }

    int main() {
        try {
            function2();
        } catch (MyException & e) {
            std::cout << "Caught...";
        }

        try {
            function2();
        } catch (MyException e) {
            std::cout << "Caught...";
        }

        return 0;
    }
    '''

    self.do_run(src, 'Throw...Construct...Caught...Destruct...Throw...Construct...Copy...Caught...Destruct...Destruct...')

  @with_both_eh_sjlj
  def test_exceptions_2(self):
    for safe in [0, 1]:
      print(safe)
      if safe and '-fsanitize=address' in self.emcc_args:
        # Can't use safe heap with ASan
        continue
      self.set_setting('SAFE_HEAP', safe)
      self.do_core_test('test_exceptions_2.cpp')

  @with_both_eh_sjlj
  def test_exceptions_3(self):
    src = r'''
#include <iostream>
#include <stdexcept>

int main(int argc, char **argv) {
  if (argc != 2) {
    std::cout << "need an arg" << std::endl;
    return 1;
  }

  int arg = argv[1][0] - '0';
  try {
    if (arg == 0) throw "a c string";
    if (arg == 1) throw std::exception();
    if (arg == 2) throw std::runtime_error("Hello");
  } catch(const char * ex) {
    std::cout << "Caught C string: " << ex << std::endl;
  } catch(const std::exception &ex) {
    std::cout << "Caught exception: " << ex.what() << std::endl;
  } catch(...) {
    std::cout << "Caught something else" << std::endl;
  }

  std::cout << "Done.\n";
}
'''

    print('0')
    self.do_run(src, 'Caught C string: a c string\nDone.', args=['0'])
    print('1')
    self.do_run('src.js', 'Caught exception: std::exception\nDone.', args=['1'], no_build=True)
    print('2')
    self.do_run('src.js', 'Caught exception: Hello\nDone.', args=['2'], no_build=True)

  def test_exceptions_allowed(self):
    self.set_setting('EXCEPTION_CATCHING_ALLOWED', ["_Z12somefunctionv"])
    # otherwise it is inlined and not identified
    self.set_setting('INLINING_LIMIT')

    self.do_core_test('test_exceptions_allowed.cpp')
    size = os.path.getsize('test_exceptions_allowed.js')
    if self.is_wasm():
      size += os.path.getsize('test_exceptions_allowed.wasm')
    shutil.copyfile('test_exceptions_allowed.js', 'orig.js')

    # check that an empty allow list works properly (as in, same as exceptions disabled)
    src = test_file('core/test_exceptions_allowed.cpp')
    empty_output = test_file('core/test_exceptions_allowed_empty.out')

    self.set_setting('EXCEPTION_CATCHING_ALLOWED', [])
    self.do_run_from_file(src, empty_output, assert_returncode=NON_ZERO)
    empty_size = os.path.getsize('test_exceptions_allowed.js')
    if self.is_wasm():
      empty_size += os.path.getsize('test_exceptions_allowed.wasm')
    shutil.copyfile('test_exceptions_allowed.js', 'empty.js')

    self.set_setting('EXCEPTION_CATCHING_ALLOWED', ['fake'])
    self.do_run_from_file(src, empty_output, assert_returncode=NON_ZERO)
    fake_size = os.path.getsize('test_exceptions_allowed.js')
    if self.is_wasm():
      fake_size += os.path.getsize('test_exceptions_allowed.wasm')
    shutil.copyfile('test_exceptions_allowed.js', 'fake.js')

    self.clear_setting('EXCEPTION_CATCHING_ALLOWED')
    self.do_run_from_file(src, empty_output, assert_returncode=NON_ZERO)
    disabled_size = os.path.getsize('test_exceptions_allowed.js')
    if self.is_wasm():
      disabled_size += os.path.getsize('test_exceptions_allowed.wasm')
    shutil.copyfile('test_exceptions_allowed.js', 'disabled.js')

    print('size: %d' % size)
    print('empty_size: %d' % empty_size)
    print('fake_size: %d' % fake_size)
    print('disabled_size: %d' % disabled_size)
    # empty list acts the same as fully disabled
    self.assertEqual(empty_size, disabled_size)
    # big change when we disable exception catching of the function
    if '-fsanitize=leak' not in self.emcc_args:
      self.assertGreater(size - empty_size, 0.01 * size)
    # full disable can remove a little bit more
    # For some reason this no longer holds true at high optimizations
    # levels: https://github.com/emscripten-core/emscripten/issues/18312
    if not any(o in self.emcc_args for o in ('-O3', '-Oz', '-Os')):
      self.assertLess(disabled_size, fake_size)

  def test_exceptions_allowed_2(self):
    self.set_setting('EXCEPTION_CATCHING_ALLOWED', ["main"])
    # otherwise it is inlined and not identified
    self.set_setting('INLINING_LIMIT')
    self.do_core_test('test_exceptions_allowed_2.cpp')

    # When 'main' function does not have a signature, its contents will be
    # outlined to '__original_main'. Check if we can handle that case.
    self.emcc_args += ['-DMAIN_NO_SIGNATURE']
    self.do_core_test('test_exceptions_allowed_2.cpp')

  def test_exceptions_allowed_uncaught(self):
    self.emcc_args += ['-std=c++11']
    self.set_setting('EXCEPTION_CATCHING_ALLOWED', ["_Z4testv"])
    # otherwise it is inlined and not identified
    self.set_setting('INLINING_LIMIT')

    self.do_core_test('test_exceptions_allowed_uncaught.cpp')

  def test_exceptions_allowed_misuse(self):
    self.set_setting('EXCEPTION_CATCHING_ALLOWED', ['foo'])

    # Test old =2 setting for DISABLE_EXCEPTION_CATCHING
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 2)
    err = self.expect_fail([EMCC, test_file('hello_world.c')] + self.get_emcc_args())
    self.assertContained('error: DISABLE_EXCEPTION_CATCHING=X is no longer needed when specifying EXCEPTION_CATCHING_ALLOWED [-Wdeprecated] [-Werror]', err)

    # =0 should also be a warning
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
    err = self.expect_fail([EMCC, test_file('hello_world.c')] + self.get_emcc_args())
    self.assertContained('error: DISABLE_EXCEPTION_CATCHING=X is no longer needed when specifying EXCEPTION_CATCHING_ALLOWED [-Wdeprecated] [-Werror]', err)

    # =1 should be a hard error
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 1)
    err = self.expect_fail([EMCC, test_file('hello_world.c')] + self.get_emcc_args())
    self.assertContained('error: DISABLE_EXCEPTION_CATCHING and EXCEPTION_CATCHING_ALLOWED are mutually exclusive', err)

    # even setting an empty list should trigger the error;
    self.set_setting('EXCEPTION_CATCHING_ALLOWED', [])
    err = self.expect_fail([EMCC, test_file('hello_world.c')] + self.get_emcc_args())
    self.assertContained('error: DISABLE_EXCEPTION_CATCHING and EXCEPTION_CATCHING_ALLOWED are mutually exclusive', err)

  @with_both_eh_sjlj
  def test_exceptions_uncaught(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    src = r'''
      #include <stdio.h>
      #include <exception>
      struct X {
        ~X() {
          printf("exception? %s\n", std::uncaught_exception() ? "yes" : "no");
        }
      };
      int main() {
        printf("exception? %s\n", std::uncaught_exception() ? "yes" : "no");
        try {
          X x;
          throw 1;
        } catch(...) {
          printf("exception? %s\n", std::uncaught_exception() ? "yes" : "no");
        }
        printf("exception? %s\n", std::uncaught_exception() ? "yes" : "no");
        return 0;
      }
    '''
    self.do_run(src, 'exception? no\nexception? yes\nexception? no\nexception? no\n')

    src = r'''
      #include <fstream>
      #include <iostream>
      int main() {
        std::ofstream os("test");
        os << std::unitbuf << "foo"; // trigger a call to std::uncaught_exception from
                                     // std::basic_ostream::sentry::~sentry
        std::cout << "success";
      }
    '''
    self.do_run(src, 'success')

  @with_both_eh_sjlj
  def test_exceptions_uncaught_2(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    src = r'''
      #include <iostream>
      #include <exception>

      int main() {
        try {
            throw std::exception();
        } catch(std::exception) {
          try {
            throw;
          } catch(std::exception) {}
        }

        if (std::uncaught_exception())
          std::cout << "ERROR: uncaught_exception still set.\n";
        else
          std::cout << "OK\n";
      }
    '''
    self.do_run(src, 'OK\n')

  @with_both_eh_sjlj
  def test_exceptions_typed(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.clear_setting('SAFE_HEAP') # Throwing null will cause an ignorable null pointer access.
    self.do_core_test('test_exceptions_typed.cpp')

  @with_both_eh_sjlj
  def test_exceptions_virtual_inheritance(self):
    self.do_core_test('test_exceptions_virtual_inheritance.cpp')

  @with_both_eh_sjlj
  def test_exceptions_convert(self):
    self.do_core_test('test_exceptions_convert.cpp')

  # TODO Make setjmp-longjmp also use Wasm exception handling
  @with_both_eh_sjlj
  def test_exceptions_multi(self):
    self.do_core_test('test_exceptions_multi.cpp')

  @with_both_eh_sjlj
  def test_exceptions_std(self):
    self.clear_setting('SAFE_HEAP')
    self.do_core_test('test_exceptions_std.cpp')

  @with_both_eh_sjlj
  def test_exceptions_alias(self):
    self.do_core_test('test_exceptions_alias.cpp')

  @with_both_eh_sjlj
  def test_exceptions_rethrow(self):
    self.do_core_test('test_exceptions_rethrow.cpp')

  @with_both_eh_sjlj
  def test_exceptions_uncaught_count(self):
    self.do_core_test('test_exceptions_uncaught_count.cpp')

  @with_both_eh_sjlj
  def test_exceptions_resume(self):
    self.set_setting('EXCEPTION_DEBUG')
    self.do_core_test('test_exceptions_resume.cpp')

  @with_both_eh_sjlj
  def test_exceptions_destroy_virtual(self):
    self.do_core_test('test_exceptions_destroy_virtual.cpp')

  @with_both_eh_sjlj
  def test_exceptions_refcount(self):
    self.do_core_test('test_exceptions_refcount.cpp')

  @with_both_eh_sjlj
  def test_exceptions_primary(self):
    self.do_core_test('test_exceptions_primary.cpp')

  @with_both_eh_sjlj
  def test_exceptions_simplify_cfg(self):
    self.do_core_test('test_exceptions_simplify_cfg.cpp')

  @with_both_eh_sjlj
  def test_exceptions_libcxx(self):
    self.do_core_test('test_exceptions_libcxx.cpp')

  @with_both_eh_sjlj
  def test_exceptions_multiple_inherit(self):
    self.do_core_test('test_exceptions_multiple_inherit.cpp')

  @with_both_eh_sjlj
  def test_exceptions_multiple_inherit_rethrow(self):
    self.do_core_test('test_exceptions_multiple_inherit_rethrow.cpp')

  @with_both_eh_sjlj
  def test_exceptions_rethrow_missing(self):
    create_file('main.cpp', 'int main() { throw; }')
    self.do_runf('main.cpp', None, assert_returncode=NON_ZERO)

  @with_both_eh_sjlj
  def test_EXPORT_EXCEPTION_HANDLING_HELPERS(self):
    self.set_setting('ASSERTIONS', 0)
    self.set_setting('EXPORT_EXCEPTION_HANDLING_HELPERS')
    # FIXME Temporary workaround. See 'FIXME' in the test source code below for
    # details.
    if self.get_setting('DISABLE_EXCEPTION_CATCHING') == 0:
      self.emcc_args.append('-D__USING_EMSCRIPTEN_EXCEPTION__')

    self.maybe_closure()
    create_file('main.cpp', '''
      #include <emscripten.h>
      #include <exception>
      #include <stdexcept>
      using namespace std;

      class myexception : public exception {
        virtual const char* what() const throw() { return "My exception happened"; }
      } myex;

      EMSCRIPTEN_KEEPALIVE extern "C" void throw_exc(int x) {
        if (x == 1) {
          throw 1000;
        }
        if (x == 2) {
          throw 'c';
        }
        if (x == 3) {
          throw runtime_error("abc");
        }
        if (x == 4) {
          throw myex;
        }
        if (x == 5) {
          throw "abc";
        }
      }

      int main() {
          EM_ASM({
            for (let i = 1; i < 6; i++){
              try {
                  _throw_exc(i);
              } catch(p) {
                  // Because we are catching and handling the exception in JS, the normal
                  // exception catching C++ code doesn't kick in, so we need to make sure we free
                  // the exception, if necessary. By incrementing and decrementing the refcount
                  // we trigger the free'ing of the exception if its refcount was zero.
#ifdef __USING_EMSCRIPTEN_EXCEPTION__
                  // FIXME Currently Wasm EH and Emscripten EH increases
                  // refcounts in different places. Wasm EH sets the refcount to
                  // 1 when throwing, and decrease it in __cxa_end_catch.
                  // Emscripten EH sets the refcount to 0 when throwing, and
                  // increase it in __cxa_begin_catch, and decrease it in
                  // __cxa_end_catch. Fix this inconsistency later.
                  incrementExceptionRefcount(p);
#endif
                  console.log(getExceptionMessage(p).toString());
                  decrementExceptionRefcount(p);
              }
            }
          });
      }
    ''')
    expected = '''\
int,
char,
std::runtime_error,abc
myexception,My exception happened
char const*,
'''

    self.do_runf('main.cpp', expected)

  @with_both_eh_sjlj
  def test_bad_typeid(self):
    self.do_run(r'''
// exception example
#include <iostream>       // std::cerr
#include <typeinfo>       // operator typeid
#include <exception>      // std::exception

class Polymorphic {virtual void member(){}};

int main () {
  try
  {
    Polymorphic * pb = 0;
    const std::type_info& ti = typeid(*pb);  // throws a bad_typeid exception
  }
  catch (std::exception& e)
  {
    std::cerr << "exception caught: " << e.what() << '\n';
  }
  return 0;
}
''', 'exception caught: std::bad_typeid')

  @with_both_eh_sjlj
  def test_abort_no_dtors(self):
    # abort() should not run destructors
    out = self.do_run(r'''
#include <stdlib.h>
#include <stdio.h>

struct Foo {
  ~Foo() { printf("Destructing Foo\n"); }
};

int main() {
  Foo f;
  abort();
}
''', assert_returncode=NON_ZERO)
    self.assertNotContained('Destructing Foo', out)

  def test_iostream_ctors(self):
    # iostream stuff must be globally constructed before user global
    # constructors, so iostream works in global constructors
    self.do_run(r'''
#include <iostream>

struct A {
  A() { std::cout << "bug"; }
};
A a;

int main() {
  std::cout << "free code" << std::endl;
  return 0;
}
''', 'bugfree code')

  @with_both_eh_sjlj
  def test_exceptions_longjmp1(self):
    self.do_core_test('test_exceptions_longjmp1.cpp')

  @with_both_eh_sjlj
  def test_exceptions_longjmp2(self):
    self.do_core_test('test_exceptions_longjmp2.cpp')

  @with_both_eh_sjlj
  def test_exceptions_longjmp3(self):
    if '-fwasm-exceptions' in self.emcc_args:
      self.skipTest('https://github.com/emscripten-core/emscripten/issues/17004')
    self.do_core_test('test_exceptions_longjmp3.cpp')

  @with_both_eh_sjlj
  def test_exceptions_longjmp4(self):
    self.do_core_test('test_exceptions_longjmp4.cpp')

  def test_exception_sjlj_options(self):
    # Clear all settings used in this test
    def clear_all_relevant_settings(self):
      self.clear_setting('DISABLE_EXCEPTION_THROWING')
      self.clear_setting('DISABLE_EXCEPTION_CATCHING')
      self.clear_setting('SUPPORT_LONGJMP')
      self.clear_setting('ASYNCIFY')

    # Emscripten EH and Wasm EH cannot be enabled at the same time
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp'), '-fwasm-exceptions'] + self.get_emcc_args())
    self.assertContained('error: DISABLE_EXCEPTION_CATCHING=0 is not compatible with -fwasm-exceptions', err)
    clear_all_relevant_settings(self)

    self.set_setting('DISABLE_EXCEPTION_THROWING', 0)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp'), '-fwasm-exceptions'] + self.get_emcc_args())
    self.assertContained('error: DISABLE_EXCEPTION_THROWING=0 is not compatible with -fwasm-exceptions', err)
    clear_all_relevant_settings(self)

    # Emscripten EH: You can't enable catching and disable throwing
    self.set_setting('DISABLE_EXCEPTION_THROWING', 1)
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp')] + self.get_emcc_args())
    self.assertContained("error: DISABLE_EXCEPTION_THROWING was set (probably from -fno-exceptions) but is not compatible with enabling exception catching (DISABLE_EXCEPTION_CATCHING=0). If you don't want exceptions, set DISABLE_EXCEPTION_CATCHING to 1; if you do want exceptions, don't link with -fno-exceptions", err)
    clear_all_relevant_settings(self)

    # When using Wasm EH, users are not supposed to explicitly pass
    # DISABLE_EXCEPTION_THROWING / DISABLE_EXCEPTION_CATCHING (even in order to
    # correctly disable them; it will be taken care of by emcc)
    # We only warn on these cases, but the tests here error out because the
    # test setting includes -Werror.
    self.set_setting('DISABLE_EXCEPTION_THROWING', 1)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp'), '-fwasm-exceptions'] + self.get_emcc_args())
    self.assertContained('error: You no longer need to pass DISABLE_EXCEPTION_CATCHING or DISABLE_EXCEPTION_THROWING when using Wasm exceptions', err)
    clear_all_relevant_settings(self)

    self.set_setting('DISABLE_EXCEPTION_CATCHING', 1)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp'), '-fwasm-exceptions'] + self.get_emcc_args())
    self.assertContained('error: You no longer need to pass DISABLE_EXCEPTION_CATCHING or DISABLE_EXCEPTION_THROWING when using Wasm exceptions', err)
    clear_all_relevant_settings(self)

    # Emscripten SjLj and Wasm EH cannot mix
    self.set_setting('SUPPORT_LONGJMP', 'emscripten')
    err = self.expect_fail([EMCC, test_file('hello_world.cpp'), '-fwasm-exceptions'] + self.get_emcc_args())
    self.assertContained('error: SUPPORT_LONGJMP=emscripten is not compatible with -fwasm-exceptions', err)
    clear_all_relevant_settings(self)

    # Wasm SjLj and Emscripten EH cannot mix
    self.set_setting('SUPPORT_LONGJMP', 'wasm')
    self.set_setting('DISABLE_EXCEPTION_THROWING', 0)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp')] + self.get_emcc_args())
    self.assertContained('error: SUPPORT_LONGJMP=wasm cannot be used with DISABLE_EXCEPTION_THROWING=0', err)
    clear_all_relevant_settings(self)

    self.set_setting('SUPPORT_LONGJMP', 'wasm')
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp')] + self.get_emcc_args())
    self.assertContained('error: SUPPORT_LONGJMP=wasm cannot be used with DISABLE_EXCEPTION_CATCHING=0', err)
    clear_all_relevant_settings(self)

    # Wasm EH does not support ASYNCIFY=1
    self.set_setting('ASYNCIFY', 1)
    err = self.expect_fail([EMCC, test_file('hello_world.cpp'), '-fwasm-exceptions'] + self.get_emcc_args())
    self.assertContained('error: ASYNCIFY=1 is not compatible with -fwasm-exceptions. Parts of the program that mix ASYNCIFY and exceptions will not compile.', err)
    clear_all_relevant_settings(self)

    # EXPORT_EXCEPTION_HANDLING_HELPERS and EXCEPTION_STACK_TRACES requires
    # either Emscripten EH or Wasm EH
    self.set_setting('EXPORT_EXCEPTION_HANDLING_HELPERS')
    err = self.expect_fail([EMCC, test_file('hello_world.cpp')] + self.get_emcc_args())
    self.assertContained('error: EXPORT_EXCEPTION_HANDLING_HELPERS requires either of -fexceptions or -fwasm-exceptions', err)
    clear_all_relevant_settings(self)

    self.set_setting('EXCEPTION_STACK_TRACES')
    err = self.expect_fail([EMCC, test_file('hello_world.cpp')] + self.get_emcc_args())
    self.assertContained('error: EXCEPTION_STACK_TRACES requires either of -fexceptions or -fwasm-exceptions', err)
    clear_all_relevant_settings(self)

  # Marked as impure since the WASI reactor modules (modules without main)
  # are not yet suppored by the wasm engines we test against.
  @also_with_standalone_wasm(impure=True)
  def test_ctors_no_main(self):
    self.emcc_args.append('--no-entry')
    self.do_core_test('test_ctors_no_main.cpp')

  @no_wasm2js('eval_ctors not supported yet')
  @also_with_standalone_wasm(impure=True)
  def test_eval_ctors_no_main(self):
    if self.get_setting('MEMORY64') == 1:
      self.skipTest('https://github.com/WebAssembly/binaryen/issues/5017')
    self.set_setting('EVAL_CTORS')
    self.emcc_args.append('--no-entry')
    self.do_core_test('test_ctors_no_main.cpp')

  def test_class(self):
    self.do_core_test('test_class.cpp')

  def test_inherit(self):
    self.do_core_test('test_inherit.cpp')

  def test_isdigit_l(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_isdigit_l.cpp')

  def test_iswdigit(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_iswdigit.cpp')

  def test_polymorph(self):
    self.do_core_test('test_polymorph.cpp')

  def test_complex(self):
    self.do_core_test('test_complex.c')

  def test_float_builtins(self):
    # tests wasm_libc_rt
    self.do_core_test('test_float_builtins.c')

  @no_asan('SAFE_HEAP cannot be used with ASan')
  def test_segfault(self):
    self.set_setting('SAFE_HEAP')

    for addr in ['get_null()', 'new D2()']:
      print(addr)
      src = r'''
        #include <stdio.h>
        #include <emscripten.h>

        struct Classey {
          virtual void doIt() = 0;
          virtual ~Classey() = default;
        };

        struct D1 : Classey {
          virtual void doIt() { printf("fleefl\n"); }
        };

        struct D2 : Classey {
          virtual void doIt() { printf("marfoosh\n"); }
        };

        EM_JS(Classey*, get_null, (), {
        #if __wasm64__
          return 0n;
        #else
          return 0;
        #endif
        });

        int main(int argc, char **argv) {
          Classey *p = argc == 100 ? new D1() : (Classey*)%s;

          p->doIt();
          delete p;

          return 0;
        }
      ''' % addr
      if 'get_null' in addr:
        self.do_run(src, 'segmentation fault', assert_returncode=NON_ZERO)
      else:
        self.do_run(src, 'marfoosh')

  def test_dynamic_cast(self):
    self.do_core_test('test_dynamic_cast.cpp')

  def test_dynamic_cast_b(self):
    self.do_core_test('test_dynamic_cast_b.cpp')

  def test_dynamic_cast_2(self):
    self.do_core_test('test_dynamic_cast_2.cpp')

  def test_funcptr(self):
    self.do_core_test('test_funcptr.c')

  def test_mathfuncptr(self):
    self.do_core_test('test_mathfuncptr.c')

  def test_funcptrfunc(self):
    self.do_core_test('test_funcptrfunc.c')

  def test_funcptr_namecollide(self):
    self.do_core_test('test_funcptr_namecollide.c')

  def test_emptyclass(self):
    self.do_core_test('test_emptyclass.cpp')

  def test_alloca(self):
    self.do_core_test('test_alloca.c')

  @also_with_wasmfs
  def test_rename(self):
    if is_sanitizing(self.emcc_args) and self.get_setting('WASMFS'):
      self.skipTest('https://github.com/emscripten-core/emscripten/issues/15820')
    self.do_run_in_out_file_test('stdio/test_rename.c')

  def test_remove(self):
   # needs to flush stdio streams
   self.set_setting('EXIT_RUNTIME')
   self.do_run_in_out_file_test('cstdio/test_remove.cpp')

  def test_alloca_stack(self):
    self.do_core_test('test_alloca_stack.c')

  def test_stack_byval(self):
    self.do_core_test('test_stack_byval.cpp')

  def test_stack_varargs(self):
    # in node.js we allocate argv[0] on the stack, which means the  length
    # of the program directory influences how much stack we need, and so
    # long random temp dir names can lead to random failures. The stack
    # size was increased here to avoid that.
    self.set_setting('INLINING_LIMIT')
    self.set_setting('STACK_SIZE', 8 * 1024)

    self.do_core_test('test_stack_varargs.c')

  def test_stack_varargs2(self):
    # in node.js we allocate argv[0] on the stack, which means the  length
    # of the program directory influences how much stack we need, and so
    # long random temp dir names can lead to random failures. The stack
    # size was increased here to avoid that.
    self.set_setting('STACK_SIZE', 8 * 1024)
    src = r'''
      #include <stdio.h>
      #include <stdlib.h>

      void func(int i) {
      }
      int main() {
        for (int i = 0; i < 7000; i++) {
          printf("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
                   i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i);
        }
        printf("ok!\n");
        return 0;
      }
    '''
    self.do_run(src, 'ok!')

    print('with return')

    src = r'''
      #include <stdio.h>
      #include <stdlib.h>

      int main() {
        for (int i = 0; i < 7000; i++) {
          int j = printf("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                   i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i);
          printf(" (%d)\n", j);
        }
        printf("ok!\n");
        return 0;
      }
    '''
    self.do_run(src, 'ok!')

    print('with definitely no return')

    src = r'''
      #include <stdio.h>
      #include <stdlib.h>
      #include <stdarg.h>

      void vary(const char *s, ...)
      {
        va_list v;
        va_start(v, s);
        char d[20];
        vsnprintf(d, 20, s, v);
        puts(d);

        // Try it with copying
        va_list tempva;
        va_copy(tempva, v);
        vsnprintf(d, 20, s, tempva);
        puts(d);

        va_end(v);
      }

      int main() {
        for (int i = 0; i < 7000; i++) {
          int j = printf("%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                   i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i, i);
          printf(" (%d)\n", j);
          vary("*cheez: %d+%d*", 99, 24);
          vary("*albeit*");
        }
        printf("ok!\n");
        return 0;
      }
    '''
    self.do_run(src, 'ok!')

  def test_stack_void(self):
    self.emcc_args.append('-Wno-format-extra-args')
    self.set_setting('INLINING_LIMIT')
    self.do_core_test('test_stack_void.c')

  def test_life(self):
    self.do_run_in_out_file_test('life.c', args=['2'])

  def test_array2(self):
    self.do_core_test('test_array2.c')

  def test_array2b(self):
    self.do_core_test('test_array2b.c')

  def test_constglobalstructs(self):
    self.do_core_test('test_constglobalstructs.c')

  def test_conststructs(self):
    self.do_core_test('test_conststructs.c')

  def test_bigarray(self):
    self.do_core_test('test_bigarray.c')

  def test_mod_globalstruct(self):
    self.do_core_test('test_mod_globalstruct.c')

  def test_sizeof(self):
    self.do_core_test('test_sizeof.cpp')

  def test_llvm_used(self):
    self.do_core_test('test_llvm_used.c')

  @no_asan('SAFE_HEAP cannot be used with ASan')
  def test_set_align(self):
    self.set_setting('SAFE_HEAP')

    self.do_core_test('test_set_align.c')

  def test_emscripten_api(self):
    self.set_setting('EXPORTED_FUNCTIONS', ['_main', '_save_me_aimee'])
    self.do_core_test('test_emscripten_api.cpp')

    # Sanitizers are not compatible with LINKABLE (dynamic linking.
    if not is_sanitizing(self.emcc_args) and not self.is_wasm64():
      # test EXPORT_ALL
      self.set_setting('EXPORTED_FUNCTIONS', [])
      self.set_setting('EXPORT_ALL')
      self.set_setting('LINKABLE')
      self.do_core_test('test_emscripten_api.cpp')

  def test_emscripten_run_script_string_int(self):
    src = r'''
      #include <stdio.h>
      #include <emscripten.h>

      int main() {
        const char *str = emscripten_run_script_string("1+1");
        printf("got string: %s\n", str);
        return 0;
      }
    '''
    self.do_run(src, '''got string: 2''')

  def test_emscripten_run_script_string_utf8(self):
    src = r'''
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <emscripten.h>

      int main() {
        const char *str = emscripten_run_script_string("'\\u2603 \\u2603 \\u2603 Hello!'");
        printf("length of returned string: %zu. Position of substring 'Hello': %zu\n", strlen(str), strstr(str, "Hello")-str);
        return 0;
      }
    '''
    self.do_run(src, '''length of returned string: 18. Position of substring 'Hello': 12''')

  def test_emscripten_run_script_string_null(self):
    src = r'''
      #include <stdio.h>
      #include <emscripten.h>

      int main() {
        const char *str = emscripten_run_script_string("void(0)");
        if (str) {
          printf("got string: %s\n", str);
        } else {
          puts("got null");
        }
        return 0;
      }
    '''
    self.do_run(src, 'got null')

  def test_emscripten_get_now(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.maybe_closure()
    self.do_runf(test_file('emscripten_get_now.cpp'), 'Timer resolution is good')

  def test_emscripten_get_compiler_setting(self):
    src = test_file('core/emscripten_get_compiler_setting.c')
    output = shared.replace_suffix(src, '.out')
    # with assertions, a nice message is shown
    self.set_setting('ASSERTIONS')
    self.do_runf(src, 'You must build with -sRETAIN_COMPILER_SETTINGS', assert_returncode=NON_ZERO)
    self.clear_setting('ASSERTIONS')
    self.set_setting('RETAIN_COMPILER_SETTINGS')
    self.do_runf(src, read_file(output).replace('waka', shared.EMSCRIPTEN_VERSION))

  def test_emscripten_has_asyncify(self):
    if self.get_setting('MEMORY64') == 1:
      # This test passes under MEMORY64=2 but not MEMORY64=1 so we don't use is_wasm64 here.
      self.skipTest('TODO: asyncify for wasm64')
    src = r'''
      #include <stdio.h>
      #include <emscripten.h>

      int main() {
        printf("%d\n", emscripten_has_asyncify());
        return 0;
      }
    '''
    self.set_setting('ASYNCIFY', 0)
    self.do_run(src, '0')
    self.set_setting('ASYNCIFY')
    self.do_run(src, '1')

  # TODO: test only worked in non-fastcomp
  def test_inlinejs(self):
    self.skipTest('non-fastcomp is deprecated and fails in 3.5') # only supports EM_ASM

    self.do_core_test('test_inlinejs.c')

    if self.emcc_args == []:
      # opts will eliminate the comments
      out = read_file('src.js')
      for i in range(1, 5):
        assert ('comment%d' % i) in out

  # TODO: test only worked in non-fastcomp
  def test_inlinejs2(self):
    self.skipTest('non-fastcomp is deprecated and fails in 3.5') # only supports EM_ASM

    self.do_core_test('test_inlinejs2.c')

  def test_inlinejs3(self):
    if self.is_wasm():
      self.skipTest('wasm requires a proper asm module')

    src = test_file('core/test_inlinejs3.c')
    output = shared.unsuffixed(src) + '.out'

    self.do_core_test('test_inlinejs3.c')

    print('no debugger, check validation')
    src = read_file(src).replace('emscripten_debugger();', '')
    self.do_run(src, read_file(output))

  def test_inlinejs4(self):
    self.do_run(r'''
#include <emscripten.h>

#define TO_STRING_INNER(x) #x
#define TO_STRING(x) TO_STRING_INNER(x)
#define assert_msg(msg, file, line) EM_ASM( throw 'Assert (' + msg + ') failed in ' + file + ':' + line + '!'; )
#define assert(expr) { \
  if (!(expr)) { \
    assert_msg(#expr, TO_STRING(__FILE__), TO_STRING(__LINE__)); \
  } \
}

int main(int argc, char **argv) {
  assert(argc != 17);
  assert(false);
  return 0;
}
''', 'false', assert_returncode=NON_ZERO)

  def test_em_asm(self):
    self.do_core_test('test_em_asm.cpp')

  def test_em_asm_c(self):
    self.emcc_args.append('-std=gnu89')
    self.do_core_test('test_em_asm.cpp', force_c=True)

  # Tests various different ways to invoke the EM_ASM(), EM_ASM_INT()
  # and EM_ASM_DOUBLE() macros.
  def test_em_asm_2(self):
    self.do_core_test('test_em_asm_2.cpp')
    self.emcc_args.append('-std=gnu89')
    self.do_core_test('test_em_asm_2.cpp', force_c=True)

  # Tests various different ways to invoke the MAIN_THREAD_EM_ASM(), MAIN_THREAD_EM_ASM_INT() and MAIN_THREAD_EM_ASM_DOUBLE() macros.
  # This test is identical to test_em_asm_2, just search-replaces EM_ASM to MAIN_THREAD_EM_ASM on the test file. That way if new
  # test cases are added to test_em_asm_2.cpp for EM_ASM, they will also get tested in MAIN_THREAD_EM_ASM form.
  def test_main_thread_em_asm(self):
    src = read_file(test_file('core/test_em_asm_2.cpp'))
    create_file('src.cpp', src.replace('EM_ASM', 'MAIN_THREAD_EM_ASM'))

    expected_result = read_file(test_file('core/test_em_asm_2.out'))
    create_file('result.out', expected_result.replace('EM_ASM', 'MAIN_THREAD_EM_ASM'))

    self.do_run_from_file('src.cpp', 'result.out')
    self.do_run_from_file('src.cpp', 'result.out', force_c=True)

  @needs_dylink
  @parameterized({
    '': ([], False),
    'relocatable': (['-sMAIN_MODULE=2'], False),
    'force_c': ([], True),
  })
  def test_main_thread_async_em_asm(self, args, force_c=False):
    self.do_core_test('test_main_thread_async_em_asm.cpp', emcc_args=args, force_c=force_c)

  # Tests MAIN_THREAD_EM_ASM_INT() function call with different signatures.
  def test_main_thread_em_asm_signatures(self):
    self.do_core_test('test_em_asm_signatures.cpp', assert_returncode=NON_ZERO)

  @crossplatform
  def test_em_asm_unicode(self):
    self.do_core_test('test_em_asm_unicode.cpp')
    self.do_core_test('test_em_asm_unicode.cpp', force_c=True)

  def test_em_asm_types(self):
    self.do_core_test('test_em_asm_types.cpp')

  def test_em_asm_types_c(self):
    self.do_core_test('test_em_asm_types.cpp', force_c=True)

  def test_em_asm_unused_arguments(self):
    self.do_core_test('test_em_asm_unused_arguments.cpp')

  # Verify that EM_ASM macros support getting called with multiple arities.
  # Maybe tests will later be joined into larger compilation units?
  # Then this must still be compiled separately from other code using EM_ASM
  # macros with arities 1-3. Otherwise this may incorrectly report a success.
  def test_em_asm_parameter_pack(self):
    self.do_core_test('test_em_asm_parameter_pack.cpp')

  def test_em_asm_arguments_side_effects(self):
    self.do_core_test('test_em_asm_arguments_side_effects.cpp')
    self.do_core_test('test_em_asm_arguments_side_effects.cpp', force_c=True)

  def test_em_asm_direct(self):
    self.do_core_test('test_em_asm_direct.c')

  @needs_dylink
  def test_em_asm_side_module(self):
    self.build(test_file('core/test_em_asm_side.c'), js_outfile=False, emcc_args=['-sSIDE_MODULE'], output_basename='side')
    self.do_core_test('test_em_asm_main.c', emcc_args=['-sMAIN_MODULE=2', 'side.wasm'])

  @parameterized({
    '': ([], False),
    'pthreads': (['-pthread', '-sPROXY_TO_PTHREAD', '-sEXIT_RUNTIME'], False),
    'pthreads_dylink': (['-pthread', '-sPROXY_TO_PTHREAD', '-sEXIT_RUNTIME', '-sMAIN_MODULE=2', '-Wno-experimental'], False),
    'c': ([], True),
    'dylink': (['-sMAIN_MODULE=2'], False),
    'dylink_c': (['-sMAIN_MODULE=2'], True),
  })
  @crossplatform
  def test_em_js(self, args, force_c):
    if '-sMAIN_MODULE=2' in args:
      self.check_dylink()
    else:
      self.emcc_args += ['-sEXPORTED_FUNCTIONS=_main,_malloc']
    self.emcc_args += args
    if '-pthread' in args:
      self.setup_node_pthreads()

    self.do_core_test('test_em_js.cpp', force_c=force_c)
    self.assertContained("no args returning int", read_file('test_em_js.js'))

  @no_wasm2js('WASM_BIGINT is not compatible with wasm2js')
  def test_em_js_i64(self):
    err = self.expect_fail([EMCC, '-Werror', test_file('core/test_em_js_i64.c')])
    self.assertContained('emcc: error: using 64-bit arguments in EM_JS function without WASM_BIGINT is not yet fully supported: `foo`', err)

    self.set_setting('WASM_BIGINT')
    self.node_args += shared.node_bigint_flags()
    self.do_core_test('test_em_js_i64.c')

  def test_em_js_address_taken(self):
    self.do_core_test('test_em_js_address_taken.c')
    if self.check_dylink():
      self.set_setting('MAIN_MODULE', 2)
      self.do_core_test('test_em_js_address_taken.c')

  def test_runtime_stacksave(self):
    self.do_runf(test_file('core/test_runtime_stacksave.c'), 'success')

  # Tests that -sMINIMAL_RUNTIME builds can utilize -sALLOW_MEMORY_GROWTH option.
  def test_minimal_runtime_memorygrowth(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')
    self.set_setting('MINIMAL_RUNTIME')
    src = test_file('core/test_memorygrowth.c')
    # Fail without memory growth
    self.do_runf(src, 'OOM', assert_returncode=NON_ZERO)
    # Win with it
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.do_runf(src, '*pre: hello,4.955*\n*hello,4.955*\n*hello,4.955*')

  def test_memorygrowth(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')
    if self.maybe_closure():
      # verify NO_DYNAMIC_EXECUTION is compatible with closure
      self.set_setting('DYNAMIC_EXECUTION', 0)
      self.emcc_args.append('-Wno-closure')
    # With typed arrays in particular, it is dangerous to use more memory than INITIAL_MEMORY,
    # since we then need to enlarge the heap(s).
    src = test_file('core/test_memorygrowth.c')

    # Fail without memory growth
    self.do_runf(src, 'OOM', assert_returncode=NON_ZERO)
    fail = read_file('test_memorygrowth.js')

    # Win with it
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.do_runf(src, '*pre: hello,4.955*\n*hello,4.955*\n*hello,4.955*')
    win = read_file('test_memorygrowth.js')

    if '-O2' in self.emcc_args and not self.is_wasm():
      # Make sure ALLOW_MEMORY_GROWTH generates different code (should be less optimized)
      code_start = '// EMSCRIPTEN_START_FUNCS'
      self.assertContained(code_start, fail)
      fail = fail[fail.find(code_start):]
      win = win[win.find(code_start):]
      assert len(fail) < len(win), 'failing code - without memory growth on - is more optimized, and smaller' + str([len(fail), len(win)])

    # Tracing of memory growths should work
    # (SAFE_HEAP would instrument the tracing code itself, leading to recursion)
    if not self.get_setting('SAFE_HEAP'):
      self.emcc_args += ['--tracing']
      self.do_runf(src, '*pre: hello,4.955*\n*hello,4.955*\n*hello,4.955*')

  def test_memorygrowth_2(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')

    # With typed arrays in particular, it is dangerous to use more memory than INITIAL_MEMORY,
    # since we then need to enlarge the heap(s).
    src = test_file('core/test_memorygrowth_2.c')

    # Fail without memory growth
    self.do_runf(src, 'OOM', assert_returncode=NON_ZERO)
    fail = read_file('test_memorygrowth_2.js')

    # Win with it
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.do_runf(src, '*pre: hello,4.955*\n*hello,4.955*\n*hello,4.955*')
    win = read_file('test_memorygrowth_2.js')

    if '-O2' in self.emcc_args and not self.is_wasm():
      # Make sure ALLOW_MEMORY_GROWTH generates different code (should be less optimized)
      assert len(fail) < len(win), 'failing code - without memory growth on - is more optimized, and smaller' + str([len(fail), len(win)])

  def test_memorygrowth_3(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')

    # checks handling of malloc failure properly
    self.set_setting('ABORTING_MALLOC', 0)
    self.set_setting('SAFE_HEAP')
    self.do_core_test('test_memorygrowth_3.c')

  @also_with_standalone_wasm(impure=True)
  def test_memorygrowth_MAXIMUM_MEMORY(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')
    if not self.is_wasm():
      self.skipTest('wasm memory specific test')

    # check that memory growth does not exceed the wasm mem max limit
    self.emcc_args += ['-sALLOW_MEMORY_GROWTH', '-sINITIAL_MEMORY=64Mb', '-sMAXIMUM_MEMORY=100Mb']
    self.do_core_test('test_memorygrowth_wasm_mem_max.c')

  def test_memorygrowth_linear_step(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')
    if not self.is_wasm():
      self.skipTest('wasm memory specific test')

    # check that memory growth does not exceed the wasm mem max limit and is exactly or one step below the wasm mem max
    self.emcc_args += ['-sALLOW_MEMORY_GROWTH', '-sSTACK_SIZE=1Mb', '-sINITIAL_MEMORY=64Mb', '-sMAXIMUM_MEMORY=130Mb', '-sMEMORY_GROWTH_LINEAR_STEP=1Mb']
    self.do_core_test('test_memorygrowth_memory_growth_step.c')

  @no_ubsan('UBSan seems to effect the precise memory usage')
  def test_memorygrowth_geometric_step(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')
    if not self.is_wasm():
      self.skipTest('wasm memory specific test')

    self.emcc_args += ['-sALLOW_MEMORY_GROWTH', '-sMEMORY_GROWTH_GEOMETRIC_STEP=8.5', '-sMEMORY_GROWTH_GEOMETRIC_CAP=32MB']
    self.do_core_test('test_memorygrowth_geometric_step.c')

  def test_memorygrowth_3_force_fail_reallocBuffer(self):
    if self.has_changed_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('test needs to modify memory growth')

    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.set_setting('TEST_MEMORY_GROWTH_FAILS')
    self.do_core_test('test_memorygrowth_3.c')

  @parameterized({
    'nogrow': ([],),
    'grow': (['-sALLOW_MEMORY_GROWTH', '-sMAXIMUM_MEMORY=18MB'],)
  })
  @no_asan('requires more memory when growing')
  @no_lsan('requires more memory when growing')
  def test_aborting_new(self, args):
    # test that C++ new properly errors if we fail to malloc when growth is
    # enabled, with or without growth
    self.emcc_args += args
    self.do_core_test('test_aborting_new.cpp')

  @no_wasm2js('no WebAssembly.Memory()')
  @no_asan('ASan alters the memory size')
  @no_lsan('LSan alters the memory size')
  def test_module_wasm_memory(self):
    self.emcc_args += ['--pre-js', test_file('core/test_module_wasm_memory.js')]
    self.set_setting('IMPORTED_MEMORY')
    self.do_runf(test_file('core/test_module_wasm_memory.c'), 'success')

  def test_ssr(self): # struct self-ref
    src = '''
      #include <stdio.h>

      // see related things in openjpeg
      typedef struct opj_mqc_state {
        unsigned int qeval;
        int mps;
        struct opj_mqc_state *nmps;
        struct opj_mqc_state *nlps;
      } opj_mqc_state_t;

      static opj_mqc_state_t mqc_states[4] = {
        {0x5600, 0, &mqc_states[2], &mqc_states[3]},
        {0x5602, 1, &mqc_states[3], &mqc_states[2]},
      };

      int main() {
        printf("*%ld*\\n", (long)(mqc_states+1)-(long)mqc_states);
        for (int i = 0; i < 2; i++)
          printf("%d:%d,%d,%ld,%ld\\n", i, mqc_states[i].qeval, mqc_states[i].mps,
                 (long)mqc_states[i].nmps-(long)mqc_states, (long)mqc_states[i].nlps-(long)mqc_states);
        return 0;
      }
      '''
    if self.is_wasm64():
      expected = '*24*\n0:22016,0,48,72\n1:22018,1,72,48\n'
    else:
      expected = '*16*\n0:22016,0,32,48\n1:22018,1,48,32\n'
    self.do_run(src, expected)

  def test_tinyfuncstr(self):
    self.do_core_test('test_tinyfuncstr.cpp')

  def test_llvmswitch(self):
    self.do_core_test('test_llvmswitch.c')

  @no_wasm2js('massive switches can break js engines')
  def test_bigswitch(self):
    if not self.is_optimizing():
      self.skipTest('nodejs takes ~4GB to compile this if the wasm is not optimized, which OOMs')
    self.set_setting('USE_SDL')
    self.do_runf(test_file('bigswitch.cpp'), '''34962: GL_ARRAY_BUFFER (0x8892)
26214: what?
35040: GL_STREAM_DRAW (0x88E0)
3060: what?
''', args=['34962', '26214', '35040', str(0xbf4)])

  @no_wasm2js('massive switches can break js engines')
  @is_slow_test
  def test_biggerswitch(self):
    if not self.is_optimizing():
      self.skipTest('nodejs takes >6GB to compile this if the wasm is not optimized, which OOMs, see https://github.com/emscripten-core/emscripten/issues/7928#issuecomment-458308453')
    num_cases = 20000
    switch_case = self.run_process([PYTHON, test_file('gen_large_switchcase.py'), str(num_cases)], stdout=PIPE, stderr=PIPE).stdout
    self.do_run(switch_case, '''58996: 589965899658996
59297: 592975929759297
59598: default
59899: 598995989959899
Success!''')

  @no_ubsan('local count too large for VMs')
  def test_indirectbr(self):
    self.emcc_args = [x for x in self.emcc_args if x != '-g']

    self.do_core_test('test_indirectbr.c')

  @no_asan('local count too large for VMs')
  @no_ubsan('local count too large for VMs')
  @no_wasm2js('extremely deep nesting, hits stack limit on some VMs')
  def test_indirectbr_many(self):
    if not self.is_optimizing():
      self.skipTest('nodejs takes ~1.8GB to compile this if the wasm is not optimized, which can cause OOM on the test runners')
    self.do_core_test('test_indirectbr_many.c')

  def test_pack(self):
    src = '''
      #include <stdio.h>
      #include <string.h>

      #pragma pack(push,1)
      typedef struct header {
          unsigned char  id;
          unsigned short colour;
          unsigned char  desc;
      } header;
      #pragma pack(pop)

      typedef struct fatheader {
          unsigned char  id;
          unsigned short colour;
          unsigned char  desc;
      } fatheader;

      int main( int argc, const char *argv[] ) {
        header ph[2];
        fatheader pfh[2];
        printf("*%zu,%ld,%ld*\\n", sizeof(header), offsetof(header, desc) - offsetof(header, id), (long)(&ph[1])-(long)(&ph[0]));
        printf("*%zu,%ld,%ld*\\n", sizeof(fatheader), offsetof(fatheader, desc) - offsetof(fatheader, id), (long)(&pfh[1])-(long)(&pfh[0]));
        return 0;
      }
      '''
    self.do_run(src, '*4,3,4*\n*6,4,6*')

  def test_varargs(self):
    self.do_core_test('test_varargs.c')

  def test_varargs_multi(self):
    self.do_core_test('test_varargs_multi.c')

  @unittest.skip('clang cannot compile this code with that target yet')
  def test_varargs_byval(self):
    src = r'''
      #include <stdio.h>
      #include <stdarg.h>

      typedef struct type_a {
        union {
          double f;
          void *p;
          int i;
          short sym;
        } value;
      } type_a;

      enum mrb_vtype {
        MRB_TT_FALSE = 0,   /*   0 */
        MRB_TT_CLASS = 9    /*   9 */
      };

      typedef struct type_b {
        enum mrb_vtype tt:8;
      } type_b;

      void print_type_a(int argc, ...);
      void print_type_b(int argc, ...);

      int main(int argc, char *argv[])
      {
        type_a a;
        type_b b;
        a.value.p = (void*) 0x12345678;
        b.tt = MRB_TT_CLASS;

        printf("The original address of a is: %p\n", a.value.p);
        printf("The original type of b is: %d\n", b.tt);

        print_type_a(1, a);
        print_type_b(1, b);

        return 0;
      }

      void print_type_a(int argc, ...) {
        va_list ap;
        type_a a;

        va_start(ap, argc);
        a = va_arg(ap, type_a);
        va_end(ap);

        printf("The current address of a is: %p\n", a.value.p);
      }

      void print_type_b(int argc, ...) {
        va_list ap;
        type_b b;

        va_start(ap, argc);
        b = va_arg(ap, type_b);
        va_end(ap);

        printf("The current type of b is: %d\n", b.tt);
      }
      '''
    self.do_run(src, '''The original address of a is: 0x12345678
The original type of b is: 9
The current address of a is: 0x12345678
The current type of b is: 9
''')

  def test_functionpointer_libfunc_varargs(self):
    self.do_core_test('test_functionpointer_libfunc_varargs.c')

  def test_structbyval(self):
    self.set_setting('INLINING_LIMIT')

    # part 1: make sure that normally, passing structs by value works

    src = r'''
      #include <stdio.h>

      struct point
      {
        int x, y;
      };

      void dump(struct point p) {
        p.x++; // should not modify
        p.y++; // anything in the caller!
        printf("dump: %d,%d\n", p.x, p.y);
      }

      void dumpmod(struct point *p) {
        p->x++; // should not modify
        p->y++; // anything in the caller!
        printf("dump: %d,%d\n", p->x, p->y);
      }

      int main( int argc, const char *argv[] ) {
        point p = { 54, 2 };
        printf("pre:  %d,%d\n", p.x, p.y);
        dump(p);
        void (*dp)(point p) = dump; // And, as a function pointer
        dp(p);
        printf("post: %d,%d\n", p.x, p.y);
        dumpmod(&p);
        dumpmod(&p);
        printf("last: %d,%d\n", p.x, p.y);
        return 0;
      }
    '''
    self.do_run(src, 'pre:  54,2\ndump: 55,3\ndump: 55,3\npost: 54,2\ndump: 55,3\ndump: 56,4\nlast: 56,4')

  def test_stdlibs(self):
    # safe heap prints a warning that messes up our output.
    self.set_setting('SAFE_HEAP', 0)
    # needs atexit
    self.set_setting('EXIT_RUNTIME')
    if self.is_wasm64():
      out_suffix = '64'
    else:
      out_suffix = ''
    self.do_core_test('test_stdlibs.c', out_suffix=out_suffix)

  def test_stdbool(self):
    create_file('test_stdbool.c', r'''
        #include <stdio.h>
        #include <stdbool.h>

        int main() {
          bool x = true;
          bool y = false;
          printf("*%d*\n", x != y);
          return 0;
        }
      ''')

    self.do_runf('test_stdbool.c', '*1*')

  def test_strtoll_hex(self):
    # tests strtoll for hex strings (0x...)
    self.do_core_test('test_strtoll_hex.c')

  def test_strtoll_dec(self):
    # tests strtoll for decimal strings (0x...)
    self.do_core_test('test_strtoll_dec.c')

  def test_strtoll_bin(self):
    # tests strtoll for binary strings (0x...)
    self.do_core_test('test_strtoll_bin.c')

  def test_strtoll_oct(self):
    # tests strtoll for decimal strings (0x...)
    self.do_core_test('test_strtoll_oct.c')

  def test_strtol_hex(self):
    # tests strtoll for hex strings (0x...)
    self.do_core_test('test_strtol_hex.c')

  def test_strtol_dec(self):
    # tests strtoll for decimal strings (0x...)
    self.do_core_test('test_strtol_dec.c')

  def test_strtol_bin(self):
    # tests strtoll for binary strings (0x...)
    self.do_core_test('test_strtol_bin.c')

  def test_strtol_oct(self):
    # tests strtoll for decimal strings (0x...)
    self.do_core_test('test_strtol_oct.c')

  @also_with_standalone_wasm()
  def test_atexit(self):
    # Confirms they are called in the proper reverse order
    if not self.get_setting('STANDALONE_WASM'):
      # STANDALONE_WASM mode always sets EXIT_RUNTIME if main exists
      self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_atexit.c')

  @no_lsan('https://github.com/emscripten-core/emscripten/issues/15988')
  def test_atexit_threads_stub(self):
    # also tests thread exit (__cxa_thread_atexit)
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_atexit_threads.cpp')

  @node_pthreads
  def test_atexit_threads(self):
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_atexit_threads.cpp')

  @no_asan('test relies on null pointer reads')
  def test_pthread_specific(self):
    self.do_run_in_out_file_test('pthread/specific.c')

  def test_pthread_equal(self):
    self.do_run_in_out_file_test('pthread/test_pthread_equal.cpp')

  @node_pthreads
  @parameterized({
      '': (False,),
      'modularize': (True,),
  })
  def test_pthread_proxying(self, modularize):
    if modularize and self.get_setting('WASM') == 0:
      self.skipTest('MODULARIZE + WASM=0 + pthreads does not work (#16794)')
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('INITIAL_MEMORY=32mb')
    args = []
    if modularize:
      self.set_setting('MODULARIZE')
      self.set_setting('EXPORT_NAME=ModuleFactory')
      # Only instantiate the module on the main thread.
      create_file('extern-post.js',
                  'if (typeof importScripts != "function") ModuleFactory();')
      args = ['--extern-post-js=extern-post.js']
    self.do_run_in_out_file_test('pthread/test_pthread_proxying.c',
                                 interleaved_output=False, emcc_args=args)

  @node_pthreads
  def test_pthread_proxying_cpp(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('INITIAL_MEMORY=32mb')
    self.do_run_in_out_file_test('pthread/test_pthread_proxying_cpp.cpp',
                                 interleaved_output=False)

  @node_pthreads
  def test_pthread_proxying_dropped_work(self):
    self.set_setting('PTHREAD_POOL_SIZE=2')
    self.do_run_in_out_file_test('pthread/test_pthread_proxying_dropped_work.c')

  @node_pthreads
  def test_pthread_proxying_canceled_work(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.do_run_in_out_file_test(
        'pthread/test_pthread_proxying_canceled_work.c',
        interleaved_output=False)

  @node_pthreads
  def test_pthread_proxying_refcount(self):
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('PTHREAD_POOL_SIZE=1')
    self.set_setting('ASSERTIONS=0')
    self.do_run_in_out_file_test('pthread/test_pthread_proxying_refcount.c')

  @node_pthreads
  def test_pthread_dispatch_after_exit(self):
    self.do_run_in_out_file_test('pthread/test_pthread_dispatch_after_exit.c', interleaved_output=False)

  @node_pthreads
  def test_pthread_atexit(self):
    # Test to ensure threads are still running when atexit-registered functions are called
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    self.do_run_in_out_file_test('pthread/test_pthread_atexit.c')

  @node_pthreads
  def test_pthread_nested_work_queue(self):
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    self.do_run_in_out_file_test('pthread/test_pthread_nested_work_queue.c')

  @node_pthreads
  def test_pthread_thread_local_storage(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('INITIAL_MEMORY', '300mb')
    self.do_run_in_out_file_test('pthread/test_pthread_thread_local_storage.cpp')

  @node_pthreads
  def test_pthread_cleanup(self):
    self.set_setting('PTHREAD_POOL_SIZE', 4)
    self.do_run_in_out_file_test('pthread/test_pthread_cleanup.cpp')

  @node_pthreads
  def test_pthread_setspecific_mainthread(self):
    print('.. return')
    self.do_runf(test_file('pthread/test_pthread_setspecific_mainthread.c'), 'done!', emcc_args=['-DRETURN'])
    print('.. exit')
    self.do_runf(test_file('pthread/test_pthread_setspecific_mainthread.c'), 'done!', emcc_args=['-DEXIT'])
    print('.. pthread_exit')
    self.do_run_in_out_file_test('pthread/test_pthread_setspecific_mainthread.c')

  @node_pthreads
  def test_pthread_attr_getstack(self):
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    self.do_run_in_out_file_test('pthread/test_pthread_attr_getstack.c')

  @node_pthreads
  @no_mac('https://github.com/emscripten-core/emscripten/issues/15014')
  def test_pthread_abort(self):
    self.set_setting('PROXY_TO_PTHREAD')
    # Add the onAbort handler at runtime during preRun.  This means that onAbort
    # handler will only be present in the main thread (much like it would if it
    # was passed in by pre-populating the module object on prior to loading).
    self.add_pre_run("Module.onAbort = () => console.log('onAbort called');")
    self.do_run_in_out_file_test('pthread/test_pthread_abort.c', assert_returncode=NON_ZERO)

  @node_pthreads
  def test_pthread_abort_interrupt(self):
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    expected = ['Aborted(). Build with -sASSERTIONS for more info', 'Aborted(native code called abort())']
    self.do_runf(test_file('pthread/test_pthread_abort_interrupt.c'), expected, assert_returncode=NON_ZERO)

  @no_asan('ASan does not support custom memory allocators')
  @no_lsan('LSan does not support custom memory allocators')
  @node_pthreads
  def test_pthread_emmalloc(self):
    self.emcc_args += ['-fno-builtin']
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('ASSERTIONS', 2)
    self.set_setting('MALLOC', 'emmalloc')
    self.do_core_test('test_emmalloc.c')

  @node_pthreads
  def test_pthread_stdout_after_main(self):
    # Verify that secondary threads can continue to write to stdout even
    # after the main thread returns.  We had a regression where stdio
    # streams were locked when the main thread returned.
    self.do_runf(test_file('pthread/test_pthread_stdout_after_main.c'))

  @node_pthreads
  def test_pthread_proxy_to_pthread(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.do_run_in_out_file_test(test_file('pthread/test_pthread_proxy_to_pthread.c'))

  @node_pthreads
  @needs_dylink
  def test_pthread_tls_dylink(self):
    self.set_setting('MAIN_MODULE', 2)
    self.emcc_args.append('-Wno-experimental')
    self.do_run_in_out_file_test('pthread/test_pthread_tls_dylink.c')

  def test_pthread_run_script(self):
    shutil.copyfile(test_file('pthread/foo.js'), 'foo.js')
    self.do_runf(test_file('pthread/test_pthread_run_script.c'))

    # Run the test again with PROXY_TO_PTHREAD
    self.setup_node_pthreads()
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.do_runf(test_file('pthread/test_pthread_run_script.c'))

  def test_tcgetattr(self):
    self.do_runf(test_file('termios/test_tcgetattr.c'), 'success')

  def test_time(self):
    self.do_core_test('test_time.cpp')
    for tz in ['EST+05EDT', 'UTC+0', 'CET']:
      print('extra tz test:', tz)
      with env_modify({'TZ': tz}):
        # Run the test with different time zone settings if
        # possible. It seems that the TZ environment variable does not
        # work all the time (at least it's not well respected by
        # Node.js on Windows), but it does no harm either.
        self.do_core_test('test_time.cpp')

  def test_timeb(self):
    # Confirms they are called in reverse order
    self.do_core_test('test_timeb.c')

  def test_time_c(self):
    self.do_core_test('test_time_c.c')

  def test_gmtime(self):
    self.do_core_test('test_gmtime.c')

  def test_strptime_tm(self):
    self.do_core_test('test_strptime_tm.c')

  def test_strptime_days(self):
    self.do_core_test('test_strptime_days.c')

  def test_strptime_reentrant(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_strptime_reentrant.c')

  def test_strftime(self):
    self.do_core_test('test_strftime.cpp')

  def test_trickystring(self):
    self.do_core_test('test_trickystring.c')

  def test_statics(self):
    self.do_core_test('test_statics.cpp')

  def test_copyop(self):
    # clang generated code is vulnerable to this, as it uses
    # memcpy for assignments, with hardcoded numbers of bytes
    # (llvm-gcc copies items one by one).
    self.do_core_test('test_copyop.cpp')

  def test_memcpy_memcmp(self):
    def check(output):
      output = output.replace('\n \n', '\n') # remove extra node output
      return hashlib.sha1(output.encode('utf-8')).hexdigest()

    self.do_core_test('test_memcpy_memcmp.c', output_nicerizer=check)

  def test_memcpy2(self):
    self.do_core_test('test_memcpy2.c')

  def test_memcpy3(self):
    self.do_core_test('test_memcpy3.c')

  @also_with_standalone_wasm()
  def test_memcpy_alignment(self):
    self.do_runf(test_file('test_memcpy_alignment.cpp'), 'OK.')

  def test_memset_alignment(self):
    self.do_runf(test_file('test_memset_alignment.cpp'), 'OK.')

  def test_memset(self):
    self.do_core_test('test_memset.c')

  def test_getopt(self):
    self.do_core_test('test_getopt.c', args=['-t', '12', '-n', 'foobar'])

  def test_getopt_long(self):
    self.do_core_test('test_getopt_long.c', args=['--file', 'foobar', '-b'])

  def test_memmove(self):
    self.do_core_test('test_memmove.c')

  def test_memmove2(self):
    self.do_core_test('test_memmove2.c')

  def test_memmove3(self):
    self.do_core_test('test_memmove3.c')

  def test_flexarray_struct(self):
    self.do_core_test('test_flexarray_struct.c')

  def test_bsearch(self):
    self.do_core_test('test_bsearch.c')

  def test_stack_overflow(self):
    self.set_setting('ASSERTIONS', 2)
    self.do_runf(test_file('core/stack_overflow.c'), 'Aborted(stack overflow', assert_returncode=NON_ZERO)

  def test_stackAlloc(self):
    self.do_core_test('stackAlloc.cpp')

  def test_nestedstructs(self):
    src = r'''
      #include <stdio.h>
      #include "emscripten.h"

      struct base {
        int x;
        float y;
        union {
          int a;
          float b;
        };
        char c;
      };

      struct hashtableentry {
        int key;
        base data;
      };

      struct hashset {
        typedef hashtableentry entry;
        struct chain { entry elem; chain *next; };
      //  struct chainchunk { chain chains[100]; chainchunk *next; };
      };

      struct hashtable : hashset {
        hashtable() {
          base b;
          entry e;
          chain c;
          printf("*%zu,%ld,%ld,%ld,%ld,%ld|%zu,%ld,%ld,%ld,%ld,%ld,%ld,%ld|%zu,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld*\n",
            sizeof(base),
            long(&b.x) - long(&b),
            long(&b.y) - long(&b),
            long(&b.a) - long(&b),
            long(&b.b) - long(&b),
            long(&b.c) - long(&b),
            sizeof(hashtableentry),
            long(&e.key) - long(&e),
            long(&e.data) - long(&e),
            long(&e.data.x) - long(&e),
            long(&e.data.y) - long(&e),
            long(&e.data.a) - long(&e),
            long(&e.data.b) - long(&e),
            long(&e.data.c) - long(&e),
            sizeof(hashset::chain),
            long(&c.elem) - long(&c),
            long(&c.next) - long(&c),
            long(&c.elem.key) - long(&c),
            long(&c.elem.data) - long(&c),
            long(&c.elem.data.x) - long(&c),
            long(&c.elem.data.y) - long(&c),
            long(&c.elem.data.a) - long(&c),
            long(&c.elem.data.b) - long(&c),
            long(&c.elem.data.c) - long(&c)
          );
        }
      };

      struct B { char buffer[62]; int last; char laster; char laster2; };

      struct Bits {
        unsigned short A : 1;
        unsigned short B : 1;
        unsigned short C : 1;
        unsigned short D : 1;
        unsigned short x1 : 1;
        unsigned short x2 : 1;
        unsigned short x3 : 1;
        unsigned short x4 : 1;
      };

      int main() {
        hashtable t;

        // Part 2 - the char[] should be compressed, BUT have a padding space at the end so the next
        // one is aligned properly. Also handle char; char; etc. properly.
        B b;
        printf("*%ld,%ld,%ld,%ld,%ld,%ld,%ld,%zu*\n", long(&b.buffer) - long(&b),
                                                      long(&b.buffer[0]) - long(&b),
                                                      long(&b.buffer[1]) - long(&b),
                                                      long(&b.buffer[2]) - long(&b),
                                                      long(&b.last) - long(&b),
                                                      long(&b.laster) - long(&b),
                                                      long(&b.laster2) - long(&b),
                                                      sizeof(B));

        // Part 3 - bitfields, and small structures
        printf("*%zu*\n", sizeof(Bits));
        return 0;
      }
      '''
    # Bloated memory; same layout as C/C++
    if self.is_wasm64():
      expected = '*16,0,4,8,8,12|20,0,4,4,8,12,12,16|32,0,24,0,4,4,8,12,12,16*\n*0,0,1,2,64,68,69,72*\n*2*'
    else:
      expected = '*16,0,4,8,8,12|20,0,4,4,8,12,12,16|24,0,20,0,4,4,8,12,12,16*\n*0,0,1,2,64,68,69,72*\n*2*'
    self.do_run(src, expected)

  def prep_dlfcn_main(self, libs=None):
    if libs is None:
      libs = ['liblib.so']
    self.clear_setting('SIDE_MODULE')
    # Link against the side modules but don't load them on startup.
    self.set_setting('NO_AUTOLOAD_DYLIBS')
    self.emcc_args += libs
    # This means we can use MAIN_MODULE=2 without needing to explicitly
    # specify EXPORTED_FUNCTIONS.
    self.set_setting('MAIN_MODULE', 2)

  def build_dlfcn_lib(self, filename, outfile='liblib.so', emcc_args=None):
    self.clear_setting('MAIN_MODULE')
    self.set_setting('SIDE_MODULE')
    cmd = [compiler_for(filename), filename, '-o', outfile] + self.get_emcc_args()
    if emcc_args:
      cmd += emcc_args
    self.run_process(cmd)

  @needs_dylink
  def test_dlfcn_missing(self):
    self.set_setting('MAIN_MODULE')
    self.set_setting('ASSERTIONS')
    src = r'''
      #include <dlfcn.h>
      #include <stdio.h>
      #include <assert.h>

      int main() {
        void* lib_handle = dlopen("libfoo.so", RTLD_NOW);
        assert(!lib_handle);
        printf("error: %s\n", dlerror());
        return 0;
      }
      '''

    if self.js_engines == [config.V8_ENGINE]:
      expected = "error: Could not load dynamic lib: libfoo.so\nError: Error reading file"
    else:
      expected = "error: Could not load dynamic lib: libfoo.so\nError: ENOENT: no such file or directory"
    self.do_run(src, expected)

  @needs_dylink
  @parameterized({
    '': ([],),
    'pthreads': (['-pthread', '-sEXIT_RUNTIME', '-sPROXY_TO_PTHREAD', '-Wno-experimental'],),
  })
  def test_dlfcn_basic(self, args):
    if args:
      self.setup_node_pthreads()
    self.emcc_args += args
    create_file('liblib.cpp', '''
      #include <cstdio>

      class Foo {
      public:
        Foo() {
          puts("Constructing lib object.");
        }
      };

      Foo side_global;
      ''')
    self.build_dlfcn_lib('liblib.cpp')

    self.prep_dlfcn_main()
    src = '''
      #include <cstdio>
      #include <dlfcn.h>

      class Bar {
      public:
        Bar() {
          puts("Constructing main object.");
        }
      };

      Bar global;

      int main() {
        dlopen("liblib.so", RTLD_NOW);
        return 0;
      }
      '''
    self.do_run(src, 'Constructing main object.\nConstructing lib object.\n')

  @needs_dylink
  def test_dlfcn_i64(self):
    create_file('liblib.c', '''
      #include <inttypes.h>

      int64_t foo(int x) {
        return (long long)x / (long long)1234;
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    src = r'''
      #include <inttypes.h>
      #include <stdio.h>
      #include <stdlib.h>
      #include <dlfcn.h>

      typedef int64_t (*int64func)(int);

      int main() {
        void *lib_handle = dlopen("liblib.so", RTLD_NOW);
        if (!lib_handle) {
          puts(dlerror());
          abort();
        }
        printf("dll handle: %p\n", lib_handle);
        int64func x = (int64func)dlsym(lib_handle, "foo");
        printf("foo func handle: %p\n", x);
        if (!x) {
          printf("dlsym failed: %s\n", dlerror());
          return 1;
        }
        printf("|%lld|\n", x(81234567));
        return 0;
      }
      '''
    self.do_run(src, '|65830|')

  @needs_dylink
  @disabled('EM_ASM in not yet supported in SIDE_MODULE')
  def test_dlfcn_em_asm(self):
    create_file('liblib.cpp', '''
      #include <emscripten.h>
      class Foo {
      public:
        Foo() {
          EM_ASM( out("Constructing lib object.") );
        }
      };
      Foo global;
      ''')
    self.build_dlfcn_lib('liblib.cpp')

    self.prep_dlfcn_main()
    src = '''
      #include <emscripten.h>
      #include <dlfcn.h>
      class Bar {
      public:
        Bar() {
          EM_ASM( out("Constructing main object.") );
        }
      };
      Bar global;
      int main() {
        dlopen("liblib.so", RTLD_NOW);
        EM_ASM( out("All done.") );
        return 0;
      }
      '''
    self.do_run(src, 'Constructing main object.\nConstructing lib object.\nAll done.\n')

  @needs_dylink
  def test_dlfcn_qsort(self):
    create_file('liblib.c', '''
      int lib_cmp(const void* left, const void* right) {
        const int* a = (const int*) left;
        const int* b = (const int*) right;
        if(*a > *b) return 1;
        else if(*a == *b) return  0;
        else return -1;
      }

      typedef int (*CMP_TYPE)(const void*, const void*);

      CMP_TYPE get_cmp() {
        return lib_cmp;
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    src = '''
      #include <stdio.h>
      #include <stdlib.h>
      #include <dlfcn.h>

      typedef int (*CMP_TYPE)(const void*, const void*);

      int main_cmp(const void* left, const void* right) {
        const int* a = (const int*) left;
        const int* b = (const int*) right;
        if(*a < *b) return 1;
        else if(*a == *b) return  0;
        else return -1;
      }

      int main() {
        void* lib_handle;
        CMP_TYPE (*getter_ptr)();
        CMP_TYPE lib_cmp_ptr;
        int arr[5] = {4, 2, 5, 1, 3};

        qsort((void*)arr, 5, sizeof(int), main_cmp);
        printf("Sort with main comparison: ");
        for (int i = 0; i < 5; i++) {
          printf("%d ", arr[i]);
        }
        printf("\\n");

        lib_handle = dlopen("liblib.so", RTLD_NOW);
        if (lib_handle == NULL) {
          printf("Could not load lib.\\n");
          return 1;
        }
        getter_ptr = (CMP_TYPE (*)()) dlsym(lib_handle, "get_cmp");
        if (getter_ptr == NULL) {
          printf("Could not find func.\\n");
          return 1;
        }
        lib_cmp_ptr = getter_ptr();
        qsort((void*)arr, 5, sizeof(int), lib_cmp_ptr);
        printf("Sort with lib comparison: ");
        for (int i = 0; i < 5; i++) {
          printf("%d ", arr[i]);
        }
        printf("\\n");

        return 0;
      }
      '''
    self.do_run(src, 'Sort with main comparison: 5 4 3 2 1 *Sort with lib comparison: 1 2 3 4 5 *',
                output_nicerizer=lambda x: x.replace('\n', '*'))

  @needs_dylink
  def test_dlfcn_data_and_fptr(self):
    create_file('liblib.c', r'''
      #include <stdio.h>

      int theglobal = 42;

      extern void parent_func(); // a function that is defined in the parent

      int* lib_get_global_addr() {
        return &theglobal;
      }

      void lib_fptr() {
        printf("Second calling lib_fptr from main.\n");
        parent_func();
        // call it also through a pointer, to check indexizing
        void (*p_f)();
        p_f = parent_func;
        p_f();
      }

      void (*func(int x, void(*fptr)()))() {
        printf("In func: %d\n", x);
        fptr();
        return lib_fptr;
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    src = r'''
      #include <stdio.h>
      #include <dlfcn.h>
      #include <emscripten.h>

      typedef void (*FUNCTYPE(int, void(*)()))();

      FUNCTYPE func;

      void EMSCRIPTEN_KEEPALIVE parent_func() {
        printf("parent_func called from child\n");
      }

      void main_fptr() {
        printf("First calling main_fptr from lib.\n");
      }

      int main() {
        void* lib_handle;
        FUNCTYPE* func_fptr;

        // Test basic lib loading.
        lib_handle = dlopen("liblib.so", RTLD_NOW);
        if (lib_handle == NULL) {
          printf("Could not load lib.\n");
          return 1;
        }

        // Test looked up function.
        func_fptr = (FUNCTYPE*) dlsym(lib_handle, "func");
        // Load twice to test cache.
        func_fptr = (FUNCTYPE*) dlsym(lib_handle, "func");
        if (func_fptr == NULL) {
          printf("Could not find func.\n");
          return 1;
        }

        // Test passing function pointers across module bounds.
        void (*fptr)() = func_fptr(13, main_fptr);
        fptr();

        // Test global data.
        int* globaladdr = (int*) dlsym(lib_handle, "theglobal");
        if (globaladdr == NULL) {
          printf("Could not find global.\n");
          return 1;
        }

        printf("Var: %d\n", *globaladdr);

        return 0;
      }
      '''
    self.do_run(src, '''\
In func: 13
First calling main_fptr from lib.
Second calling lib_fptr from main.
parent_func called from child
parent_func called from child
Var: 42
''', force_c=True)

  @needs_dylink
  def test_dlfcn_varargs(self):
    # this test is not actually valid - it fails natively. the child should fail
    # to be loaded, not load and successfully see the parent print_ints func

    create_file('liblib.c', r'''
      void print_ints(int n, ...);
      void func() {
        print_ints(2, 13, 42);
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    src = r'''
      #include <stdarg.h>
      #include <stdio.h>
      #include <dlfcn.h>
      #include <assert.h>

      void print_ints(int n, ...) {
        va_list args;
        va_start(args, n);
        for (int i = 0; i < n; i++) {
          printf("%d\n", va_arg(args, int));
        }
        va_end(args);
      }

      int main() {
        void* lib_handle;
        void (*fptr)();

        print_ints(2, 100, 200);

        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle);
        fptr = (void (*)())dlsym(lib_handle, "func");
        fptr();

        return 0;
      }
      '''
    self.do_run(src, '100\n200\n13\n42\n', force_c=True)

  @needs_dylink
  @no_sanitize('contains ODR violation')
  def test_dlfcn_alignment_and_zeroing(self):
    self.set_setting('INITIAL_MEMORY', '16mb')
    create_file('liblib.c', r'''
      int prezero = 0;
      __attribute__((aligned(1024))) int superAligned = 12345;
      int postzero = 0;
      ''')
    self.build_dlfcn_lib('liblib.c')
    for i in range(10):
      curr = '%d.so' % i
      shutil.copyfile('liblib.so', curr)

    self.prep_dlfcn_main()
    self.set_setting('INITIAL_MEMORY', '128mb')
    create_file('src.c', r'''
      #include <stdio.h>
      #include <stdlib.h>
      #include <string.h>
      #include <dlfcn.h>
      #include <assert.h>
      #include <emscripten.h>

      int main() {
        printf("'prepare' memory with non-zero inited stuff\n");
        int num = 120 * 1024 * 1024; // total is 128; we'll use 5*5 = 25 at least, so allocate pretty much all of it
        void* mem = malloc(num);
        assert(mem);
        printf("setting this range to non-zero: %ld - %ld\n", (long)mem, ((long)mem) + num);
        memset(mem, 1, num);
        EM_ASM({
          var value = HEAP8[64*1024*1024];
          out('verify middle of memory is non-zero: ' + value);
          assert(value === 1);
        });
        free(mem);
        for (int i = 0; i < 10; i++) {
          char curr[] = "?.so";
          curr[0] = '0' + i;
          printf("loading %s\n", curr);
          void* lib_handle = dlopen(curr, RTLD_NOW);
          if (!lib_handle) {
            puts(dlerror());
            assert(0);
          }
          printf("getting superAligned\n");
          int* superAligned = (int*)dlsym(lib_handle, "superAligned");
          assert(superAligned);
          assert(((long)superAligned) % 1024 == 0); // alignment
          printf("checking value of superAligned, at %p\n", superAligned);
          assert(*superAligned == 12345); // value
          printf("getting prezero\n");
          int* prezero = (int*)dlsym(lib_handle, "prezero");
          assert(prezero);
          printf("checking value of prezero, at %p\n", prezero);
          assert(*prezero == 0);
          *prezero = 1;
          assert(*prezero != 0);
          printf("getting postzero\n");
          int* postzero = (int*)dlsym(lib_handle, "postzero");
          printf("checking value of postzero, at %p\n", postzero);
          assert(postzero);
          printf("checking value of postzero\n");
          assert(*postzero == 0);
          *postzero = 1;
          assert(*postzero != 0);
        }
        printf("success.\n");
        return 0;
      }
      ''')
    self.do_runf('src.c', 'success.\n')

  @needs_dylink
  def test_dlfcn_self(self):
    self.set_setting('MAIN_MODULE')
    self.set_setting('EXPORT_ALL')

    self.do_core_test('test_dlfcn_self.c')

    # check that we only export relevant things.
    # disable this in WasmFS as it adds a bunch of additional exports for its
    # own purposes internally TODO: when we focus on code size, we'll likely
    # want to look at this
    if self.get_setting('WASMFS'):
      return

    # sanitizers add a lot of extra symbols
    if is_sanitizing(self.emcc_args):
      return

    def get_data_exports(wasm):
      wat = self.get_wasm_text(wasm)
      lines = wat.splitlines()
      exports = [l for l in lines if l.strip().startswith('(export ')]
      data_exports = [l for l in exports if '(global ' in l]
      data_exports = [d.split()[1].strip('"') for d in data_exports]
      return data_exports

    data_exports = get_data_exports('test_dlfcn_self.wasm')
    # Certain exports are removed by wasm-emscripten-finalize, but this
    # tool is not run in all configurations, so ignore these exports.
    data_exports = [d for d in data_exports if d not in ('__start_em_asm', '__stop_em_asm')]
    data_exports = '\n'.join(sorted(data_exports)) + '\n'
    self.assertFileContents(test_file('core/test_dlfcn_self.exports'), data_exports)

  @needs_dylink
  def test_dlfcn_unique_sig(self):
    create_file('liblib.c', r'''
      #include <stdio.h>

      int myfunc(int a, int b, int c, int d, int e, int f, int g, int h, int i, int j, int k, int l, int m) {
        return 13;
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    create_file('main.c', r'''
      #include <assert.h>
      #include <stdio.h>
      #include <dlfcn.h>

      typedef int (*FUNCTYPE)(int, int, int, int, int, int, int, int, int, int, int, int, int);

      int main() {
        void *lib_handle;
        FUNCTYPE func_ptr;

        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle != NULL);

        func_ptr = (FUNCTYPE)dlsym(lib_handle, "myfunc");
        assert(func_ptr != NULL);
        assert(func_ptr(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0) == 13);

        puts("success");

        return 0;
      }
      ''')
    self.do_runf('main.c', 'success')

  @needs_dylink
  def test_dlfcn_info(self):
    create_file('liblib.c', r'''
      #include <stdio.h>

      int myfunc(int a, int b, int c, int d, int e, int f, int g, int h, int i, int j, int k, int l, int m) {
        return 13;
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    create_file('main.c', '''
      #include <assert.h>
      #include <stdio.h>
      #include <string.h>
      #include <dlfcn.h>

      typedef int (*FUNCTYPE)(int, int, int, int, int, int, int, int, int, int, int, int, int);

      int main() {
        void *lib_handle;
        FUNCTYPE func_ptr;

        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle != NULL);

        func_ptr = (FUNCTYPE)dlsym(lib_handle, "myfunc");
        assert(func_ptr != NULL);
        assert(func_ptr(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0) == 13);

        /* Verify that we don't corrupt func_ptr when calling dladdr.  */
        Dl_info info;
        memset(&info, 0, sizeof(info));
        int rtn = dladdr(func_ptr, &info);
        assert(rtn == 0);

        assert(func_ptr != NULL);
        assert(func_ptr(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0) == 13);

        puts("success");

        return 0;
      }
      ''')
    self.do_runf('main.c', 'success')

  @needs_dylink
  def test_dlfcn_stacks(self):
    create_file('liblib.c', r'''
      #include <assert.h>
      #include <stdio.h>
      #include <string.h>

      int myfunc(const char *input) {
        char bigstack[1024] = { 0 };

        // make sure we didn't just trample the stack!
        assert(!strcmp(input, "foobar"));

        snprintf(bigstack, sizeof(bigstack), "%s", input);
        return strlen(bigstack);
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    create_file('main.c', '''
      #include <assert.h>
      #include <stdio.h>
      #include <dlfcn.h>
      #include <string.h>

      typedef int (*FUNCTYPE)(const char *);

      int main() {
        void *lib_handle;
        FUNCTYPE func_ptr;
        char str[128];

        snprintf(str, sizeof(str), "foobar");

        // HACK: Use strcmp in the main executable so that it doesn't get optimized out and the dynamic library
        //       is able to use it.
        assert(!strcmp(str, "foobar"));

        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle != NULL);

        func_ptr = (FUNCTYPE)dlsym(lib_handle, "myfunc");
        assert(func_ptr != NULL);
        assert(func_ptr(str) == 6);

        puts("success");

        return 0;
      }
      ''')
    self.do_runf('main.c', 'success')

  @needs_dylink
  def test_dlfcn_funcs(self):
    create_file('liblib.c', r'''
      #include <assert.h>
      #include <stdio.h>
      #include <string.h>

      typedef void (*voidfunc)(void);
      typedef void (*intfunc)(int);

      void callvoid(voidfunc f) { f(); }
      void callint(intfunc f, int x) { f(x); }

      void void_0() { printf("void 0\n"); }
      void void_1() { printf("void 1\n"); }
      voidfunc getvoid(int i) {
        switch(i) {
          case 0: return void_0;
          case 1: return void_1;
          default: return NULL;
        }
      }

      void int_0(int x) { printf("int 0 %d\n", x); }
      void int_1(int x) { printf("int 1 %d\n", x); }
      intfunc getint(int i) {
        switch(i) {
          case 0: return int_0;
          case 1: return int_1;
          default: return NULL;
        }
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    create_file('main.c', r'''
      #include <assert.h>
      #include <stdio.h>
      #include <dlfcn.h>

      typedef void (*voidfunc)();
      typedef void (*intfunc)(int);

      typedef void (*voidcaller)(voidfunc);
      typedef void (*intcaller)(intfunc, int);

      typedef voidfunc (*voidgetter)(int);
      typedef intfunc (*intgetter)(int);

      void void_main() { printf("void_main.\n"); }
      void int_main(int x) { printf("int_main %d\n", x); }

      int main() {
        printf("go\n");
        void *lib_handle;
        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle != NULL);

        voidcaller callvoid = (voidcaller)dlsym(lib_handle, "callvoid");
        assert(callvoid != NULL);
        callvoid(void_main);

        intcaller callint = (intcaller)dlsym(lib_handle, "callint");
        assert(callint != NULL);
        callint(int_main, 201);

        voidgetter getvoid = (voidgetter)dlsym(lib_handle, "getvoid");
        assert(getvoid != NULL);
        callvoid(getvoid(0));
        callvoid(getvoid(1));

        intgetter getint = (intgetter)dlsym(lib_handle, "getint");
        assert(getint != NULL);
        callint(getint(0), 54);
        callint(getint(1), 9000);

        assert(getint(1000) == NULL);

        puts("ok");
        return 0;
      }
      ''')
    self.do_runf('main.c', '''go
void_main.
int_main 201
void 0
void 1
int 0 54
int 1 9000
ok
''')

  @needs_dylink
  def test_dlfcn_mallocs(self):
    # will be exhausted without functional malloc/free
    self.set_setting('INITIAL_MEMORY', '64mb')

    create_file('liblib.c', r'''
      #include <assert.h>
      #include <stdio.h>
      #include <string.h>
      #include <stdlib.h>

      void *mallocproxy(int n) { return malloc(n); }
      void freeproxy(void *p) { free(p); }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    self.do_runf(test_file('dlmalloc_proxy.c'), '*293,153*')

  @needs_dylink
  def test_dlfcn_longjmp(self):
    create_file('liblib.c', r'''
      #include <setjmp.h>
      #include <stdio.h>

      void jumpy(jmp_buf buf) {
        static int i = 0;
        i++;
        if (i == 10) longjmp(buf, i);
        printf("pre %d\n", i);
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    create_file('main.c', r'''
      #include <assert.h>
      #include <stdio.h>
      #include <dlfcn.h>
      #include <setjmp.h>

      typedef void (*jumpfunc)(jmp_buf);

      int main() {
        printf("go!\n");

        void *lib_handle;
        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle != NULL);

        jumpfunc jumpy = (jumpfunc)dlsym(lib_handle, "jumpy");
        assert(jumpy);

        jmp_buf buf;
        int jmpval = setjmp(buf);
        if (jmpval == 0) {
          while (1) jumpy(buf);
        } else {
          printf("out!\n");
        }

        return 0;
      }
      ''')
    self.do_runf('main.c', '''go!
pre 1
pre 2
pre 3
pre 4
pre 5
pre 6
pre 7
pre 8
pre 9
out!
''')

  # TODO: make this work. need to forward tempRet0 across modules
  # TODO Enable @with_both_eh_sjlj (the test is not working now)
  @needs_dylink
  def zzztest_dlfcn_exceptions(self):
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)

    create_file('liblib.cpp', r'''
      extern "C" {
      int ok() {
        return 65;
      }
      int fail() {
        throw 123;
      }
      }
      ''')
    self.build_dlfcn_lib('liblib.cpp')

    self.prep_dlfcn_main()
    src = r'''
      #include <assert.h>
      #include <stdio.h>
      #include <dlfcn.h>

      typedef int (*intfunc)();

      int main() {
        printf("go!\n");

        void *lib_handle;
        lib_handle = dlopen("liblib.so", RTLD_NOW);
        assert(lib_handle != NULL);

        intfunc okk = (intfunc)dlsym(lib_handle, "ok");
        intfunc faill = (intfunc)dlsym(lib_handle, "fail");
        assert(okk && faill);

        try {
          printf("ok: %d\n", okk());
        } catch(...) {
          printf("wha\n");
        }

        try {
          printf("fail: %d\n", faill());
        } catch(int x) {
          printf("int %d\n", x);
        }

        try {
          printf("fail: %d\n", faill());
        } catch(double x) {
          printf("caught %f\n", x);
        }

        return 0;
      }
      '''
    self.do_run(src, '''go!
ok: 65
int 123
ok
''')

  @needs_dylink
  def test_dlfcn_handle_alloc(self):
    # verify that dlopen does not allocate already used handles
    dirname = self.get_dir()

    def indir(name):
      return os.path.join(dirname, name)

    create_file('a.cpp', r'''
      #include <stdio.h>

      static class A {
      public:
        A() {
          puts("a: loaded");
        }
      } _;
    ''')

    create_file('b.cpp', r'''
      #include <stdio.h>

      static class B {
      public:
        B() {
          puts("b: loaded");
        }
      } _;
    ''')

    self.build_dlfcn_lib('a.cpp', outfile='liba.so')
    self.build_dlfcn_lib('b.cpp', outfile='libb.so')

    self.set_setting('MAIN_MODULE')
    self.clear_setting('SIDE_MODULE')

    create_file('main.c', r'''
      #include <dlfcn.h>
      #include <assert.h>
      #include <stddef.h>

      int main() {
        void *liba, *libb, *liba2, *libb2;
        int err;

        liba = dlopen("liba.so", RTLD_NOW);
        assert(liba != NULL);
        libb = dlopen("libb.so", RTLD_NOW);
        assert(libb != NULL);

        // Test that opening libb a second times gives the same handle
        libb2 = dlopen("libb.so", RTLD_NOW);
        assert(libb == libb2);

        err = dlclose(liba);
        assert(!err);

        liba2 = dlopen("liba.so", RTLD_NOW);
        assert(liba2 != libb);

        return 0;
      }
      ''')
    self.do_runf('main.c', 'a: loaded\nb: loaded\n')

  @needs_dylink
  @needs_non_trapping_float_to_int
  def test_dlfcn_feature_in_lib(self):
    self.emcc_args.append('-mnontrapping-fptoint')

    create_file('liblib.c', r'''
        int magic(float x) {
          return __builtin_wasm_trunc_saturate_s_i32_f32(x);
        }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    src = r'''
      #include <dlfcn.h>
      #include <stdio.h>
      #include <stdlib.h>

      typedef int (*fi)(float);

      int main() {
        void *lib_handle = dlopen("liblib.so", RTLD_NOW);
        if (!lib_handle) {
          puts(dlerror());
          abort();
        }
        fi x = (fi)dlsym(lib_handle, "magic");
        if (!x) {
          puts(dlerror());
          abort();
        }
        printf("float: %d.\n", x(42.99));
        return 0;
      }
      '''
    self.do_run(src, 'float: 42.\n')

  @needs_dylink
  @no_wasm64('TODO: asyncify for wasm64')
  def test_dlfcn_asyncify(self):
    self.set_setting('ASYNCIFY')

    create_file('liblib.c', r'''
      #include <stdio.h>
      #include <emscripten/emscripten.h>

      int side_module_run() {
        printf("before sleep\n");
        emscripten_sleep(1000);
        printf("after sleep\n");
        return 42;
      }
      ''')
    self.build_dlfcn_lib('liblib.c')

    self.prep_dlfcn_main()
    src = r'''
      #include <stdio.h>
      #include <dlfcn.h>

      typedef int (*func_t)();

      int main(int argc, char **argv) {
        void *_dlHandle = dlopen("liblib.so", RTLD_NOW | RTLD_LOCAL);
        func_t my_func = (func_t)dlsym(_dlHandle, "side_module_run");
        printf("%d\n", my_func());
        return 0;
      }
      '''
    self.do_run(src, 'before sleep\nafter sleep\n42\n')

  @needs_dylink
  def test_dlfcn_rtld_local(self):
    create_file('liba.c', r'''
      #include <stdio.h>

      void func_b();

      void func_a() {
        printf("func_a\n");
        // Call a function from a dependent DSO. This symbol should
        // be available here even though liba itself is loaded with RTLD_LOCAL.
        func_b();
      }
      ''')

    create_file('libb.c', r'''
      #include <stdio.h>

      void func_b() {
        printf("func_b\n");
      }
    ''')

    self.build_dlfcn_lib('libb.c', outfile='libb.so')
    self.build_dlfcn_lib('liba.c', outfile='liba.so', emcc_args=['libb.so'])

    self.prep_dlfcn_main(['liba.so', 'libb.so', '-L.'])
    create_file('main.c', r'''
      #include <assert.h>
      #include <dlfcn.h>
      #include <stdio.h>

      int main() {
        printf("main\n");
        void* handle = dlopen("liba.so", RTLD_NOW|RTLD_LOCAL);
        assert(handle);

        void (*f)();
        f = dlsym(handle, "func_a");
        assert(f);
        f();

        // Verify that symbols from liba.so and libb.so are not globally
        // visible.
        void* func_a = dlsym(RTLD_DEFAULT, "func_a");
        assert(func_a == NULL);
        void* func_b = dlsym(RTLD_DEFAULT, "func_b");
        assert(func_b == NULL);

        printf("done\n");
        return 0;
      }
      ''')

    self.do_runf('main.c', 'main\nfunc_a\nfunc_b\ndone\n')

  def dylink_test(self, main, side, expected=None, header=None, force_c=False,
                  main_module=2, **kwargs):
    # Same as dylink_testf but take source code in string form
    if not isinstance(side, list):
      side_file = 'liblib.cpp' if not force_c else 'liblib.c'
      create_file(side_file, side)
      side = side_file
    if not isinstance(main, list):
      main_file = 'main.cpp' if not force_c else 'main.c'
      create_file(main_file, main)
      main = main_file
    if header:
      create_file('header.h', header)

    return self.dylink_testf(main, side, expected, main_module=main_module, **kwargs)

  def dylink_testf(self, main, side=None, expected=None, force_c=False, main_emcc_args=None,
                   main_module=2,
                   so_dir='',
                   so_name='liblib.so',
                   need_reverse=True, **kwargs):
    main_emcc_args = main_emcc_args or []
    self.maybe_closure()
    # Same as dylink_test but takes source code as filenames on disc.
    old_args = self.emcc_args.copy()
    if not expected:
      outfile = shared.replace_suffix(main, '.out')
      expected = read_file(outfile)
    if not side:
      side, ext = os.path.splitext(main)
      side += '_side' + ext

    # side settings
    self.clear_setting('MAIN_MODULE')
    self.set_setting('SIDE_MODULE')
    side_suffix = 'wasm' if self.is_wasm() else 'js'
    if isinstance(side, list):
      out_file = 'liblib.' + side_suffix
      # side is just a library
      self.run_process([EMCC] + side + self.get_emcc_args() + ['-o', out_file])
    else:
      out_file = self.build(side, js_outfile=(side_suffix == 'js'))
    shutil.move(out_file, os.path.join(so_dir, so_name))

    # main settings
    self.set_setting('MAIN_MODULE', main_module)
    self.clear_setting('SIDE_MODULE')
    self.emcc_args += main_emcc_args
    self.emcc_args.append(os.path.join(so_dir, so_name))

    if force_c:
      self.emcc_args.append('-nostdlib++')

    if isinstance(main, list):
      # main is just a library
      delete_file('main.js')
      self.run_process([EMCC] + main + self.get_emcc_args() + ['-o', 'main.js'])
      self.do_run('main.js', expected, no_build=True, **kwargs)
    else:
      self.do_runf(main, expected, force_c=force_c, **kwargs)

    self.emcc_args = old_args

    if need_reverse:
      print('flip')
      # Test the reverse as well.  There we flip the role of the side module and main module.
      # - We add --no-entry since the side module doesn't have a `main`
      self.dylink_testf(side, main, expected, force_c, main_emcc_args + ['--no-entry'],
                        main_module, so_dir, so_name, need_reverse=False, **kwargs)

  def do_basic_dylink_test(self, **kwargs):
    self.dylink_test(r'''
      #include <stdio.h>
      #include "header.h"

      int main() {
        printf("other says %d.\n", sidey());
        return 0;
      }
    ''', '''
      #include "header.h"

      int sidey() {
        return 11;
      }
    ''', 'other says 11.', 'int sidey();', force_c=True, **kwargs)

  @needs_dylink
  @crossplatform
  def test_dylink_basics(self):
    self.do_basic_dylink_test(need_reverse=False)
    self.verify_in_strict_mode('main.js')

  @needs_dylink
  def test_dylink_basics_no_modify(self):
    if self.is_optimizing():
      self.skipTest('no modify mode only works with non-optimizing builds')
    if self.get_setting('MEMORY64') == 2:
      self.skipTest('MEMORY64=2 always requires module re-writing')
    self.set_setting('WASM_BIGINT')
    self.set_setting('ERROR_ON_WASM_CHANGES_AFTER_LINK')
    self.do_basic_dylink_test()

  @needs_dylink
  def test_dylink_no_export(self):
    self.set_setting('NO_DECLARE_ASM_MODULE_EXPORTS')
    self.do_basic_dylink_test()

  @needs_dylink
  def test_dylink_memory_growth(self):
    if not self.is_wasm():
      self.skipTest('wasm only')
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.do_basic_dylink_test()

  @needs_dylink
  @no_asan('SAFE_HEAP cannot be used with ASan')
  def test_dylink_safe_heap(self):
    self.set_setting('SAFE_HEAP')
    self.do_basic_dylink_test()

  @needs_dylink
  def test_dylink_locate_file(self):
    so_dir = 'so_dir'
    so_name = 'liblib.so'
    os.mkdir(so_dir)
    create_file('pre.js', '''
    Module['locateFile'] = function(f) {
      if (f === '%s') {
        return '%s/' + f;
      } else {
        return f;
      }
    };
    ''' % (so_name, so_dir))
    self.do_basic_dylink_test(so_dir=so_dir, so_name=so_name, main_emcc_args=['--pre-js', 'pre.js'])

  @needs_dylink
  def test_dylink_function_pointer_equality(self):
    self.dylink_test(r'''
      #include <stdio.h>
      #include "header.h"

      int main() {
        void* puts_side = get_address();
        printf("main module address %p.\n", &puts);
        printf("side module address address %p.\n", puts_side);
        if (&puts == puts_side)
          printf("success\n");
        else
          printf("failure\n");
        return 0;
      }
    ''', '''
      #include <stdio.h>
      #include "header.h"

      void* get_address() {
        return (void*)&puts;
      }
    ''', 'success', header='void* get_address();', force_c=True)

  @needs_dylink
  def test_dylink_floats(self):
    self.dylink_test(r'''
      #include <stdio.h>
      extern float sidey();
      int main() {
        printf("other says %.2f.\n", sidey()+1);
        return 0;
      }
    ''', '''
      float sidey() { return 11.5; }
    ''', 'other says 12.50', force_c=True)

  @needs_dylink
  def test_dylink_printf(self):
    self.dylink_test(r'''
      #include <stdio.h>
     void sidey();
      int main() {
        printf("hello from main\n");
        sidey();
        return 0;
      }
    ''', r'''
      #include <stdio.h>
      void sidey() {
        printf("hello from side\n");
      }
    ''', 'hello from main\nhello from side\n', force_c=True)

  # Verify that a function pointer can be passed back and forth and invoked
  # on both sides.
  @needs_dylink
  def test_dylink_funcpointer(self):
    self.dylink_test(
      main=r'''
      #include <stdio.h>
      #include <assert.h>
      #include "header.h"
      intfunc sidey(intfunc f);
      void a(int arg) { printf("hello from funcptr: %d\n", arg); }
      int main() {
        intfunc b = sidey(a);
        assert(a == b);
        b(0);
        return 0;
      }
      ''',
      side='''
      #include "header.h"
      intfunc sidey(intfunc f) { f(1); return f; }
      ''',
      expected='hello from funcptr: 1\nhello from funcptr: 0\n',
      header='typedef void (*intfunc)(int );', force_c=True)

  @needs_dylink
  # test dynamic linking of a module with multiple function pointers, stored
  # statically
  def test_dylink_static_funcpointers(self):
    self.dylink_test(
      main=r'''
      #include <stdio.h>
      #include "header.h"
      void areturn0() { printf("hello 0\n"); }
      void areturn1() { printf("hello 1\n"); }
      void areturn2() { printf("hello 2\n"); }
      voidfunc func_ptrs[3] = { areturn0, areturn1, areturn2 };
      int main(int argc, char **argv) {
        sidey(func_ptrs[0]);
        sidey(func_ptrs[1]);
        sidey(func_ptrs[2]);
        return 0;
      }
      ''',
      side='''
      #include "header.h"
      void sidey(voidfunc f) { f(); }
      ''',
      expected='hello 0\nhello 1\nhello 2\n',
      header='typedef void (*voidfunc)(); void sidey(voidfunc f);', force_c=True)

  @needs_dylink
  def test_dylink_funcpointers_wrapper(self):
    self.dylink_test(
      main=r'''\
      #include <stdio.h>
      #include "header.h"
      int main(int argc, char **argv) {
        charfunc f1 = emscripten_run_script;
        f1("console.log('one')");
        charfunc f2 = get();
        f2("console.log('two')");
        return 0;
      }
      ''',
      side='''\
      #include "header.h"
      charfunc get() {
        return emscripten_run_script;
      }
      ''',
      expected='one\ntwo\n',
      header='''\
      #include <emscripten.h>
      typedef void (*charfunc)(const char*);
      extern charfunc get();
      ''', force_c=True)

  @needs_dylink
  def test_dylink_static_funcpointer_float(self):
    self.dylink_test(
      main=r'''\
      #include <stdio.h>
      #include "header.h"
      int sidey(floatfunc f);
      float func1(float f) { printf("hello 1: %f\n", f); return 0; }
      floatfunc f1 = &func1;
      int main(int argc, char **argv) {
        printf("got: %d\n", sidey(f1));
        f1(12.34);
        return 0;
      }
      ''',
      side='''\
      #include "header.h"
      int sidey(floatfunc f) { f(56.78); return 1; }
      ''',
      expected='hello 1: 56.779999\ngot: 1\nhello 1: 12.340000\n',
      header='typedef float (*floatfunc)(float);', force_c=True)

  @needs_dylink
  def test_missing_signatures(self):
    create_file('test_sig.c', r'''#include <emscripten.h>
                                       int main() {
                                         return 0 == ( (long)&emscripten_run_script_string +
                                                       (long)&emscripten_run_script );
                                       }''')
    self.set_setting('MAIN_MODULE', 1)
    # also test main module with 4GB of memory. we need to emit a "maximum"
    # clause then, even though 4GB is the maximum; see
    # https://github.com/emscripten-core/emscripten/issues/14130
    self.set_setting('ALLOW_MEMORY_GROWTH', '1')
    self.set_setting('MAXIMUM_MEMORY', '4GB')
    self.do_runf('test_sig.c', '')

  @needs_dylink
  def test_dylink_global_init(self):
    self.dylink_test(r'''
      #include <stdio.h>
      struct Class {
        Class() { printf("a new Class\n"); }
      };
      static Class c;
      int main() {
        return 0;
      }
    ''', r'''
      void nothing() {}
    ''', 'a new Class\n')

  @needs_dylink
  def test_dylink_global_inits(self):
    def test():
      self.dylink_test(header=r'''
        #include <stdio.h>
        struct Class {
          Class(const char *name) { printf("new %s\n", name); }
        };
      ''', main=r'''
        #include "header.h"
        static Class c("main");
        int main() {
          return 0;
        }
      ''', side=r'''
        #include "header.h"
        static Class c("side");
      ''', expected=['new main\nnew side\n', 'new side\nnew main\n'])
    test()

    print('check warnings')
    self.set_setting('ASSERTIONS', 2)
    test()
    # TODO: this in wasm
    # full = self.run_js('src.js')
    # self.assertNotContained('already exists', full)

  @needs_dylink
  def test_dylink_i64(self):
    self.dylink_test(r'''
      #include <stdio.h>
      #include <stdint.h>
      extern int64_t sidey();
      int main() {
        printf("other says %lld.\n", sidey());
        return 0;
      }
    ''', '''
      #include <stdint.h>
      int64_t sidey() {
        return 42;
      }
    ''', 'other says 42.', force_c=True)

  @all_engines
  @needs_dylink
  def test_dylink_i64_b(self):
    self.dylink_test(r'''
      #include <stdio.h>
      #include <stdint.h>
      extern int64_t sidey();
      int64_t testAdd(int64_t a) {
        return a + 1;
      }
      int64_t testAddB(int a) {
        return a + 1;
      }
      typedef int64_t (*testAddHandler)(int64_t);
      testAddHandler h = &testAdd;
      typedef int64_t (*testAddBHandler)(int);
      testAddBHandler hb = &testAddB;
      int main() {
        printf("other says %lld.\n", sidey());
        int64_t r = h(42);
        printf("my fp says: %lld.\n", r);
        int64_t rb = hb(42);
        printf("my second fp says: %lld.\n", r);
      }
    ''', '''
      #include <stdint.h>
      int64_t sidey() {
        volatile int64_t x = 0x12345678abcdef12LL;
        x += x % 17;
        x = 18 - x;
        return x;
      }
    ''', 'other says -1311768467750121224.\nmy fp says: 43.\nmy second fp says: 43.', force_c=True)

  @needs_dylink
  @also_with_wasm_bigint
  def test_dylink_i64_c(self):
    self.dylink_test(r'''
      #include <stdio.h>
      #include <inttypes.h>
      #include "header.h"

      typedef int32_t (*fp_type_32)(int32_t, int32_t, int32_t);
      typedef int64_t (*fp_type_64)(int32_t, int32_t, int32_t);

      int32_t internal_function_ret_32(int32_t i, int32_t j, int32_t k) {
        return 32;
      }
      int64_t internal_function_ret_64(int32_t i, int32_t j, int32_t k) {
        return 64;
      }

      int main() {
        fp_type_32 fp32_internal = &internal_function_ret_32;
        fp_type_32 fp32_external = &function_ret_32;
        fp_type_64 fp64_external = &function_ret_64;
        fp_type_64 fp64_internal = &internal_function_ret_64;
        int32_t ires32 = fp32_internal(0,0,0);
        printf("res32 - internal %d\n", ires32);
        int32_t eres32 = fp32_external(0,0,0);
        printf("res32 - external %d\n", eres32);

        int64_t ires64 = fp64_internal(0,0,0);
        printf("res64 - internal %" PRId64 "\n", ires64);
        int64_t eres64 = fp64_external(0,0,0);
        printf("res64 - external %" PRId64 "\n", eres64);
        return 0;
      }
    ''', '''\
      #include "header.h"
      int32_t function_ret_32(int32_t i, int32_t j, int32_t k) {
        return 32;
      }
      int64_t function_ret_64(int32_t i, int32_t j, int32_t k) {
        return 64;
      }
    ''', '''\
res32 - internal 32
res32 - external 32
res64 - internal 64
res64 - external 64\n''', header='''\
      #include <emscripten.h>
      #include <stdint.h>
      EMSCRIPTEN_KEEPALIVE int32_t function_ret_32(int32_t i, int32_t j, int32_t k);
      EMSCRIPTEN_KEEPALIVE int64_t function_ret_64(int32_t i, int32_t j, int32_t k);
    ''', force_c=True)

  @needs_dylink
  @also_with_wasm_bigint
  def test_dylink_i64_invoke(self):
    self.set_setting('DISABLE_EXCEPTION_CATCHING', 0)
    self.dylink_test(r'''\
    #include <stdio.h>
    #include <stdint.h>

    extern "C" int64_t sidey(int64_t arg);

    int main(int argc, char *argv[]) {
        int64_t temp = 42;
        printf("got %lld\n", sidey(temp));
        return 0;
    }''', r'''\
    #include <stdint.h>
    #include <stdio.h>
    #include <emscripten.h>

    extern "C" {

    EMSCRIPTEN_KEEPALIVE int64_t do_call(int64_t arg) {
        if (arg == 0) {
            throw;
        }
        return 2 * arg;
    }
    int64_t sidey(int64_t arg) {
        try {
            return do_call(arg);
        } catch(...) {
            return 0;
        }
    }
    }''', 'got 84', need_reverse=False)

  @needs_dylink
  def test_dylink_class(self):
    self.dylink_test(header=r'''
      #include <stdio.h>
      struct Class {
        Class(const char *name);
      };
    ''', main=r'''
      #include "header.h"
      int main() {
        Class c("main");
        return 0;
      }
    ''', side=r'''
      #include "header.h"
      Class::Class(const char *name) { printf("new %s\n", name); }
    ''', expected=['new main\n'])

  @needs_dylink
  def test_dylink_global_var(self):
    self.dylink_test(main=r'''
      #include <stdio.h>
      extern int x;
      int main() {
        printf("extern is %d.\n", x);
        return 0;
      }
    ''', side=r'''
      int x = 123;
    ''', expected=['extern is 123.\n'], force_c=True)

  @needs_dylink
  def test_dylink_global_var_modded(self):
    self.dylink_test(main=r'''
      #include <stdio.h>
      extern int x;
      int main() {
        printf("extern is %d.\n", x);
        return 0;
      }
    ''', side=r'''
      int x = 123;
      struct Initter {
        Initter() { x = 456; }
      };
      Initter initter;
    ''', expected=['extern is 456.\n'])

  @needs_dylink
  def test_dylink_stdlib(self):
    self.dylink_test(header=r'''
      #include <math.h>
      #include <stdlib.h>
      #include <string.h>
      char *side(const char *data);
      double pow_two(double x);
    ''', main=r'''
      #include <stdio.h>
      #include "header.h"
      int main() {
        char *temp = side("hello through side\n");
        char *ret = (char*)malloc(strlen(temp)+1);
        strcpy(ret, temp);
        temp[1] = 'x';
        puts(ret);
        printf("pow_two: %d.\n", (int)pow_two(5.9));
        free(ret);
        free(temp);
        return 0;
      }
    ''', side=r'''
      #include "header.h"
      char *side(const char *data) {
        char *ret = (char*)malloc(strlen(data)+1);
        strcpy(ret, data);
        return ret;
      }
      double pow_two(double x) {
        return pow(2, x);
      }
    ''', expected=['hello through side\n\npow_two: 59.'], force_c=True)

  @needs_dylink
  def test_dylink_jslib(self):
    create_file('lib.js', r'''
      mergeInto(LibraryManager.library, {
        test_lib_func: function(x) {
          return x + 17.2;
        }
      });
    ''')
    self.dylink_test(header=r'''
      extern double test_lib_func(int input);
    ''', main=r'''
      #include <stdio.h>
      #include "header.h"
      extern double sidey();
      int main2() { return 11; }
      int main() {
        int input = sidey();
        double temp = test_lib_func(input);
        printf("other says %.2f\n", temp);
        printf("more: %.5f, %d\n", temp, input);
        return 0;
      }
    ''', side=r'''
      #include <stdio.h>
      #include "header.h"
      extern int main2();
      double sidey() {
        int temp = main2();
        printf("main2 sed: %d\n", temp);
        printf("main2 sed: %u, %c\n", temp, temp/2);
        return test_lib_func(temp);
      }
    ''', expected='other says 45.2', main_emcc_args=['--js-library', 'lib.js'], force_c=True)

  @needs_dylink
  def test_dylink_many_postsets(self):
    NUM = 1234
    self.dylink_test(header=r'''
      #include <stdio.h>
      typedef void (*voidfunc)();
      static void simple() {
        printf("simple.\n");
      }
      static volatile voidfunc funcs[''' + str(NUM) + '] = { ' + ','.join(['simple'] * NUM) + r''' };
      static void test() {
        volatile int i = ''' + str(NUM - 1) + r''';
        funcs[i]();
        i = 0;
        funcs[i]();
      }
      extern void more();
    ''', main=r'''
      #include "header.h"
      int main() {
        test();
        more();
        return 0;
      }
    ''', side=r'''
      #include "header.h"
      void more() {
        test();
      }
    ''', expected=['simple.\nsimple.\nsimple.\nsimple.\n'], force_c=True)

  @needs_dylink
  def test_dylink_postsets_chunking(self):
    self.dylink_test(header=r'''
      extern int global_var;
    ''', main=r'''
      #include <stdio.h>
      #include "header.h"

      // prepare 99 global variable with local initializer
      static int p = 1;
      #define P(x) __attribute__((used)) int *padding##x = &p;
      P(01) P(02) P(03) P(04) P(05) P(06) P(07) P(08) P(09) P(10)
      P(11) P(12) P(13) P(14) P(15) P(16) P(17) P(18) P(19) P(20)
      P(21) P(22) P(23) P(24) P(25) P(26) P(27) P(28) P(29) P(30)
      P(31) P(32) P(33) P(34) P(35) P(36) P(37) P(38) P(39) P(40)
      P(41) P(42) P(43) P(44) P(45) P(46) P(47) P(48) P(49) P(50)
      P(51) P(52) P(53) P(54) P(55) P(56) P(57) P(58) P(59) P(60)
      P(61) P(62) P(63) P(64) P(65) P(66) P(67) P(68) P(69) P(70)
      P(71) P(72) P(73) P(74) P(75) P(76) P(77) P(78) P(79) P(80)
      P(81) P(82) P(83) P(84) P(85) P(86) P(87) P(88) P(89) P(90)
      P(91) P(92) P(93) P(94) P(95) P(96) P(97) P(98) P(99)

      // prepare global variable with global initializer
      int *ptr = &global_var;

      int main(int argc, char *argv[]) {
        printf("%d\n", *ptr);
      }
    ''', side=r'''
      #include "header.h"

      int global_var = 12345;
    ''', expected=['12345\n'], force_c=True)

  @needs_dylink
  @parameterized({
    'libcxx': ('libc,libc++,libmalloc,libc++abi',),
    'all': ('1',),
    'missing': ('libc,libmalloc,libc++abi', False, False, False),
    'missing_assertions': ('libc,libmalloc,libc++abi', False, False, True),
  })
  def test_dylink_syslibs(self, syslibs, expect_pass=True, need_reverse=True, assertions=True):
    # one module uses libcxx, need to force its inclusion when it isn't the main
    self.emcc_args.append('-Wno-deprecated')
    self.set_setting('WARN_ON_UNDEFINED_SYMBOLS', 0)

    if assertions is not None:
      self.set_setting('ASSERTIONS', int(assertions))

    if expect_pass:
      expected = 'cout hello from side'
      assert_returncode = 0
    else:
      if assertions:
        expected = 'build the MAIN_MODULE with EMCC_FORCE_STDLIBS=1 in the environment'
      else:
        expected = 'Error'
      assert_returncode = NON_ZERO

    with env_modify({'EMCC_FORCE_STDLIBS': syslibs, 'EMCC_ONLY_FORCED_STDLIBS': '1'}):
      self.dylink_test(main=r'''
        void side();
        int main() {
          side();
          return 0;
        }
      ''', side=r'''
        #include <iostream>
        void side() { std::cout << "cout hello from side\n"; }
      ''', expected=expected, need_reverse=need_reverse, main_module=1, assert_returncode=assert_returncode)

  @needs_dylink
  @with_env_modify({'EMCC_FORCE_STDLIBS': 'libc++'})
  def test_dylink_iostream(self):
    self.dylink_test(header=r'''
      #include <iostream>
      #include <string>
      std::string side();
    ''', main=r'''
      #include "header.h"
      int main() {
        std::cout << "hello from main " << side() << std::endl;
        return 0;
      }
    ''', side=r'''
      #include "header.h"
      std::string side() { return "and hello from side"; }
    ''', expected=['hello from main and hello from side\n'])

  @needs_dylink
  def test_dylink_dynamic_cast(self): # issue 3465
    self.dylink_test(header=r'''
      class Base {
      public:
          virtual void printName();
      };

      class Derived : public Base {
      public:
          void printName();
      };
    ''', main=r'''
      #include "header.h"
      #include <stdio.h>

      int main() {
        printf("starting main\n");

        Base *base = new Base();
        Base *derived = new Derived();
        base->printName();
        derived->printName();

        if (dynamic_cast<Derived*>(derived)) {
          printf("OK\n");
        } else {
          printf("KO\n");
        }

        delete base;
        delete derived;
        return 0;
      }
    ''', side=r'''
      #include "header.h"
      #include <stdio.h>

      void Base::printName() {
        printf("Base\n");
      }

      void Derived::printName() {
        printf("Derived\n");
      }
    ''', expected=['starting main\nBase\nDerived\nOK'])

  @with_both_eh_sjlj
  @needs_dylink
  def test_dylink_raii_exceptions(self):
    self.dylink_test(main=r'''
      #include <stdio.h>
      extern int side();
      int main() {
        printf("from side: %d.\n", side());
      }
    ''', side=r'''
      #include <stdio.h>
      typedef int (*ifdi)(float, double, int);
      int func_with_special_sig(float a, double b, int c) {
        printf("special %f %f %d\n", a, b, c);
        return 1337;
      }
      struct DestructorCaller {
        ~DestructorCaller() { printf("destroy\n"); }
      };
      int side() {
        // d has a destructor that must be called on function
        // exit, which means an invoke will be used for the
        // indirect call here - and the signature of that call
        // is special and not present in the main module, so
        // it must be generated for the side module.
        DestructorCaller d;
        volatile ifdi p = func_with_special_sig;
        return p(2.18281, 3.14159, 42);
      }
    ''', expected=['special 2.182810 3.141590 42\ndestroy\nfrom side: 1337.\n'])

  @with_both_eh_sjlj
  @needs_dylink
  def test_dylink_exceptions_try_catch(self):
    self.dylink_test(main=r'''
      #include <stdio.h>
      extern void side();
      int main() {
        try {
          throw 3;
        } catch (int n) {
          printf("main: caught %d\n", n);
        }
        side();
        return 0;
      }
    ''', side=r'''
      #include <stdio.h>
      void side() {
        try {
          throw 5.3f;
        } catch (float f) {
          printf("side: caught %.1f\n", f);
        }
      }
      ''', expected=['main: caught 3\nside: caught 5.3\n'])

  @with_both_eh_sjlj
  @needs_dylink
  def test_dylink_exceptions_try_catch_2(self):
    self.dylink_test(main=r'''
      #include <stdio.h>
      extern void side_throw_int();
      int main() {
        try {
          side_throw_int();
        } catch (int n) {
          printf("main: caught %d\n", n);
        }
        return 0;
      }
      void main_throw_float() {
        throw 5.3f;
      }
    ''', side=r'''
      #include <stdio.h>
      extern void main_throw_float();
      void side_throw_int() {
        try {
          main_throw_float();
        } catch (float f) {
          printf("side: caught %.1f\n", f);
        }
        throw 3;
      }
      ''', expected=['side: caught 5.3\nmain: caught 3\n'])

  @with_both_eh_sjlj
  @needs_dylink
  def test_dylink_exceptions_try_catch_6(self):
    create_file('main.cpp', r'''
      #include <dlfcn.h>
      int main() {
        void* handle = dlopen("liblib.so", RTLD_LAZY);
        void (*side)(void) = (void (*)(void))dlsym(handle, "side");
        (side)();
        return 0;
      }
    ''')

    # Create a dependency on __cxa_find_matching_catch_6 (6 = num clauses + 2)
    # which is one higher than the default set of __cxa_find_matching_catch
    # functions created in library_exceptions.js.
    # This means we end up depending on dynamic linking code to redirect
    # __cxa_find_matching_catch_6 to __cxa_find_matching_catch.
    create_file('liblib.cpp', r'''
      #include <stdio.h>
      extern "C" void side() {
        try {
          throw 3;
        } catch (int x){
          printf("side: caught int %d\n", x);
        } catch (float x){
          printf("side: caught float %f\n", x);
        } catch (double x){
          printf("side: caught double %f\n", x);
        } catch (short x){
          printf("side: caught short %hd\n", x);
        }
      }
    ''')

    self.maybe_closure()

    # side settings
    self.clear_setting('MAIN_MODULE')
    self.set_setting('SIDE_MODULE')
    out_file = self.build('liblib.cpp', js_outfile=False)
    shutil.move(out_file, "liblib.so")

    # main settings
    self.set_setting('MAIN_MODULE', 1)
    self.clear_setting('SIDE_MODULE')

    self.do_runf("main.cpp", "side: caught int 3\n")

  @needs_dylink
  @disabled('https://github.com/emscripten-core/emscripten/issues/12815')
  def test_dylink_hyper_dupe(self):
    self.set_setting('INITIAL_MEMORY', '64mb')
    self.set_setting('ASSERTIONS', 2)

    # test hyper-dynamic linking, and test duplicate warnings
    create_file('third.cpp', r'''
      #include <stdio.h>
      int sidef() { return 36; }
      int sideg = 49;
      int bsidef() { return 536; }
      extern void only_in_second_1(int x);
      extern int second_to_third;
      int third_to_second = 1337;

      void only_in_third_0() {
        // note we access our own globals directly, so
        // it doesn't matter that overriding failed
        printf("only_in_third_0: %d, %d, %d\n", sidef(), sideg, second_to_third);
        only_in_second_1(2112);
      }

      void only_in_third_1(int x) {
        printf("only_in_third_1: %d, %d, %d, %d\n", sidef(), sideg, second_to_third, x);
      }
    ''')
    if self.is_wasm():
      libname = 'third.wasm'
    else:
      libname = 'third.js'
    self.run_process([EMCC, 'third.cpp', '-o', libname, '-sSIDE_MODULE'] + self.get_emcc_args())
    self.dylink_test(main=r'''
      #include <stdio.h>
      #include <emscripten.h>
      extern int sidef();
      extern int sideg;
      extern int bsidef();
      extern int bsideg;
      extern void only_in_second_0();
      extern void only_in_third_0();
      int main() {
        EM_ASM({
          loadDynamicLibrary('%s'); // hyper-dynamic! works at least for functions (and consts not used in same block)
        });
        printf("sidef: %%d, sideg: %%d.\n", sidef(), sideg);
        printf("bsidef: %%d.\n", bsidef());
        only_in_second_0();
        only_in_third_0();
      }
    ''' % libname,
                     side=r'''
      #include <stdio.h>
      int sidef() { return 10; } // third will try to override these, but fail!
      int sideg = 20;
      extern void only_in_third_1(int x);
      int second_to_third = 500;
      extern int third_to_second;

      void only_in_second_0() {
        printf("only_in_second_0: %d, %d, %d\n", sidef(), sideg, third_to_second);
        only_in_third_1(1221);
      }

      void only_in_second_1(int x) {
        printf("only_in_second_1: %d, %d, %d, %d\n", sidef(), sideg, third_to_second, x);
      }
    ''',
                     expected=['sidef: 10, sideg: 20.\nbsidef: 536.\nonly_in_second_0: 10, 20, 1337\nonly_in_third_1: 36, 49, 500, 1221\nonly_in_third_0: 36, 49, 500\nonly_in_second_1: 10, 20, 1337, 2112\n'],
                     # in wasm, we can't flip as the side would have an EM_ASM, which we don't support yet TODO
                     need_reverse=not self.is_wasm())

    print('check warnings')
    full = self.run_js('src.js')
    self.assertContained("warning: symbol '_sideg' from '%s' already exists" % libname, full)

  @needs_dylink
  @requires_node
  def test_dylink_load_compiled_side_module(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.emcc_args.append('-lnodefs.js')
    self.set_setting('INITIAL_MEMORY', '64mb')
    # This test loads the module at runtime with loadWebAssemblyModule so we
    # want to suppress the automatic loading that would otherwise be done at
    # startup.
    self.set_setting('NO_AUTOLOAD_DYLIBS')

    self.dylink_test(main=r'''
      #include <stdio.h>
      #include <emscripten.h>
      extern int sidef();
      int main() {
        EM_ASM({
          FS.mkdir('/working');
          FS.mount(NODEFS, { root: '.' }, '/working');
          var libData = FS.readFile('/working/liblib.so', {encoding: 'binary'});
          if (!(libData instanceof Uint8Array)) {
            libData = new Uint8Array(libData);
          }
          var compiledModule = new WebAssembly.Module(libData);
          var sideExports = loadWebAssemblyModule(compiledModule, {loadAsync: false, nodelete: true});
          mergeLibSymbols(sideExports, 'liblib.so');
        });
        printf("sidef: %d.\n", sidef());
      }
    ''',
                     side=r'''
      #include <stdio.h>
      int sidef() { return 10; }
    ''',
                     expected=['sidef: 10'],
                     # in wasm, we can't flip as the side would have an EM_ASM, which we don't support yet TODO
                     need_reverse=not self.is_wasm())

  @needs_dylink
  def test_dylink_dso_needed(self):
    def do_run(src, expected_output, emcc_args=None):
      create_file('main.c', src + 'int main() { return test_main(); }')
      self.do_runf('main.c', expected_output, emcc_args=emcc_args)
    self._test_dylink_dso_needed(do_run)

  @needs_dylink
  def test_dylink_dot_a(self):
    # .a linking must force all .o files inside it, when in a shared module
    create_file('third.c', 'int sidef() { return 36; }')
    create_file('fourth.c', 'int sideg() { return 17; }')

    self.run_process([EMCC, '-fPIC', '-c', 'third.c', '-o', 'third.o'] + self.get_emcc_args(ldflags=False))
    self.run_process([EMCC, '-fPIC', '-c', 'fourth.c', '-o', 'fourth.o'] + self.get_emcc_args(ldflags=False))
    self.run_process([EMAR, 'rc', 'libfourth.a', 'fourth.o'])

    self.dylink_test(main=r'''
      #include <stdio.h>
      #include <emscripten.h>
      int sidef();
      int sideg();
      int main() {
        printf("sidef: %d, sideg: %d.\n", sidef(), sideg());
      }
    ''',
                     # contents of libfourth.a must be included, even if they aren't referred to!
                     side=['libfourth.a', 'third.o'],
                     expected=['sidef: 36, sideg: 17.\n'], force_c=True)

  @needs_dylink
  def test_dylink_spaghetti(self):
    self.dylink_test(main=r'''
      #include <stdio.h>
      int main_x = 72;
      extern int side_x;
      int adjust = side_x + 10;
      int *ptr = &side_x;
      struct Class {
        Class() {
          printf("main init sees %d, %d, %d.\n", adjust, *ptr, main_x);
        }
      };
      Class cm;
      int main() {
        printf("main main sees %d, %d, %d.\n", adjust, *ptr, main_x);
        return 0;
      }
    ''', side=r'''
      #include <stdio.h>
      extern int main_x;
      int side_x = -534;
      int adjust2 = main_x + 10;
      int *ptr2 = &main_x;
      struct SideClass {
        SideClass() {
          printf("side init sees %d, %d, %d.\n", adjust2, *ptr2, side_x);
        }
      };
      SideClass cs;
    ''', expected=['''\
side init sees 82, 72, -534.
main init sees -524, -534, 72.
main main sees -524, -534, 72.
''', '''\
main init sees -524, -534, 72.
side init sees 82, 72, -534.
main main sees -524, -534, 72.
'''])

  @needs_make('mingw32-make')
  @needs_dylink
  def test_dylink_zlib(self):
    self.set_setting('RELOCATABLE')
    zlib_archive = self.get_zlib_library(cmake=WINDOWS)
    # example.c uses K&R style function declarations
    self.emcc_args.append('-Wno-deprecated-non-prototype')
    self.emcc_args.append('-I' + test_file('third_party/zlib'))
    self.dylink_test(main=read_file(test_file('third_party/zlib/example.c')),
                     side=zlib_archive,
                     expected=read_file(test_file('core/test_zlib.out')),
                     force_c=True)

  # @needs_dylink
  # def test_dylink_bullet(self):
  #   self.emcc_args += ['-I' + test_file('bullet/src')]
  #   side = self.get_bullet_library(self, True)
  #   self.dylink_test(main=read_file(test_file('bullet/Demos/HelloWorld/HelloWorld.cpp')),
  #                    side=side,
  #                    expected=[read_file(test_file('bullet/output.txt')), # different roundings
  #                              read_file(test_file('bullet/output2.txt')),
  #                              read_file(test_file('bullet/output3.txt'))])

  @needs_dylink
  def test_dylink_rtti(self):
    # Verify that objects created in one module and be dynamic_cast<> correctly
    # in the another module.
    # Each module will define its own copy of certain COMDAT symbols such as
    # each classs's typeinfo, but at runtime they should both use the same one.
    header = '''
    #include <cstddef>

    class Foo {
    public:
      virtual ~Foo() {}
    };

    class Bar : public Foo {
    public:
      virtual ~Bar() {}
    };

    bool is_bar(Foo* foo);
    '''

    main = '''
    #include <stdio.h>
    #include "header.h"

    int main() {
      Bar bar;
      if (!is_bar(&bar)) {
        puts("failure");
        return 1;
      }
      puts("success");
      return 0;
    }
    '''

    side = '''
    #include "header.h"

    bool is_bar(Foo* foo) {
      return dynamic_cast<Bar*>(foo) != nullptr;
    }
    '''

    self.dylink_test(main=main,
                     side=side,
                     header=header,
                     expected='success')

  @needs_dylink
  def test_dylink_argv_argc(self):
    # Verify that argc and argv can be sent to main when main is in a side module

    self.emcc_args += ['--extern-pre-js', 'pre.js']

    create_file('pre.js', '''
      var Module = { arguments: ['hello', 'world!'] }
    ''')

    self.dylink_test(
      '', # main module is empty.
      r'''
      #include <stdio.h>
      int main(int argc, char const *argv[]) {
        printf("%d ", argc);
        for (int i=1; i<argc; i++) printf("%s ", argv[i]);
        printf("\n");
        return 0;
      }
      ''',
      expected='3 hello world!',
      need_reverse=False)

  @needs_dylink
  def test_dylink_weak(self):
    # Verify that weakly defined symbols can be defined in both side module and main
    # module but that only one gets used at runtime.
    self.dylink_testf(test_file('core/test_dylink_weak.c'), need_reverse=False)

  @node_pthreads
  @needs_dylink
  def test_dylink_tls(self):
    self.emcc_args.append('-Wno-experimental')
    self.dylink_testf(test_file('core/test_dylink_tls.c'),
                      need_reverse=False)

  @node_pthreads
  @needs_dylink
  def test_dylink_tls_export(self):
    self.emcc_args.append('-Wno-experimental')
    self.dylink_testf(test_file('core/test_dylink_tls_export.c'),
                      need_reverse=False)

  def test_random(self):
    src = r'''#include <stdlib.h>
#include <stdio.h>

int main()
{
    srandom(0xdeadbeef);
    printf("%ld\n", random());
}
'''
    self.do_run(src, '956867869')

  def test_rand(self):
    src = r'''#include <stdlib.h>
#include <stdio.h>
#include <assert.h>
int main()
{
    // we need RAND_MAX to be a bitmask (power of 2 minus 1). this assertions guarantees
    // if RAND_MAX changes the test failure will focus attention on that issue here.
    assert(RAND_MAX == 0x7fffffff);

    srand(0xdeadbeef);
    for(int i = 0; i < 10; ++i)
        printf("%d\n", rand());

    unsigned int seed = 0xdeadbeef;
    for(int i = 0; i < 10; ++i)
        printf("%d\n", rand_r(&seed));

    bool haveEvenAndOdd = true;
    for(int i = 1; i <= 30; ++i)
    {
        int mask = 1 << i;
        if (mask > RAND_MAX) break;
        bool haveEven = false;
        bool haveOdd = false;
        for(int j = 0; j < 1000 && (!haveEven || !haveOdd); ++j)
        {
            if ((rand() & mask) == 0)
                haveEven = true;
            else
                haveOdd = true;
        }
        haveEvenAndOdd = haveEvenAndOdd && haveEven && haveOdd;
    }
    if (haveEvenAndOdd)
        printf("Have even and odd!\n");

    return 0;
}
'''
    expected = '''490242850
2074599277
1480056542
1912638067
931112055
2110392489
2053422194
1614832492
216117595
174823244
760368382
602359081
1121118963
1291018924
1608306807
352705809
958258461
1182561381
114276303
1481323674
Have even and odd!
'''
    self.do_run(src, expected)

  def test_strtod(self):
    self.do_core_test('test_strtod.c')

  def test_strtold(self):
    self.do_core_test('test_strtold.c')

  def test_strtok(self):
    self.do_core_test('test_strtok.c')

  def test_strtol(self):
    if self.get_setting('MEMORY64'):
      out_suffix = '64'
    else:
      out_suffix = ''
    self.do_core_test('test_strtol.c', out_suffix=out_suffix)

  def test_transtrcase(self):
    self.do_core_test('test_transtrcase.c')

  @no_wasm2js('very slow to compile')
  @no_wasm64('produces different output due to %z printing')
  @is_slow_test
  def test_printf(self):
    # needs to flush stdio streams
    self.emcc_args.append('-Wno-format')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('STACK_SIZE', '1MB')
    self.do_run_in_out_file_test('printf/test.c')

  def test_printf_2(self):
    self.do_core_test('test_printf_2.c')

  def test_printf_float(self):
    self.do_run_in_out_file_test('printf/test_float.c')

  def test_printf_octal(self):
    self.do_run_in_out_file_test('printf/test_octal.c')

  def test_printf_macros(self):
    self.do_core_test('test_printf_macros.c')

  def test_vprintf(self):
    self.do_core_test('test_vprintf.c')

  def test_vsnprintf(self):
    self.do_core_test('test_vsnprintf.c')

  def test_printf_more(self):
    self.do_core_test('test_printf_more.c')

  def test_perrar(self):
    self.do_core_test('test_perrar.c')

  def test_atoX(self):
    self.do_core_test('test_atoX.c')

  def test_strstr(self):
    self.do_core_test('test_strstr.c')

  def test_fnmatch(self):
    self.do_core_test('test_fnmatch.cpp')

  def test_sscanf(self):
    self.do_core_test('test_sscanf.c')

  def test_sscanf_2(self):
    # doubles
    for ftype in ['float', 'double']:
      src = r'''
          #include <stdio.h>

          int main(){
              char strval1[] = "1.2345678901";
              char strval2[] = "1.23456789e5";
              char strval3[] = "1.23456789E5";
              char strval4[] = "1.2345678e-5";
              char strval5[] = "1.2345678E-5";
              double dblval = 1.2345678901;
              double tstval;

              sscanf(strval1, "%lf", &tstval);
              if(dblval != tstval) printf("FAIL: Values are not equal: %lf %lf\n", dblval, tstval);
              else printf("Pass: %lf %lf\n", tstval, dblval);

              sscanf(strval2, "%lf", &tstval);
              dblval = 123456.789;
              if(dblval != tstval) printf("FAIL: Values are not equal: %lf %lf\n", dblval, tstval);
              else printf("Pass: %lf %lf\n", tstval, dblval);

              sscanf(strval3, "%lf", &tstval);
              dblval = 123456.789;
              if(dblval != tstval) printf("FAIL: Values are not equal: %lf %lf\n", dblval, tstval);
              else printf("Pass: %lf %lf\n", tstval, dblval);

              sscanf(strval4, "%lf", &tstval);
              dblval = 0.000012345678;
              if(dblval != tstval) printf("FAIL: Values are not equal: %lf %lf\n", dblval, tstval);
              else printf("Pass: %lf %lf\n", tstval, dblval);

              sscanf(strval5, "%lf", &tstval);
              dblval = 0.000012345678;
              if(dblval != tstval) printf("FAIL: Values are not equal: %lf %lf\n", dblval, tstval);
              else printf("Pass: %lf %lf\n", tstval, dblval);

              return 0;
          }
        '''
      if ftype == 'float':
        self.do_run(src.replace('%lf', '%f').replace('double', 'float'), '''Pass: 1.234568 1.234568
Pass: 123456.789062 123456.789062
Pass: 123456.789062 123456.789062
Pass: 0.000012 0.000012
Pass: 0.000012 0.000012''')
      else:
        self.do_run(src, '''Pass: 1.234568 1.234568
Pass: 123456.789000 123456.789000
Pass: 123456.789000 123456.789000
Pass: 0.000012 0.000012
Pass: 0.000012 0.000012''')

  def test_sscanf_n(self):
    self.do_core_test('test_sscanf_n.c')

  def test_sscanf_whitespace(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_sscanf_whitespace.c')

  def test_sscanf_other_whitespace(self):
    # use i16s in printf
    self.set_setting('SAFE_HEAP', 0)
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_sscanf_other_whitespace.c')

  def test_sscanf_3(self):
    self.do_core_test('test_sscanf_3.c')

  def test_sscanf_4(self):
    self.do_core_test('test_sscanf_4.c')

  def test_sscanf_5(self):
    self.do_core_test('test_sscanf_5.c')

  def test_sscanf_6(self):
    self.do_core_test('test_sscanf_6.c')

  def test_sscanf_skip(self):
    self.do_core_test('test_sscanf_skip.c')

  def test_sscanf_caps(self):
    self.do_core_test('test_sscanf_caps.c')

  def test_sscanf_hex(self):
    self.do_core_test('test_sscanf_hex.cpp')

  def test_sscanf_float(self):
    self.do_core_test('test_sscanf_float.c')

  def test_langinfo(self):
    self.do_core_test('test_langinfo.c')

  def test_files(self):
    # Use closure here, to test we don't break FS stuff
    if '-O3' in self.emcc_args and not self.is_wasm():
      print('closure 2')
      self.emcc_args += ['--closure', '2'] # Use closure 2 here for some additional coverage
      # Sadly --closure=2 is not yet free of closure warnings
      # FIXME(https://github.com/emscripten-core/emscripten/issues/17080)
      self.ldflags.append('-Wno-error=closure')
    else:
      self.maybe_closure()

    self.emcc_args += ['--pre-js', 'pre.js']
    self.set_setting('FORCE_FILESYSTEM')

    create_file('pre.js', '''
/** @suppress{checkTypes}*/
Module = {
  'noFSInit': true,
  'preRun': () => {
    FS.createLazyFile('/', 'test.file', 'test.file', true, false);
    // Test FS_* exporting
    Module['FS_createDataFile']('/', 'somefile.binary', [100, 200, 50, 25, 10, 77, 123], true, false, false);  // 200 becomes -56, since signed chars are used in memory
    var test_files_input = 'hi there!';
    var test_files_input_index = 0;
    FS.init(() => {
      return test_files_input.charCodeAt(test_files_input_index++) || null;
    });
  }
};
''')

    create_file('test.file', 'some data')

    def clean(out):
      return '\n'.join([line for line in out.split('\n') if 'binaryen' not in line and 'wasm' not in line and 'so not running' not in line])

    self.do_runf(test_file('files.cpp'), ('size: 7\ndata: 100,-56,50,25,10,77,123\nloop: 100 -56 50 25 10 77 123 \ninput:hi there!\ntexto\n$\n5 : 10,30,20,11,88\nother=some data.\nseeked=me da.\nseeked=ata.\nseeked=ta.\nfscanfed: 10 - hello\n5 bytes to dev/null: 5\nok.\ntexte\n', 'size: 7\ndata: 100,-56,50,25,10,77,123\nloop: 100 -56 50 25 10 77 123 \ninput:hi there!\ntexto\ntexte\n$\n5 : 10,30,20,11,88\nother=some data.\nseeked=me da.\nseeked=ata.\nseeked=ta.\nfscanfed: 10 - hello\n5 bytes to dev/null: 5\nok.\n'),
                 output_nicerizer=clean)

    if self.uses_memory_init_file():
      self.assertExists('files.js.mem')

  def test_files_m(self):
    # Test for Module.stdin etc.
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')

    create_file('pre.js', '''
    Module = {
      data: [10, 20, 40, 30],
      stdin: () => { return Module.data.pop() || null },
      stdout: (x) => out('got: ' + x)
    };
    ''')
    self.emcc_args += ['--pre-js', 'pre.js']

    src = r'''
      #include <stdio.h>
      #include <unistd.h>

      int main () {
        char c;
        fprintf(stderr, "isatty? %d,%d,%d\n", isatty(fileno(stdin)), isatty(fileno(stdout)), isatty(fileno(stderr)));
        while ((c = fgetc(stdin)) != EOF) {
          putc(c+5, stdout);
        }
        return 0;
      }
      '''

    def clean(out):
      return '\n'.join(l for l in out.splitlines() if 'warning' not in l and 'binaryen' not in l)

    self.do_run(src, ('got: 35\ngot: 45\ngot: 25\ngot: 15\nisatty? 0,0,1\n', 'got: 35\ngot: 45\ngot: 25\ngot: 15\nisatty? 0,0,1', 'isatty? 0,0,1\ngot: 35\ngot: 45\ngot: 25\ngot: 15'), output_nicerizer=clean)

  def test_mount(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.do_runf(test_file('fs/test_mount.c'), 'success')

  def test_getdents64(self):
    self.do_runf(test_file('fs/test_getdents64.cpp'), '..')

  def test_getdents64_special_cases(self):
    self.do_run_in_out_file_test('fs/test_getdents64_special_cases.cpp')

  def test_getcwd_with_non_ascii_name(self):
    self.do_run_in_out_file_test('fs/test_getcwd_with_non_ascii_name.cpp')

  @no_wasmfs('no support for /proc/self/fd/, see https://github.com/emscripten-core/emscripten/issues/19430')
  def test_proc_self_fd(self):
    self.do_run_in_out_file_test('fs/test_proc_self_fd.c')

  def test_fwrite_0(self):
    self.do_core_test('test_fwrite_0.c')

  @parameterized({
    '': (['MEMFS']),
    'nodefs': (['NODEFS'])
  })
  def test_fgetc_ungetc(self, fs):
    print('TODO: update this test once the musl ungetc-on-EOF-stream bug is fixed upstream and reaches us')
    self.emcc_args += ['-D' + fs]
    if fs == 'NODEFS':
      self.require_node()
      self.emcc_args += ['-lnodefs.js']
    self.do_runf(test_file('stdio/test_fgetc_ungetc.c'), 'success')

  def test_fgetc_unsigned(self):
    src = r'''
      #include <stdio.h>
      int main() {
        FILE *file = fopen("file_with_byte_234.txt", "rb");
        int c = fgetc(file);
        printf("*%d\n", c);
      }
    '''
    create_file('file_with_byte_234.txt', b'\xea', binary=True)
    self.emcc_args += ['--embed-file', 'file_with_byte_234.txt']
    self.do_run(src, '*234\n')

  def test_fgets_eol(self):
    src = r'''
      #include <stdio.h>
      char buf[32];
      int main() {
        const char *r = "SUCCESS";
        FILE *f = fopen("eol.txt", "r");
        while (fgets(buf, 32, f) != NULL) {
          if (buf[0] == '\0') {
            r = "FAIL";
            break;
          }
        }
        printf("%s\n", r);
        fclose(f);
        return 0;
      }
    '''
    create_file('eol.txt', b'\n', binary=True)
    self.emcc_args += ['--embed-file', 'eol.txt']
    self.do_run(src, 'SUCCESS\n')

  def test_fscanf(self):
    create_file('three_numbers.txt', '-1 0.1 -.1')
    src = r'''
      #include <stdio.h>
      #include <assert.h>
      #include <float.h>
      int main() {
        float x = FLT_MAX, y = FLT_MAX, z = FLT_MAX;

        FILE* fp = fopen("three_numbers.txt", "r");
        if (fp) {
          int match = fscanf(fp, " %f %f %f ", &x, &y, &z);
          printf("match = %d\n", match);
          printf("x = %0.1f, y = %0.1f, z = %0.1f\n", x, y, z);
        } else {
          printf("failed to open three_numbers.txt\n");
        }
        return 0;
      }
    '''
    self.emcc_args += ['--embed-file', 'three_numbers.txt']
    self.do_run(src, 'match = 3\nx = -1.0, y = 0.1, z = -0.1\n')

  def test_fscanf_2(self):
    create_file('a.txt', '1/2/3 4/5/6 7/8/9\n')
    self.emcc_args += ['--embed-file', 'a.txt']
    self.do_run(r'''\
      #include <stdio.h>

      int main(int argv, char** argc) {
        printf("fscanf test\n");

        FILE* file = fopen("a.txt", "rb");
        int vertexIndex[4];
        int normalIndex[4];
        int uvIndex[4];

        int matches = fscanf(file, "%d/%d/%d %d/%d/%d %d/%d/%d %d/%d/%d\n",
                             &vertexIndex[0], &uvIndex[0], &normalIndex[0],
                             &vertexIndex[1], &uvIndex[1], &normalIndex[1],
                             &vertexIndex[2], &uvIndex[2], &normalIndex[2],
                             &vertexIndex[3], &uvIndex[3], &normalIndex[3]);
        fclose(file);

        printf("matches: %d\n", matches);
        return 0;
      }
    ''', 'fscanf test\nmatches: 9\n')

  def test_fileno(self):
    create_file('empty.txt', '')
    src = r'''
      #include <stdio.h>
      #include <unistd.h>
      int main() {
        FILE* fp = fopen("empty.txt", "r");
        if (fp) {
            printf("%d\n", fileno(fp));
        } else {
            printf("failed to open empty.txt\n");
        }
        return 0;
      }
    '''
    self.emcc_args += ['--embed-file', 'empty.txt']
    self.do_run(src, '3\n')

  @also_with_noderawfs
  def test_readdir(self):
    if self.get_setting('WASMFS') and self.get_setting('NODERAWFS'):
      # WasmFS + NODERAWFS lacks ino numbers in directory listings, see
      # https://github.com/emscripten-core/emscripten/issues/19418
      # We need to tell the test we are in this mode so it can ignore them.
      self.emcc_args += ['-DWASMFS_NODERAWFS']
    self.do_run_in_out_file_test('dirent/test_readdir.c')

  @also_with_wasm_bigint
  def test_readdir_empty(self):
    self.do_run_in_out_file_test('dirent/test_readdir_empty.c')

  def test_readdir_unlink(self):
    self.do_run_in_out_file_test('dirent/test_readdir_unlink.c')

  def test_stat(self):
    self.set_setting("FORCE_FILESYSTEM")
    self.do_runf(test_file('stat/test_stat.c'), 'success')
    self.verify_in_strict_mode('test_stat.js')

  def test_fstatat(self):
    self.do_runf(test_file('stat/test_fstatat.c'), 'success')

  @also_with_wasmfs
  def test_stat_chmod(self):
    self.do_runf(test_file('stat/test_chmod.c'), 'success')

  @also_with_wasmfs
  def test_stat_mknod(self):
    self.do_runf(test_file('stat/test_mknod.c'), 'success')

  @also_with_wasmfs
  def test_fcntl(self):
    if self.get_setting('WASMFS'):
      self.emcc_args += ['-sFORCE_FILESYSTEM']
    self.add_pre_run("FS.createDataFile('/', 'test', 'abcdef', true, true, false);")
    self.do_run_in_out_file_test('fcntl/test_fcntl.c')

  def test_fcntl_open(self):
    self.do_run_in_out_file_test('fcntl/test_fcntl_open.c')

  @also_with_wasm_bigint
  def test_fcntl_misc(self):
    if self.get_setting('WASMFS'):
      self.emcc_args += ['-sFORCE_FILESYSTEM']
    self.add_pre_run("FS.createDataFile('/', 'test', 'abcdef', true, true, false);")
    self.do_run_in_out_file_test('fcntl/test_fcntl_misc.c')

  def test_poll(self):
    self.add_pre_run('''
      var dummy_device = FS.makedev(64, 0);
      FS.registerDevice(dummy_device, {});

      FS.createDataFile('/', 'file', 'abcdef', true, true, false);
      FS.mkdev('/device', dummy_device);
    ''')
    self.do_core_test('test_poll.c')

  def test_statvfs(self):
    self.do_core_test('test_statvfs.c')

  def test_libgen(self):
    self.do_core_test('test_libgen.c')

  def test_utime(self):
    self.do_runf(test_file('utime/test_utime.c'), 'success')

  def test_futimens(self):
    self.do_runf(test_file('utime', 'test_futimens.c'), 'success')

  @no_minimal_runtime('MINIMAL_RUNTIME does not have getValue() and setValue() (TODO add it to a JS library function to get it in)')
  @requires_node  # only node handles utf well
  def test_utf(self):
    self.set_setting('EXPORTED_FUNCTIONS', ['_main', '_malloc', '_free'])
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['getValue', 'setValue', 'UTF8ToString', 'stringToUTF8'])
    self.do_core_test('test_utf.c')

  def test_utf32(self):
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['UTF32ToString', 'stringToUTF32', 'lengthBytesUTF32'])
    self.do_runf(test_file('utf32.cpp'), 'OK.')
    self.do_runf(test_file('utf32.cpp'), 'OK.', args=['-fshort-wchar'])

  @crossplatform
  def test_utf16(self):
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['UTF16ToString', 'stringToUTF16'])
    self.do_runf(test_file('core/test_utf16.cpp'), 'OK.')

  def test_utf8(self):
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['UTF8ToString', 'stringToUTF8', 'AsciiToString', 'stringToAscii'])
    self.do_runf(test_file('utf8.cpp'), 'OK.')

  @also_with_wasm_bigint
  def test_utf8_textdecoder(self):
    self.emcc_args += ['--embed-file', test_file('utf8_corpus.txt') + '@/utf8_corpus.txt']
    self.do_runf(test_file('benchmark/benchmark_utf8.c'), 'OK.')

  # Test that invalid character in UTF8 does not cause decoding to crash.
  def test_utf8_invalid(self):
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['UTF8ToString', 'stringToUTF8'])
    for decoder_mode in [[], ['-sTEXTDECODER']]:
      self.emcc_args += decoder_mode
      print(str(decoder_mode))
      self.do_runf(test_file('utf8_invalid.cpp'), 'OK.')

  # Test that invalid character in UTF8 does not cause decoding to crash.
  @no_asan('TODO: ASan support in minimal runtime')
  def test_minimal_runtime_utf8_invalid(self):
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['UTF8ToString', 'stringToUTF8'])
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    for decoder_mode in [0, 1]:
      self.set_setting('TEXTDECODER', decoder_mode)
      print(str(decoder_mode))
      self.do_runf(test_file('utf8_invalid.cpp'), 'OK.')

  def test_utf16_textdecoder(self):
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['UTF16ToString', 'stringToUTF16', 'lengthBytesUTF16'])
    self.emcc_args += ['--embed-file', test_file('utf16_corpus.txt') + '@/utf16_corpus.txt']
    self.do_runf(test_file('benchmark/benchmark_utf16.cpp'), 'OK.')

  def test_wprintf(self):
    self.do_core_test('test_wprintf.cpp')

  def test_write_stdout_fileno(self):
    self.do_core_test('test_write_stdout_fileno.c')
    self.do_core_test('test_write_stdout_fileno.c', args=['-sFILESYSTEM=0'])

  @also_with_wasmfs # tests EXIT_RUNTIME flushing
  def test_direct_string_constant_usage(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_direct_string_constant_usage.cpp')

  def test_std_cout_new(self):
    self.do_core_test('test_std_cout_new.cpp')

  def test_std_function_incomplete_return(self):
    self.do_core_test('test_std_function_incomplete_return.cpp')

  def test_istream(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')

    for linkable in [0]: # , 1]:
      print(linkable)
      # regression check for issue #273
      self.set_setting('LINKABLE', linkable)
      self.do_core_test('test_istream.cpp')

  def test_fs_base(self):
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$FS'])
    self.uses_es6 = True
    self.add_pre_run(read_file(test_file('filesystem/src.js')))
    src = 'int main() {return 0;}\n'
    expected = read_file(test_file('filesystem/output.txt'))
    self.do_run(src, expected)

  @also_with_noderawfs
  @is_slow_test
  @requires_node
  def test_fs_nodefs_rw(self):
    # TODO(sbc): This test exposes in issue in the way we run closure compiler and
    # causes it to generate non-ES5 output.
    # Remove this line once we fix: https://github.com/emscripten-core/emscripten/issues/12628
    self.uses_es6 = True
    self.emcc_args += ['-lnodefs.js']
    self.set_setting('SYSCALL_DEBUG')
    self.do_runf(test_file('fs/test_nodefs_rw.c'), 'success')
    if self.maybe_closure():
      self.do_runf(test_file('fs/test_nodefs_rw.c'), 'success')

  @also_with_noderawfs
  @requires_node
  def test_fs_nodefs_cloexec(self):
    self.emcc_args += ['-lnodefs.js']
    self.do_runf(test_file('fs/test_nodefs_cloexec.c'), 'success')

  @requires_node
  def test_fs_nodefs_home(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.emcc_args += ['-lnodefs.js']
    self.do_runf(test_file('fs/test_nodefs_home.c'), 'success')

  @requires_node
  def test_fs_nodefs_nofollow(self):
    self.emcc_args += ['-lnodefs.js']
    self.do_runf(test_file('fs/test_nodefs_nofollow.c'), 'success')

  @requires_node
  def test_fs_nodefs_readdir(self):
    # externally setup an existing folder structure: existing/a
    os.makedirs(os.path.join(self.working_dir, 'existing', 'a'))
    self.emcc_args += ['-lnodefs.js']
    self.do_runf(test_file('fs/test_nodefs_readdir.c'), 'success')

  @no_windows('no symlink support on windows')
  @requires_node
  def test_fs_noderawfs_nofollow(self):
    self.set_setting('NODERAWFS')
    create_file('filename', 'foo')
    os.symlink('filename', 'linkname')
    self.emcc_args += ['-lnodefs.js']
    self.do_runf(test_file('fs/test_noderawfs_nofollow.c'), 'success')

  def test_fs_trackingdelegate(self):
    self.set_setting('FS_DEBUG')
    self.do_run_in_out_file_test('fs/test_trackingdelegate.c')

  @also_with_noderawfs
  @also_with_wasmfs_js
  def test_fs_writeFile(self):
    self.do_run_in_out_file_test('fs/test_writeFile.cpp')

  def test_fs_js_api(self):
    self.set_setting("FORCE_FILESYSTEM")
    self.do_runf(test_file('fs/test_fs_js_api.c'), 'success')

  def test_fs_write(self):
    if self.get_setting('WASMFS'):
      self.set_setting("FORCE_FILESYSTEM")
    self.do_run_in_out_file_test('fs/test_write.cpp')

  @also_with_noderawfs
  def test_fs_emptyPath(self):
    self.do_run_in_out_file_test('fs/test_emptyPath.c')

  @also_with_noderawfs
  def test_fs_append(self):
    self.do_runf(test_file('fs/test_append.c'), 'success')

  @parameterized({
    'memfs': ['MEMFS'],
    'nodefs': ['NODEFS'],
    'noderaswfs': ['NODERAWFS'],
    'wasmfs': ['WASMFS']
  })
  def test_fs_mmap(self, fs):
    self.uses_es6 = True
    if fs == 'NODEFS':
      self.require_node()
      self.emcc_args += ['-lnodefs.js']
    if fs == 'NODERAWFS':
      self.require_node()
      self.emcc_args += ['-lnodefs.js', '-lnoderawfs.js']
    if fs == 'WASMFS':
      self.emcc_args += ['-sWASMFS', '-sFORCE_FILESYSTEM']
    self.do_run_in_out_file_test('fs/test_mmap.c', emcc_args=['-D' + fs])

  @no_wasmfs('wasmfs will (?) need a non-JS mechanism to ignore permissions during startup')
  @parameterized({
    '': [],
    'minimal_runtime': ['-sMINIMAL_RUNTIME=1']
  })
  def test_fs_no_main(self, *args):
    # library_fs.js uses hooks to enable ignoring of permisions up until ATMAINs are run.  This
    # test verified that they work correctly, even in programs without a main function.
    create_file('pre.js', '''
Module.preRun = () => {
  assert(FS.ignorePermissions, "ignorePermissions not set during preRun");
}
Module.onRuntimeInitialized = () => {
  assert(!FS.ignorePermissions, "ignorePermissions not unset during onRuntimeInitialized");
  assert(_foo() == 42);
}
''')
    self.set_setting('EXPORTED_FUNCTIONS', '_foo')
    self.set_setting('FORCE_FILESYSTEM')
    self.emcc_args += ['--pre-js', 'pre.js'] + list(args)
    self.do_run('int foo() { return 42; }', '', force_c=True)

  @also_with_noderawfs
  def test_fs_errorstack(self):
    # Enables strict mode, which may catch some strict-mode-only errors
    # so that users can safely work with strict JavaScript if enabled.
    create_file('pre.js', '"use strict";')
    self.emcc_args += ['--pre-js', 'pre.js']

    self.set_setting('FORCE_FILESYSTEM')
    self.set_setting('ASSERTIONS')
    self.do_run(r'''
      #include <emscripten.h>
      #include <stdio.h>
      int main(void) {
        printf("hello world\n"); // should work with strict mode
        EM_ASM(
          try {
            FS.readFile('/dummy.txt');
          } catch (err) {
            err.stack = err.stack; // should be writable
            throw err;
          }
        );
        return 0;
      }
    ''', 'at Object.readFile', assert_returncode=NON_ZERO) # engines has different error stack format

  @also_with_noderawfs
  def test_fs_llseek(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.do_runf(test_file('fs/test_llseek.c'), 'success')

  @also_with_noderawfs
  def test_fs_readv(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.do_runf(test_file('fs/test_readv.c'), 'success')

  @also_with_noderawfs
  def test_fs_writev(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.do_runf(test_file('fs/test_writev.c'), 'success')

  def test_fs_64bit(self):
    self.do_runf(test_file('fs/test_64bit.c'), 'success')

  def test_sigalrm(self):
    self.do_runf(test_file('test_sigalrm.c'), 'Received alarm!')
    self.set_setting('EXIT_RUNTIME')
    self.do_runf(test_file('test_sigalrm.c'), 'Received alarm!')

  def test_signals(self):
    self.do_core_test(test_file('test_signals.c'))

  @no_windows('https://github.com/emscripten-core/emscripten/issues/8882')
  @requires_node
  def test_unistd_access(self):
    self.uses_es6 = True
    orig_compiler_opts = self.emcc_args.copy()
    for fs in ['MEMFS', 'NODEFS']:
      self.emcc_args = orig_compiler_opts + ['-D' + fs]
      if self.get_setting('WASMFS'):
        if fs == 'NODEFS':
          # TODO: NODEFS in WasmFS
          continue
        self.emcc_args += ['-sFORCE_FILESYSTEM']
      if fs == 'NODEFS':
        self.emcc_args += ['-lnodefs.js']
      self.do_run_in_out_file_test('unistd/access.c')
    # Node.js fs.chmod is nearly no-op on Windows
    # TODO: NODERAWFS in WasmFS
    if not WINDOWS and not self.get_setting('WASMFS'):
      self.emcc_args = orig_compiler_opts + ['-DNODERAWFS']
      self.set_setting('NODERAWFS')
      self.do_run_in_out_file_test('unistd/access.c')

  def test_unistd_curdir(self):
    self.uses_es6 = True
    self.do_run_in_out_file_test('unistd/curdir.c')

  @also_with_noderawfs
  def test_unistd_close(self):
    self.do_run_in_out_file_test('unistd/close.c')

  def test_unistd_fsync_stdout(self):
    self.do_run_in_out_file_test(test_file('unistd/fsync_stdout.c'))

  @also_with_noderawfs
  def test_unistd_pipe(self):
    self.do_runf(test_file('unistd/pipe.c'), 'success')

  @also_with_noderawfs
  def test_unistd_dup(self):
    self.do_run_in_out_file_test('unistd/dup.c')

  @parameterized({
    '': (['MEMFS']),
    'nodefs': (['NODEFS'])
  })
  def test_unistd_truncate(self, fs):
    self.uses_es6 = True
    orig_compiler_opts = self.emcc_args.copy()
    self.emcc_args = orig_compiler_opts + ['-D' + fs]
    if self.get_setting('WASMFS'):
      if fs == 'NODEFS':
        self.skipTest('TODO: NODEFS in WasmFS')
      self.emcc_args += ['-sFORCE_FILESYSTEM']
    if fs == 'NODEFS':
      self.emcc_args += ['-lnodefs.js']
      self.require_node()
    self.do_run_in_out_file_test('unistd/truncate.c')

  @no_windows("Windows throws EPERM rather than EACCES or EINVAL")
  @unittest.skipIf(WINDOWS or os.geteuid() == 0, "Root access invalidates this test by being able to write on readonly files")
  @requires_node
  def test_unistd_truncate_noderawfs(self):
    self.uses_es6 = True
    self.set_setting('NODERAWFS')
    self.maybe_closure()
    self.do_run_in_out_file_test('unistd/truncate.c')

  @also_with_standalone_wasm()
  def test_unistd_sysconf(self):
    if self.is_wasm64():
      out_suffix = '64'
    else:
      out_suffix = ''
    self.do_run_in_out_file_test('unistd/sysconf.c', out_suffix=out_suffix)

  @no_asan('ASan alters memory layout')
  def test_unistd_sysconf_phys_pages(self):
    filename = test_file('unistd/sysconf_phys_pages.c')
    if self.get_setting('ALLOW_MEMORY_GROWTH'):
      expected = (2 * 1024 * 1024 * 1024) // webassembly.WASM_PAGE_SIZE
    else:
      expected = 16 * 1024 * 1024 // webassembly.WASM_PAGE_SIZE
    self.do_runf(filename, str(expected) + ', errno: 0')

  @no_windows('https://github.com/emscripten-core/emscripten/issues/8882')
  @parameterized({
    '': (['MEMFS']),
    'nodefs': (['NODEFS']),
    'noderawfs': (['NODERAWFS']),
  })
  def test_unistd_unlink(self, fs):
    if fs in ('NODEFS', 'NODERAWFS'):
      self.require_node()
      if self.get_setting('WASMFS'):
        self.skipTest('NODEFS in WasmFS')

    self.emcc_args += ['-D' + fs]
    # symlinks on node.js on non-linux behave differently (e.g. on Windows they require administrative privileges)
    # so skip testing those bits on that combination.
    if fs == 'NODEFS':
      self.emcc_args += ['-lnodefs.js']
      if WINDOWS:
        self.emcc_args += ['-DNO_SYMLINK=1']
      if MACOS:
        self.skipTest('only tested on linux')

    # Several differences/bugs on non-linux including https://github.com/nodejs/node/issues/18014
    # TODO: NODERAWFS in WasmFS
    if fs == 'NODERAWFS':
      self.set_setting('NODERAWFS')
      # 0 if root user
      if os.geteuid() == 0:
        self.emcc_args += ['-DSKIP_ACCESS_TESTS']

    self.do_runf(test_file('unistd/unlink.c'), 'success')

  @parameterized({
    'memfs': (['-DMEMFS'], False),
    'nodefs': (['-DNODEFS', '-lnodefs.js'], True)
  })
  def test_unistd_links(self, args, nodefs):
    self.emcc_args += args

    if nodefs:
      self.require_node()
      if WINDOWS:
        self.skipTest('Skipping NODEFS part of this test for test_unistd_links on Windows, since it would require administrative privileges.')
        # Also, other detected discrepancies if you do end up running this test on NODEFS:
        # test expects /, but Windows gives \ as path slashes.
        # Calling readlink() on a non-link gives error 22 EINVAL on Unix, but simply error 0 OK on Windows.

    if self.get_setting('WASMFS'):
      if nodefs:
        self.skipTest('TODO: wasmfs+node')
      self.emcc_args += ['-sFORCE_FILESYSTEM']

    self.do_run_in_out_file_test('unistd/links.c')

  @no_windows('Skipping NODEFS test, since it would require administrative privileges.')
  @requires_node
  def test_unistd_symlink_on_nodefs(self):
    # Also, other detected discrepancies if you do end up running this test on NODEFS:
    # test expects /, but Windows gives \ as path slashes.
    # Calling readlink() on a non-link gives error 22 EINVAL on Unix, but simply error 0 OK on Windows.
    self.emcc_args += ['-lnodefs.js']
    self.do_run_in_out_file_test('unistd/symlink_on_nodefs.c')

  @also_with_wasm_bigint
  def test_unistd_io(self):
    orig_compiler_opts = self.emcc_args.copy()
    for fs in ['MEMFS', 'NODEFS']:
      self.clear()
      self.emcc_args = orig_compiler_opts + ['-D' + fs]
      if fs == 'NODEFS':
        self.emcc_args += ['-lnodefs.js']
        if config.NODE_JS not in config.JS_ENGINES:
          # NODEFS requires node
          continue
        self.require_node()
      if self.get_setting('WASMFS'):
        if fs == 'NODEFS':
          # TODO: NODEFS in WasmFS
          continue
        self.emcc_args += ['-sFORCE_FILESYSTEM']
      self.do_run_in_out_file_test('unistd/io.c')

  @no_windows('https://github.com/emscripten-core/emscripten/issues/8882')
  @parameterized({
    '': (['MEMFS']),
    'nodefs': (['NODEFS']),
  })
  def test_unistd_misc(self, fs):
    self.emcc_args += ['-D' + fs]
    if fs == 'NODEFS':
      self.require_node()
      self.emcc_args += ['-lnodefs.js']
    self.do_run_in_out_file_test('unistd/misc.c', interleaved_output=False)

  @also_with_standalone_wasm()
  def test_posixtime(self):
    self.do_core_test('test_posixtime.c')

  def test_uname(self):
    self.do_core_test('test_uname.c', regex=True)

  def test_unary_literal(self):
    self.do_core_test('test_unary_literal.cpp')

  def test_env(self):
    self.do_core_test('test_env.c', regex=True)

  def test_environ(self):
    self.do_core_test('test_environ.c', regex=True)

  def test_systypes(self):
    self.do_core_test('test_systypes.c')

  def test_stddef(self):
    self.do_core_test('test_stddef.cpp')
    self.do_core_test('test_stddef.cpp', force_c=True)

  def test_getloadavg(self):
    self.do_core_test('test_getloadavg.c')

  def test_nl_types(self):
    self.do_core_test('test_nl_types.c')

  def test_799(self):
    src = test_file('799.cpp')
    self.do_runf(src, '''Set PORT family: 0, port: 3979
Get PORT family: 0
PORT: 3979
''')

  def test_ctype(self):
    self.do_core_test('test_ctype.c')

  def test_strcasecmp(self):
    self.do_core_test('test_strcasecmp.c')

  def test_atomic(self):
    self.do_core_test('test_atomic.c')

  def test_atomic_cxx(self):
    # the wasm backend has lock-free atomics, but not asm.js or asm2wasm
    self.emcc_args += ['-DIS_64BIT_LOCK_FREE=1']
    self.do_core_test('test_atomic_cxx.cpp')

  def test_phiundef(self):
    self.do_core_test('test_phiundef.c')

  def test_netinet_in(self):
    self.do_run_in_out_file_test('netinet/in.cpp')

  @needs_dylink
  def test_main_module_static_align(self):
    if self.get_setting('ALLOW_MEMORY_GROWTH'):
      self.skipTest('no shared modules with memory growth')
    self.set_setting('MAIN_MODULE')
    self.do_core_test('test_main_module_static_align.cpp')

  # libc++ tests

  def test_iostream_and_determinism(self):
    create_file('src.cpp', '''
      #include <iostream>

      int main() {
        std::cout << "hello world" << std::endl << 77 << "." << std::endl;
        return 0;
      }
    ''')

    num = 5
    for i in range(num):
      print('(iteration %d)' % i)

      # add some timing nondeterminism here, not that we need it, but whatever
      time.sleep(random.random() / (10 * num))
      self.do_runf('src.cpp', 'hello world\n77.\n')

      # Verify that this build is identical to the previous one
      if os.path.exists('src.js.previous'):
        self.assertBinaryEqual('src.js', 'src.js.previous')
      shutil.copy2('src.js', 'src.js.previous')

      # Same but for the wasm file.
      if self.is_wasm() and not self.get_setting('WASM2JS'):
        if os.path.exists('src.wasm.previous'):
          self.assertBinaryEqual('src.wasm', 'src.wasm.previous')
        shutil.copy2('src.wasm', 'src.wasm.previous')

  def test_stdvec(self):
    self.do_core_test('test_stdvec.cpp')

  @requires_node
  def test_random_device(self):
    self.maybe_closure()
    self.do_core_test('test_random_device.cpp')

  def test_reinterpreted_ptrs(self):
    self.do_core_test('test_reinterpreted_ptrs.cpp')

  def test_js_libraries(self):
    create_file('main.cpp', '''
      #include <stdio.h>
      extern "C" {
        extern void printey();
        extern int calcey(int x, int y);
      }
      int main() {
        printey();
        printf("*%d*\\n", calcey(10, 22));
        return 0;
      }
    ''')
    create_file('mylib1.js', '''
      mergeInto(LibraryManager.library, {
        printey: () => out('hello from lib!')
      });
    ''')
    create_file('mylib2.js', '''
      mergeInto(LibraryManager.library, {
        calcey: (x, y) => x + y
      });
    ''')

    self.emcc_args += ['--js-library', 'mylib1.js', '--js-library', 'mylib2.js']
    self.do_runf('main.cpp', 'hello from lib!\n*32*\n')

  @with_env_modify({'LC_ALL': 'latin-1', 'PYTHONUTF8': '0', 'PYTHONCOERCECLOCALE': '0'})
  @crossplatform
  def test_unicode_js_library(self):
    create_file('main.c', '''
      #include <stdio.h>

      extern void printey();

      int main() {
        printey();
        return 0;
      }
    ''')

    # First verify that we have correct overridden the default python file encoding.
    # The follow program should fail, assuming the above LC_CTYPE + PYTHONUTF8
    # are having the desired effect.
    # This means that files open()'d by emscripten without an explicit encoding will
    # cause this test to file, hopefully catching any places where we forget to do this.
    create_file('expect_fail.py', 'print(len(open(r"%s").read()))' % test_file('unicode_library.js'))
    err = self.expect_fail([PYTHON, 'expect_fail.py'], expect_traceback=True)
    self.assertContained('UnicodeDecodeError', err)

    create_file('modularize_post.js', '(async function main(){await Module();})()')
    self.emcc_args += ['-sMODULARIZE', '--js-library', test_file('unicode_library.js'), '--extern-post-js', 'modularize_post.js', '--post-js', test_file('unicode_postjs.js')]
    self.do_run_from_file('main.c', test_file('test_unicode_js_library.out'))

  def test_funcptr_import_type(self):
    self.emcc_args += ['--js-library', test_file('core/test_funcptr_import_type.js')]
    self.do_core_test('test_funcptr_import_type.cpp')

  @no_asan('ASan does not work with EXPORT_ALL')
  def test_constglobalunion(self):
    self.set_setting('EXPORT_ALL')

    self.do_run(r'''
#include <stdio.h>

struct one_const {
  long a;
};

struct two_consts {
  long a;
  long b;
};

union some_consts {
  struct one_const one;
  struct two_consts two;
};

union some_consts my_consts = {{
  1
}};

struct one_const addr_of_my_consts = {
  (long)(&my_consts)
};

int main(void) {
  printf("%li\n", (long)!!addr_of_my_consts.a);
  return 0;
}
    ''', '1')

  ### 'Medium' tests

  def test_fannkuch(self):
    results = [(1, 0), (2, 1), (3, 2), (4, 4), (5, 7), (6, 10), (7, 16), (8, 22)]
    self.build(test_file('fannkuch.cpp'))
    for i, j in results:
      print(i, j)
      self.do_run('fannkuch.js', 'Pfannkuchen(%d) = %d.' % (i, j), args=[str(i)], no_build=True)

  def test_raytrace(self):
    # TODO: Should we remove this test?
    self.skipTest('Relies on double value rounding, extremely sensitive')

    src = read_file(test_file('raytrace.cpp')).replace('double', 'float')
    output = read_file(test_file('raytrace.ppm'))
    self.do_run(src, output, args=['3', '16'])

  def test_fasta(self):
    results = [(1, '''GG*ctt**tgagc*'''),
               (20, '''GGCCGGGCGCGGTGGCTCACGCCTGTAATCCCAGCACTTT*cttBtatcatatgctaKggNcataaaSatgtaaaDcDRtBggDtctttataattcBgtcg**tacgtgtagcctagtgtttgtgttgcgttatagtctatttgtggacacagtatggtcaaa**tgacgtcttttgatctgacggcgttaacaaagatactctg*'''),
               (50, '''GGCCGGGCGCGGTGGCTCACGCCTGTAATCCCAGCACTTTGGGAGGCCGAGGCGGGCGGA*TCACCTGAGGTCAGGAGTTCGAGACCAGCCTGGCCAACAT*cttBtatcatatgctaKggNcataaaSatgtaaaDcDRtBggDtctttataattcBgtcg**tactDtDagcctatttSVHtHttKtgtHMaSattgWaHKHttttagacatWatgtRgaaa**NtactMcSMtYtcMgRtacttctWBacgaa**agatactctgggcaacacacatacttctctcatgttgtttcttcggacctttcataacct**ttcctggcacatggttagctgcacatcacaggattgtaagggtctagtggttcagtgagc**ggaatatcattcgtcggtggtgttaatctatctcggtgtagcttataaatgcatccgtaa**gaatattatgtttatttgtcggtacgttcatggtagtggtgtcgccgatttagacgtaaa**ggcatgtatg*''')]

    orig_src = read_file(test_file('fasta.cpp'))

    def test(extra_args):
      for t in ['float', 'double']:
        print(t)
        src = orig_src.replace('double', t)
        create_file('fasta.cpp', src)
        self.build('fasta.cpp', emcc_args=extra_args)
        for arg, output in results:
          self.do_run('fasta.js', output, args=[str(arg)],
                      output_nicerizer=lambda x: x.replace('\n', '*'),
                      no_build=True,
                      emcc_args=extra_args)
        shutil.copyfile('fasta.js', '%s.js' % t)

    test([])

  @needs_non_trapping_float_to_int
  def test_fasta_nontrapping(self):
    self.emcc_args += ['-mnontrapping-fptoint']
    self.test_fasta()

  def test_whets(self):
    self.do_runf(test_file('whets.cpp'), 'Single Precision C Whetstone Benchmark')

  # node is slower, and fail on 64-bit
  @requires_v8
  @no_asan('depends on the specifics of memory size, which for asan we are forced to increase')
  @no_lsan('depends on the specifics of memory size, which for lsan we are forced to increase')
  def test_dlmalloc_inline(self):
    # needed with typed arrays
    self.set_setting('INITIAL_MEMORY', '128mb')

    src = read_file(path_from_root('system/lib/dlmalloc.c')) + '\n\n\n' + read_file(test_file('dlmalloc_test.c'))
    self.do_run(src, '*1,0*', args=['200', '1'], force_c=True)
    self.do_run('src.js', '*400,0*', args=['400', '400'], force_c=True, no_build=True)

  # node is slower, and fail on 64-bit
  @requires_v8
  @no_asan('depends on the specifics of memory size, which for asan we are forced to increase')
  @no_lsan('depends on the specifics of memory size, which for lsan we are forced to increase')
  @no_wasmfs('wasmfs does some malloc/free during startup, fragmenting the heap, leading to differences later')
  def test_dlmalloc(self):
    # needed with typed arrays
    self.set_setting('INITIAL_MEMORY', '128mb')

    # Linked version
    self.do_runf(test_file('dlmalloc_test.c'), '*1,0*', args=['200', '1'])
    self.do_run('dlmalloc_test.js', '*400,0*', args=['400', '400'], no_build=True)

    # TODO: do this in other passes too, passing their opts into emcc
    if self.emcc_args == []:
      # emcc should build in dlmalloc automatically, and do all the sign correction etc. for it

      delete_file('src.js')
      self.run_process([EMCC, test_file('dlmalloc_test.c'), '-sINITIAL_MEMORY=128MB', '-o', 'src.js'], stdout=PIPE, stderr=self.stderr_redirect)

      self.do_run(None, '*1,0*', ['200', '1'], no_build=True)
      self.do_run(None, '*400,0*', ['400', '400'], no_build=True)

      # The same for new and all its variants
      src = read_file(test_file('new.cpp'))
      for new, delete in [
        ('malloc(100)', 'free'),
        ('new char[100]', 'delete[]'),
        ('new Structy', 'delete'),
        ('new int', 'delete'),
        ('new Structy[10]', 'delete[]'),
      ]:
        self.do_run(src.replace('{{{ NEW }}}', new).replace('{{{ DELETE }}}', delete), '*1,0*')

  # Tests that a large allocation should gracefully fail
  @no_asan('the memory size limit here is too small for asan')
  @no_lsan('the memory size limit here is too small for lsan')
  def test_dlmalloc_large(self):
    self.emcc_args += ['-sABORTING_MALLOC=0', '-sALLOW_MEMORY_GROWTH=1', '-sMAXIMUM_MEMORY=128MB']
    self.do_runf(test_file('dlmalloc_test_large.c'), '0 0 0 1')

  @no_asan('asan also changes malloc, and that ends up linking in new twice')
  @no_lsan('lsan also changes malloc, and that ends up linking in new twice')
  def test_dlmalloc_partial(self):
    # present part of the symbols of dlmalloc, not all
    src = read_file(test_file('new.cpp')).replace('{{{ NEW }}}', 'new int').replace('{{{ DELETE }}}', 'delete') + '''
#include <emscripten/console.h>
#include <new>

void* operator new(size_t size) {
  emscripten_console_log("new!");
  return malloc(size);
}
'''
    self.do_run(src, 'new!\n*1,0*')

  @no_asan('asan also changes malloc, and that ends up linking in new twice')
  @no_lsan('lsan also changes malloc, and that ends up linking in new twice')
  def test_dlmalloc_partial_2(self):
    if 'SAFE_HEAP' in str(self.emcc_args):
      self.skipTest('we do unsafe stuff here')
    # present part of the symbols of dlmalloc, not all. malloc is harder to link than new which is weak.
    self.do_core_test('test_dlmalloc_partial_2.c', assert_returncode=NON_ZERO)

  def test_libcxx(self):
    self.do_runf(test_file('hashtest.cpp'),
                 'june -> 30\nPrevious (in alphabetical order) is july\nNext (in alphabetical order) is march')

    self.do_run('''
      #include <set>
      #include <stdio.h>
      int main() {
        std::set<int> fetchOriginatorNums;
        fetchOriginatorNums.insert(171);
        printf("hello world\\n");
        return 0;
      }
      ''', 'hello world')

  def test_typeid(self):
    self.do_core_test('test_typeid.cpp')

  def test_static_variable(self):
    # needs atexit
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_static_variable.cpp')

  def test_fakestat(self):
    self.do_core_test('test_fakestat.c')

  @also_with_standalone_wasm()
  def test_mmap_anon(self):
    # ASan needs more memory, but that is set up separately
    if '-fsanitize=address' not in self.emcc_args:
      self.set_setting('INITIAL_MEMORY', '128mb')

    self.do_core_test('test_mmap_anon.c')

  @node_pthreads
  def test_mmap_anon_pthreads(self):
    # Same test with threading enabled so give is some basic sanity
    # checks of the locking on the internal data structures.
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('INITIAL_MEMORY', '64mb')
    self.do_core_test('test_mmap_anon.c')

  @no_lsan('Test code contains memory leaks')
  @parameterized({
      '': (False,),
      '_asyncify': (True,)
  })
  def test_cubescript(self, asyncify):
    # uses register keyword
    self.emcc_args += ['-std=c++03', '-Wno-dynamic-class-memaccess']
    self.maybe_closure()
    self.emcc_args += ['-I', test_file('third_party/cubescript')]
    # Test code contains memory leaks
    if '-fsanitize=address' in self.emcc_args:
      self.emcc_args += ['--pre-js', test_file('asan-no-leak.js')]

    if asyncify:
      if self.is_wasm64():
        self.skipTest('TODO: asyncify for wasm64')
      self.set_setting('ASYNCIFY')

    src = test_file('third_party/cubescript/command.cpp')
    self.do_runf(src, '*\nTemp is 33\n9\n5\nhello, everyone\n*')

  @needs_dylink
  def test_relocatable_void_function(self):
    self.set_setting('RELOCATABLE')
    self.do_core_test('test_relocatable_void_function.c')

  @wasm_simd
  def test_wasm_intrinsics_simd(self):
    def run():
      self.do_runf(test_file('test_wasm_intrinsics_simd.c'), 'Success!')
    # Improves test readability
    self.emcc_args.append('-Wno-c++11-narrowing')
    self.emcc_args = ['-Wpedantic', '-Werror', '-Wall', '-xc++'] + self.emcc_args
    run()
    self.emcc_args.append('-funsigned-char')
    run()

  # Tests invoking the NEON SIMD API via arm_neon.h header
  @wasm_simd
  def test_neon_wasm_simd(self):
    self.emcc_args.append('-Wno-c++11-narrowing')
    self.emcc_args.append('-mfpu=neon')
    self.emcc_args.append('-msimd128')
    self.do_runf(test_file('neon/test_neon_wasm_simd.cpp'), 'Success!')

  # Tests invoking the SIMD API via x86 SSE1 xmmintrin.h header (_mm_x() functions)
  @wasm_simd
  @crossplatform
  @requires_native_clang
  @no_safe_heap('has unaligned 64-bit operations in wasm')
  @no_ubsan('test contains UB')
  def test_sse1(self):
    src = test_file('sse/test_sse1.cpp')
    self.run_process([shared.CLANG_CXX, src, '-msse', '-o', 'test_sse1', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_sse1', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), '-msse']
    self.maybe_closure()

    self.do_runf(src, native_result)

  # Tests invoking the SIMD API via x86 SSE2 emmintrin.h header (_mm_x() functions)
  @wasm_simd
  @requires_native_clang
  @no_wasm64('https://github.com/llvm/llvm-project/issues/57577')
  @no_safe_heap('has unaligned 64-bit operations in wasm')
  @is_slow_test
  @no_ubsan('https://github.com/emscripten-core/emscripten/issues/19688')
  def test_sse2(self):
    src = test_file('sse/test_sse2.cpp')
    self.run_process([shared.CLANG_CXX, src, '-msse2', '-Wno-argument-outside-range', '-o', 'test_sse2', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_sse2', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), '-msse2', '-Wno-argument-outside-range', '-sSTACK_SIZE=1MB']
    self.maybe_closure()
    self.do_runf(src, native_result)

  # Tests invoking the SIMD API via x86 SSE3 pmmintrin.h header (_mm_x() functions)
  @wasm_simd
  @requires_native_clang
  def test_sse3(self):
    src = test_file('sse/test_sse3.cpp')
    self.run_process([shared.CLANG_CXX, src, '-msse3', '-Wno-argument-outside-range', '-o', 'test_sse3', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_sse3', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), '-msse3', '-Wno-argument-outside-range']
    self.maybe_closure()
    self.do_runf(src, native_result)

  # Tests invoking the SIMD API via x86 SSSE3 tmmintrin.h header (_mm_x() functions)
  @no_wasm64('https://github.com/llvm/llvm-project/issues/57577')
  @wasm_simd
  @requires_native_clang
  def test_ssse3(self):
    src = test_file('sse/test_ssse3.cpp')
    self.run_process([shared.CLANG_CXX, src, '-mssse3', '-Wno-argument-outside-range', '-o', 'test_ssse3', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_ssse3', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), '-mssse3', '-Wno-argument-outside-range']
    self.maybe_closure()
    self.do_runf(src, native_result)

  # Tests invoking the SIMD API via x86 SSE4.1 smmintrin.h header (_mm_x() functions)
  @no_wasm64('https://github.com/llvm/llvm-project/issues/57577')
  @no_ubsan('https://github.com/emscripten-core/emscripten/issues/19749')
  @wasm_simd
  @requires_native_clang
  @is_slow_test
  def test_sse4_1(self):
    src = test_file('sse/test_sse4_1.cpp')
    if not self.is_optimizing() and '-fsanitize=address' in self.emcc_args:
      # ASan with -O0 fails with:
      # Compiling function #69:"__original_main" failed: local count too large
      self.emcc_args.append('-O1')
    self.run_process([shared.CLANG_CXX, src, '-msse4.1', '-Wno-argument-outside-range', '-o', 'test_sse4_1', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_sse4_1', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), '-msse4.1', '-Wno-argument-outside-range', '-sSTACK_SIZE=1MB']
    self.maybe_closure()
    self.do_runf(src, native_result)

  # Tests invoking the SIMD API via x86 SSE4.2 nmmintrin.h header (_mm_x() functions)
  @wasm_simd
  @requires_native_clang
  @parameterized({
      '': (False,),
      '2': (True,)
  })
  def test_sse4(self, use_4_2):
    msse4 = '-msse4.2' if use_4_2 else '-msse4'
    src = test_file('sse/test_sse4_2.cpp')
    self.run_process([shared.CLANG_CXX, src, msse4, '-Wno-argument-outside-range', '-o', 'test_sse4_2', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_sse4_2', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), msse4, '-Wno-argument-outside-range']
    self.maybe_closure()
    self.do_runf(src, native_result)

  # Tests invoking the SIMD API via x86 AVX avxintrin.h header (_mm_x() functions)
  @wasm_simd
  @requires_native_clang
  @is_slow_test
  @no_asan('local count too large')
  def test_avx(self):
    src = test_file('sse/test_avx.cpp')
    self.run_process([shared.CLANG_CXX, src, '-mavx', '-Wno-argument-outside-range', '-o', 'test_avx', '-D_CRT_SECURE_NO_WARNINGS=1'] + clang_native.get_clang_native_args(), stdout=PIPE)
    native_result = self.run_process('./test_avx', stdout=PIPE).stdout

    self.emcc_args += ['-I' + test_file('sse'), '-mavx', '-Wno-argument-outside-range', '-sSTACK_SIZE=1MB']
    self.maybe_closure()
    self.do_runf(src, native_result)

  @wasm_simd
  def test_sse_diagnostics(self):
    self.emcc_args.remove('-Werror')
    src = test_file('sse/test_sse_diagnostic.cpp')

    p = self.run_process(
      [shared.EMXX, src, '-msse', '-DWASM_SIMD_COMPAT_SLOW'] + self.get_emcc_args(),
      stderr=PIPE)
    self.assertContained('Instruction emulated via slow path.', p.stderr)

  @requires_native_clang
  @wasm_relaxed_simd
  def test_relaxed_simd_implies_simd128(self):
    src = test_file('sse/test_sse1.cpp')
    self.build(src, emcc_args=['-msse'])

  @no_asan('call stack exceeded on some versions of node')
  def test_gcc_unmangler(self):
    self.emcc_args += ['-I' + test_file('third_party/libiberty')]

    self.do_runf(test_file('third_party/libiberty/cp-demangle.c'), '*d_demangle(char const*, int, unsigned int*)*', args=['_ZL10d_demanglePKciPj'])

  @needs_make('make')
  @crossplatform
  def test_lua(self):
    self.emcc_args.remove('-Werror')
    env_init = {
      'SYSCFLAGS': ' '.join(self.get_emcc_args(ldflags=False)),
      'SYSLDFLAGS': ' '.join(self.get_emcc_args(ldflags=True))
    }
    libs = self.get_library('third_party/lua',
                            [Path('src/lua.o'), Path('src/liblua.a')],
                            make=['make', 'echo', 'generic'],
                            env_init=env_init,
                            configure=None)
    self.do_run('',
                'hello lua world!\n17\n1\n2\n3\n4\n7',
                args=['-e', '''print("hello lua world!");print(17);for x = 1,4 do print(x) end;print(10-3)'''],
                libraries=libs,
                includes=[test_file('lua')],
                output_nicerizer=lambda output: output.replace('\n\n', '\n').replace('\n\n', '\n'))

  @no_asan('issues with freetype itself')
  @needs_make('configure script')
  @no_wasm64('MEMORY64 does not yet support SJLJ')
  @is_slow_test
  def test_freetype(self):
    if self.get_setting('WASMFS'):
      self.emcc_args += ['-sFORCE_FILESYSTEM']

    self.add_pre_run("FS.createDataFile('/', 'font.ttf', %s, true, false, false);" % str(
      list(bytearray(read_binary(test_file('freetype/LiberationSansBold.ttf'))))
    ))

    # Not needed for js, but useful for debugging
    shutil.copyfile(test_file('freetype/LiberationSansBold.ttf'), 'font.ttf')

    # Main
    self.do_run_from_file(test_file('freetype/main.c'),
                          test_file('freetype/ref.txt'),
                          args=['font.ttf', 'test!', '150', '120', '25'],
                          libraries=self.get_freetype_library(),
                          includes=[test_file('third_party/freetype/include')])

    # github issue 324
    print('[issue 324]')
    self.do_run_from_file(test_file('freetype/main_2.c'),
                          test_file('freetype/ref_2.txt'),
                          args=['font.ttf', 'w', '32', '32', '25'],
                          libraries=self.get_freetype_library(),
                          includes=[test_file('third_party/freetype/include')])

    print('[issue 324 case 2]')
    self.do_run_from_file(test_file('freetype/main_3.c'),
                          test_file('freetype/ref_3.txt'),
                          args=['font.ttf', 'W', '32', '32', '0'],
                          libraries=self.get_freetype_library(),
                          includes=[test_file('third_party/freetype/include')])

    print('[issue 324 case 3]')
    self.do_run('main_3.js',
                read_file(test_file('freetype/ref_4.txt')),
                args=['font.ttf', 'ea', '40', '32', '0'],
                no_build=True)

  @no_asan('local count too large for VMs')
  @no_ubsan('local count too large for VMs')
  @is_slow_test
  @parameterized({
    '': (False,),
    'pthreads': (True,),
  })
  def test_sqlite(self, use_pthreads):
    if use_pthreads:
      self.emcc_args.append('-pthread')
      self.setup_node_pthreads()
    self.emcc_args += ['-sUSE_SQLITE3']
    self.do_run_in_out_file_test(test_file('sqlite/benchmark.c'))

  @needs_make('mingw32-make')
  @is_slow_test
  @parameterized({
    'cmake': (True,),
    'configure': (False,)
  })
  def test_zlib(self, use_cmake):
    if WINDOWS and not use_cmake:
      self.skipTest("Windows cannot run configure sh scripts")

    self.maybe_closure()
    if '-g' in self.emcc_args:
      self.emcc_args.append('-gsource-map') # more source maps coverage

    zlib = self.get_zlib_library(use_cmake)

    # example.c uses K&R style function declarations
    self.emcc_args += ['-Wno-deprecated-non-prototype']
    self.do_run_from_file(
        test_file('third_party/zlib/example.c'),
        test_file('core/test_zlib.out'),
        libraries=zlib,
        includes=[test_file('third_party/zlib')])

  @needs_make('make')
  @is_slow_test
  @no_ubsan('it seems that bullet contains UB')
  @parameterized({
    'cmake': (True,),
    'autoconf': (False,)
  })
  # Called thus so it runs late in the alphabetical cycle... it is long
  def test_bullet(self, use_cmake):
    if WINDOWS and not use_cmake:
      self.skipTest("Windows cannot run configure sh scripts")

    self.emcc_args += [
      '-Wno-c++11-narrowing',
      '-Wno-deprecated-register',
      '-Wno-writable-strings',
      '-Wno-shift-negative-value',
      '-Wno-format',
      '-Wno-bitfield-constant-conversion',
      '-Wno-int-to-void-pointer-cast',
    ]

    # extra testing for ASSERTIONS == 2
    if use_cmake:
      self.set_setting('ASSERTIONS', 2)
      self.emcc_args.append('-Wno-unused-command-line-argument')

    self.do_runf(test_file('third_party/bullet/Demos/HelloWorld/HelloWorld.cpp'),
                 [read_file(test_file('bullet/output.txt')), # different roundings
                  read_file(test_file('bullet/output2.txt')),
                  read_file(test_file('bullet/output3.txt')),
                  read_file(test_file('bullet/output4.txt'))],
                 libraries=self.get_bullet_library(use_cmake),
                 includes=[test_file('third_party/bullet/src')])

  @no_asan('issues with freetype itself')
  @no_ubsan('local count too large')
  @no_lsan('output differs')
  @needs_make('depends on freetype')
  @no_wasm64('MEMORY64 does not yet support SJLJ')
  @is_slow_test
  def test_poppler(self):
    poppler = self.get_poppler_library()
    pdf_data = read_binary(test_file('poppler/paper.pdf'))
    create_file('paper.pdf.js', str(list(bytearray(pdf_data))))

    create_file('pre.js', '''
    Module.preRun = () => {
      FS.createDataFile('/', 'paper.pdf', eval(read_('paper.pdf.js')), true, false, false);
    };
    Module.postRun = () => {
      var FileData = Array.from(MEMFS.getFileDataAsTypedArray(FS.root.contents['filename-1.ppm']));
      out("Data: " + JSON.stringify(FileData.map(function(x) { return unSign(x, 8) })));
    };
    ''')
    self.emcc_args += ['--pre-js', 'pre.js', '-sDEFAULT_LIBRARY_FUNCS_TO_INCLUDE=$unSign']

    ppm_data = str(list(bytearray(read_binary(test_file('poppler/ref.ppm')))))
    self.do_run('', ppm_data.replace(' ', ''),
                libraries=poppler,
                args=['-scale-to', '512', 'paper.pdf', 'filename'])

  @needs_make('make')
  @no_wasm64('MEMORY64 does not yet support SJLJ')
  @is_slow_test
  def test_openjpeg(self):
    def do_test_openjpeg():
      def line_splitter(data):
        out = ''
        counter = 0

        for ch in data:
          out += ch
          if ch == ' ' and counter > 60:
            out += '\n'
            counter = 0
          else:
            counter += 1

        return out

      # remove -g, so we have one test without it by default
      self.emcc_args = [x for x in self.emcc_args if x != '-g']

      original_j2k = test_file('openjpeg/syntensity_lobby_s.j2k')
      image_bytes = list(bytearray(read_binary(original_j2k)))
      create_file('pre.js', """
        Module.preRun = () => FS.createDataFile('/', 'image.j2k', %s, true, false, false);
        Module.postRun = () => {
          out('Data: ' + JSON.stringify(Array.from(FS.readFile('image.raw'))));
        };
        """ % line_splitter(str(image_bytes)))

      # ensure libpng is built so that openjpeg's configure step can detect it.
      # If we don't do this then we don't know what the state of the cache will be
      # and this test would different non-deterministic results based on, for example,
      # what other tests had previously run.
      builder_cmd = [EMBUILDER, 'build', 'libpng']
      if self.get_setting('MEMORY64'):
        builder_cmd.append('--wasm64')
        self.emcc_args.append('-Wno-pointer-to-int-cast')
      self.run_process(builder_cmd)
      lib = self.get_library('third_party/openjpeg',
                             [Path('codec/CMakeFiles/j2k_to_image.dir/index.c.o'),
                              Path('codec/CMakeFiles/j2k_to_image.dir/convert.c.o'),
                              Path('codec/CMakeFiles/j2k_to_image.dir/__/common/color.c.o'),
                              Path('bin/libopenjpeg.a')],
                             configure=['cmake', '.'],
                             # configure_args=['--enable-tiff=no', '--enable-jp3d=no', '--enable-png=no'],
                             make_args=[]) # no -j 2, since parallel builds can fail

      # We use doubles in JS, so we get slightly different values than native code. So we
      # check our output by comparing the average pixel difference
      def image_compare(output):
        # Get the image generated by JS, from the JSON.stringify'd array
        m = re.search(r'\[[\d, -]*\]', output)
        self.assertIsNotNone(m, 'Failed to find proper image output in: ' + output)
        # Evaluate the output as a python array
        js_data = eval(m.group(0))

        js_data = [x if x >= 0 else 256 + x for x in js_data] # Our output may be signed, so unsign it

        # Get the correct output
        true_data = bytearray(read_binary(test_file('openjpeg/syntensity_lobby_s.raw')))

        # Compare them
        self.assertEqual(len(js_data), len(true_data))
        num = len(js_data)
        diff_total = js_total = true_total = 0
        for i in range(num):
          js_total += js_data[i]
          true_total += true_data[i]
          diff_total += abs(js_data[i] - true_data[i])
        js_mean = js_total / float(num)
        true_mean = true_total / float(num)
        diff_mean = diff_total / float(num)

        image_mean = 83.265
        # print '[image stats:', js_mean, image_mean, true_mean, diff_mean, num, ']'
        assert abs(js_mean - image_mean) < 0.01, [js_mean, image_mean]
        assert abs(true_mean - image_mean) < 0.01, [true_mean, image_mean]
        assert diff_mean < 0.01, diff_mean

        return output

      self.emcc_args += ['--minify=0'] # to compare the versions
      self.emcc_args += ['--pre-js', 'pre.js']

      def do_test():
        self.do_runf(test_file('third_party/openjpeg/codec/j2k_to_image.c'),
                     'Successfully generated', # The real test for valid output is in image_compare
                     args='-i image.j2k -o image.raw'.split(),
                     emcc_args=['-sUSE_LIBPNG'],
                     libraries=lib,
                     includes=[test_file('third_party/openjpeg/libopenjpeg'),
                               test_file('third_party/openjpeg/codec'),
                               test_file('third_party/openjpeg/common'),
                               Path(self.get_build_dir(), 'third_party/openjpeg')],
                     output_nicerizer=image_compare)

      do_test()

      # extra testing
      if self.get_setting('ALLOW_MEMORY_GROWTH') == 1:
        print('no memory growth', file=sys.stderr)
        self.set_setting('ALLOW_MEMORY_GROWTH', 0)
        do_test()

    if is_sanitizing(self.emcc_args):
      # In ASan mode we need a large initial memory (or else wasm-ld fails).
      # The OpenJPEG CMake will build several executables (which we need parts
      # of in our testing, see above), so we must enable the flag for them all.
      with env_modify({'EMCC_CFLAGS': '-sINITIAL_MEMORY=300MB'}):
        self.emcc_args.append('-Wno-unused-command-line-argument')
        do_test_openjpeg()
    else:
      do_test_openjpeg()

  @also_with_standalone_wasm(impure=True)
  @no_asan('autodebug logging interferes with asan')
  @with_env_modify({'EMCC_AUTODEBUG': '1'})
  def test_autodebug_wasm(self):
    output = self.do_runf(test_file('core/test_autodebug.c'), 'success')
    # test that the program both works and also emits some of the logging
    # (but without the specific output, as it is logging the actual locals
    # used and so forth, which will change between opt modes and updates of
    # llvm etc.)
    for msg in ['log_execution', 'get_i32', 'set_i32', 'load_ptr', 'load_val', 'store_ptr', 'store_val']:
      self.assertIn(msg, output)

  ### Integration tests

  @crossplatform
  def test_ccall(self):
    self.emcc_args.append('-Wno-return-stack-address')
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['ccall', 'cwrap', 'STACK_SIZE'])
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    create_file('post.js', '''
      out('*');
      var ret;
      ret = Module['ccall']('get_int', 'number'); out([typeof ret, ret].join(','));
      ret = ccall('get_float', 'number'); out([typeof ret, ret.toFixed(2)].join(','));
      ret = ccall('get_bool', 'boolean'); out([typeof ret, ret].join(','));
      ret = ccall('get_string', 'string'); out([typeof ret, ret].join(','));
      ret = ccall('print_int', null, ['number'], [12]); out(typeof ret);
      ret = ccall('print_float', null, ['number'], [14.56]); out(typeof ret);
      ret = ccall('print_bool', null, ['boolean'], [true]); out(typeof ret);
      ret = ccall('print_string', null, ['string'], ["cheez"]); out(typeof ret);
      ret = ccall('print_string', null, ['array'], [[97, 114, 114, 45, 97, 121, 0]]); out(typeof ret); // JS array
      ret = ccall('print_string', null, ['array'], [new Uint8Array([97, 114, 114, 45, 97, 121, 0])]); out(typeof ret); // typed array
      ret = ccall('multi', 'number', ['number', 'number', 'number', 'string'], [2, 1.4, 3, 'more']); out([typeof ret, ret].join(','));
      var p = ccall('malloc', 'pointer', ['number'], [4]);
      setValue(p, 650, 'i32');
      ret = ccall('pointer', 'pointer', ['pointer'], [p]); out([typeof ret, getValue(ret, 'i32')].join(','));
      out('*');
      // part 2: cwrap
      var noThirdParam = Module['cwrap']('get_int', 'number');
      out(noThirdParam());
      var multi = Module['cwrap']('multi', 'number', ['number', 'number', 'number', 'string']);
      out(multi(2, 1.4, 3, 'atr'));
      out(multi(8, 5.4, 4, 'bret'));
      out('*');
      // part 3: avoid stack explosion and check it's restored correctly
      for (var i = 0; i < STACK_SIZE/60; i++) {
        ccall('multi', 'number', ['number', 'number', 'number', 'string'], [0, 0, 0, '123456789012345678901234567890123456789012345678901234567890']);
      }
      out('stack is ok.');
      ccall('call_ccall_again', null);
      ''')
    self.emcc_args += ['--post-js', 'post.js']

    self.set_setting('EXPORTED_FUNCTIONS', ['_get_int', '_get_float', '_get_bool', '_get_string', '_print_int', '_print_float', '_print_bool', '_print_string', '_multi', '_pointer', '_call_ccall_again', '_malloc'])
    self.do_core_test('test_ccall.cpp')

    if self.maybe_closure():
      self.do_core_test('test_ccall.cpp')

  def test_ccall_cwrap_fast_path(self):
    self.emcc_args.append('-Wno-return-stack-address')
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['ccall', 'cwrap'])
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    self.set_setting('ASSERTIONS', 0)
    create_file('post.js', '''
      var printBool = Module['cwrap']('print_bool', null, ['boolean']);
      out(Module['_print_bool'] === printBool); // the function should be the exact raw function in the module rather than a wrapped one
      ''')
    self.emcc_args += ['--post-js', 'post.js']

    self.set_setting('EXPORTED_FUNCTIONS', ['_print_bool'])
    self.do_runf(test_file('core/test_ccall.cpp'), 'true')

  def test_EXPORTED_RUNTIME_METHODS(self):
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$dynCall', '$ASSERTIONS'])
    self.do_core_test('EXPORTED_RUNTIME_METHODS.c')
    # test dyncall (and other runtime methods) can be exported
    self.emcc_args += ['-DEXPORTED']
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['dynCall', 'addFunction', 'lengthBytesUTF8', 'getTempRet0', 'setTempRet0'])
    self.do_core_test('EXPORTED_RUNTIME_METHODS.c')

  @parameterized({
    '': [],
    'minimal_runtime': ['-sMINIMAL_RUNTIME=1']
  })
  def test_dyncall_specific(self, *args):
    if self.get_setting('MEMORY64'):
      self.skipTest('not compatible with MEMORY64')
    if self.get_setting('WASM_BIGINT'):
      # define DYNCALLS because this test does test calling them directly, and
      # in WASM_BIGINT mode we do not enable them by default (since we can do
      # more without them - we don't need to legalize)
      args = list(args) + ['-sDYNCALLS', '-DWASM_BIGINT']
    cases = [
        ('DIRECT', []),
        ('DYNAMIC_SIG', ['-sDYNCALLS']),
      ]
    if '-sMINIMAL_RUNTIME=1' in args:
      self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    else:
      cases += [
        ('EXPORTED', []),
        ('EXPORTED_DYNAMIC_SIG', ['-sDYNCALLS', '-sEXPORTED_RUNTIME_METHODS=dynCall']),
        ('FROM_OUTSIDE', ['-sEXPORTED_RUNTIME_METHODS=dynCall_iiji'])
      ]

    for which, extra_args in cases:
      print(str(args) + ' ' + which)
      self.do_core_test('dyncall_specific.c', emcc_args=['-D' + which] + list(args) + extra_args)

  @also_with_wasm_bigint
  def test_getValue_setValue(self):
    # these used to be exported, but no longer are by default
    def test(args=None, asserts=False):
      if asserts:
        out_suffix = '_assert'
      else:
        out_suffix = ''
        if self.is_wasm64():
          out_suffix += '64'
        if self.get_setting('WASM_BIGINT'):
          out_suffix += '_bigint'
      assert_returncode = 0 if not asserts else NON_ZERO
      self.do_run_in_out_file_test('core/test_getValue_setValue.cpp',
                                   out_suffix=out_suffix,
                                   assert_returncode=assert_returncode,
                                   emcc_args=args)

    if self.get_setting('WASM_BIGINT'):
      self.emcc_args += ['-DWASM_BIGINT']

    # see that direct usage (not on module) works. we don't export, but the use
    # keeps it alive through JSDCE
    test(args=['-DDIRECT'])
    # see that with assertions, we get a nice error message
    self.set_setting('EXPORTED_RUNTIME_METHODS', [])
    self.set_setting('ASSERTIONS')
    test(asserts=True)
    self.set_setting('ASSERTIONS', 0)
    # see that when we export them, things work on the module
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['getValue', 'setValue'])
    test()

  @parameterized({
    '': ([],),
    '_files': (['-DUSE_FILES'],)
  })
  def test_FS_exports(self, extra_args):
    # these used to be exported, but no longer are by default
    def test(output_prefix='', args=None, assert_returncode=0):
      args += extra_args
      print(args)
      self.do_runf(test_file('core/FS_exports.cpp'),
                   (read_file(test_file('core/FS_exports' + output_prefix + '.out')),
                    read_file(test_file('core/FS_exports' + output_prefix + '_2.out'))),
                   assert_returncode=assert_returncode, emcc_args=args)

    # see that direct usage (not on module) works. we don't export, but the use
    # keeps it alive through JSDCE
    test(args=['-DDIRECT', '-sFORCE_FILESYSTEM'])
    # see that with assertions, we get a nice error message
    self.set_setting('EXPORTED_RUNTIME_METHODS', [])
    self.set_setting('ASSERTIONS')
    test('_assert', args=[], assert_returncode=NON_ZERO)
    self.set_setting('ASSERTIONS', 0)
    # see that when we export them, things work on the module
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['FS_createDataFile'])
    test(args=['-sFORCE_FILESYSTEM'])

  def test_legacy_exported_runtime_numbers(self):
    # these used to be exported, but no longer are by default
    def test(expected, args=None, assert_returncode=0):
      self.do_runf(test_file('core/legacy_exported_runtime_numbers.cpp'), expected,
                   assert_returncode=assert_returncode, emcc_args=args)

    # Without assertion indirect usages (via Module) result in `undefined` and direct usage
    # generates a builtin (not very helpful) JS error.
    self.set_setting('ASSERTIONS', 0)
    self.set_setting('LEGACY_RUNTIME', 0)
    test('|undefined|')
    test('ALLOC_STACK is not defined', args=['-DDIRECT'], assert_returncode=NON_ZERO)

    # When assertions are enabled direct and indirect usage both abort with a useful error message.
    not_exported = "Aborted('ALLOC_STACK' was not exported. add it to EXPORTED_RUNTIME_METHODS (see the Emscripten FAQ))"
    not_included = "`ALLOC_STACK` is a library symbol and not included by default; add it to your library.js __deps or to DEFAULT_LIBRARY_FUNCS_TO_INCLUDE on the command line (e.g. -sDEFAULT_LIBRARY_FUNCS_TO_INCLUDE='$ALLOC_STACK')"
    self.set_setting('ASSERTIONS')
    test(not_exported, assert_returncode=NON_ZERO)
    test(not_included, args=['-DDIRECT'])

    # Adding the symbol to DEFAULT_LIBRARY_FUNCS_TO_INCLUDE should allow direct usage, but
    # Module usage should continue to fail.
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$ALLOC_STACK'])
    test(not_exported, assert_returncode=NON_ZERO)
    test('1', args=['-DDIRECT'])

    # Adding the symbols to EXPORTED_RUNTIME_METHODS should make both usage patterns work.
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['ALLOC_STACK'])
    test('|1|')
    test('|1|', args=['-DDIRECT'])

  def test_response_file(self):
    response_data = '-o "%s/response_file.js" "%s"' % (self.get_dir(), test_file('hello_world.cpp'))
    create_file('rsp_file', response_data.replace('\\', '\\\\'))
    self.run_process([EMCC, "@rsp_file"] + self.get_emcc_args())
    self.do_run('response_file.js', 'hello, world', no_build=True)

    self.assertContained('response file not found: foo.txt', self.expect_fail([EMCC, '@foo.txt']))

  def test_linker_response_file(self):
    objfile = 'response_file.o'
    self.run_process([EMCC, '-c', test_file('hello_world.cpp'), '-o', objfile] + self.get_emcc_args(ldflags=False))
    # This should expand into -Wl,--start-group <objfile> -Wl,--end-group
    response_data = '--start-group ' + objfile + ' --end-group'
    create_file('rsp_file', response_data.replace('\\', '\\\\'))
    self.run_process([EMCC, "-Wl,@rsp_file", '-o', 'response_file.o.js'] + self.get_emcc_args())
    self.do_run('response_file.o.js', 'hello, world', no_build=True)

  def test_exported_response(self):
    src = r'''
      #include <stdio.h>
      #include <stdlib.h>
      #include <emscripten.h>

      extern "C" {
        int other_function() { return 5; }
      }

      int main() {
        int x = EM_ASM_INT({ return Module._other_function() });
        emscripten_run_script_string(""); // Add a reference to a symbol that exists in src/deps_info.json to uncover issue #2836 in the test suite.
        printf("waka %d!\n", x);
        return 0;
      }
    '''
    create_file('exps', '["_main","_other_function"]')

    self.set_setting('EXPORTED_FUNCTIONS', '@exps')
    self.do_run(src, '''waka 5!''')
    assert 'other_function' in read_file('src.js')

  def test_large_exported_response(self):
    src = r'''
      #include <stdio.h>
      #include <stdlib.h>
      #include <emscripten.h>

      extern "C" {
      '''

    js_funcs = []
    num_exports = 5000
    count = 0
    while count < num_exports:
      src += 'int exported_func_from_response_file_%d () { return %d;}\n' % (count, count)
      js_funcs.append('_exported_func_from_response_file_%d' % count)
      count += 1

    src += r'''
      }

      int main() {
        int x = EM_ASM_INT({ return Module._exported_func_from_response_file_4999() });
        // Add a reference to a symbol that exists in src/deps_info.json to uncover
        // issue #2836 in the test suite.
        emscripten_run_script_string("");
        printf("waka %d!\n", x);
        return 0;
      }
    '''

    js_funcs.append('_main')
    create_file('large_exported_response.json', json.dumps(js_funcs))

    self.set_setting('EXPORTED_FUNCTIONS', '@large_exported_response.json')
    self.do_run(src, 'waka 4999!')
    self.assertContained('_exported_func_from_response_file_1', read_file('src.js'))

  def test_emulate_function_pointer_casts(self):
    # Forcibly disable EXIT_RUNTIME due to:
    # https://github.com/emscripten-core/emscripten/issues/15081
    self.set_setting('EXIT_RUNTIME', 0)
    self.set_setting('EMULATE_FUNCTION_POINTER_CASTS')
    self.do_core_test('test_emulate_function_pointer_casts.cpp')

  @no_wasm2js('TODO: nicely printed names in wasm2js')
  @parameterized({
    'normal': ([],),
    'noexcept': (['-fno-exceptions'],)
  })
  def test_demangle_stacks(self, extra_args):
    self.emcc_args += extra_args
    self.set_setting('DEMANGLE_SUPPORT')
    self.set_setting('ASSERTIONS')
    # disable aggressive inlining in binaryen
    self.set_setting('BINARYEN_EXTRA_PASSES', '--one-caller-inline-max-function-size=1')
    # ensure function names are preserved
    self.emcc_args += ['--profiling-funcs']
    self.do_core_test('test_demangle_stacks.cpp', assert_returncode=NON_ZERO)

    # there should be a name section in the file
    with webassembly.Module('test_demangle_stacks.wasm') as m:
      self.assertTrue(m.has_name_section())

    print('without assertions, the stack is not printed, but a message suggesting assertions is')
    self.set_setting('ASSERTIONS', 0)
    self.do_core_test('test_demangle_stacks_noassert.cpp', assert_returncode=NON_ZERO)

  def test_demangle_stacks_symbol_map(self):
    # disable aggressive inlining in binaryen
    self.set_setting('BINARYEN_EXTRA_PASSES', '--one-caller-inline-max-function-size=1')
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', '$stackTrace')

    self.set_setting('DEMANGLE_SUPPORT')
    self.set_setting('ENVIRONMENT', 'node,shell')
    if '-O' not in str(self.emcc_args) or '-O0' in self.emcc_args or '-O1' in self.emcc_args or '-g' in self.emcc_args:
      self.skipTest("without opts, we don't emit a symbol map")
    self.emcc_args += ['--emit-symbol-map']
    self.do_runf(test_file('core/test_demangle_stacks.cpp'), 'Aborted', assert_returncode=NON_ZERO)
    # make sure the shortened name is the right one
    full_aborter = None
    short_aborter = None
    for line in read_file('test_demangle_stacks.js.symbols').splitlines():
      if ':' not in line:
        continue
      # split by the first ':' (wasm backend demangling may include more :'s later on)
      short, full = line.split(':', 1)
      if 'Aborter' in full:
        short_aborter = short
        full_aborter = full
    self.assertIsNotNone(full_aborter)
    self.assertIsNotNone(short_aborter)
    print('full:', full_aborter, 'short:', short_aborter)
    if config.SPIDERMONKEY_ENGINE:
      output = self.run_js('test_demangle_stacks.js', engine=config.SPIDERMONKEY_ENGINE, assert_returncode=NON_ZERO)
      # we may see the full one, if -g, or the short one if not
      if ' ' + short_aborter + ' ' not in output and ' ' + full_aborter + ' ' not in output:
        # stack traces may also be ' name ' or 'name@' etc
        if '\n' + short_aborter + ' ' not in output and '\n' + full_aborter + ' ' not in output and 'wasm-function[' + short_aborter + ']' not in output:
          if '\n' + short_aborter + '@' not in output and '\n' + full_aborter + '@' not in output:
            self.assertContained(' ' + short_aborter + ' ' + '\n' + ' ' + full_aborter + ' ', output)

  @no_safe_heap('tracing from sbrk into JS leads to an infinite loop')
  def test_tracing(self):
    self.emcc_args += ['--tracing']
    self.do_core_test('test_tracing.c')

  @no_wasm2js('eval_ctors not supported yet')
  @also_with_standalone_wasm()
  def test_eval_ctors(self):
    if '-O2' not in str(self.emcc_args) or '-O1' in str(self.emcc_args):
      self.skipTest('need opts')

    print('leave printf in ctor')
    self.set_setting('EVAL_CTORS')
    self.do_run(r'''
      #include <stdio.h>
      struct C {
        C() { printf("constructing!\n"); } // don't remove this!
      };
      C c;
      int main() {}
    ''', "constructing!\n")

    def do_test(test, level=1, prefix='src'):
      def get_code_size():
        if self.is_wasm():
          # this also includes the memory, but it is close enough for our
          # purposes
          return self.measure_wasm_code_lines(prefix + '.wasm')
        else:
          return os.path.getsize(prefix + '.js')

      self.set_setting('EVAL_CTORS', level)
      test()
      ec_code_size = get_code_size()
      self.clear_setting('EVAL_CTORS')
      test()
      code_size = get_code_size()
      print('code:', code_size, '=>', ec_code_size)
      self.assertLess(ec_code_size, code_size)

    print('remove ctor of just assigns to memory')

    def test1():
      self.do_run(r'''
        #include <stdio.h>
        struct C {
          int x;
          C() {
            volatile int y = 10;
            y++;
            x = y;
          }
        };
        C c;
        int main() {
          printf("x: %d\n", c.x);
        }
      ''', "x: 11\n")

    do_test(test1)

    print('libcxx - remove 2 ctors from iostream code')
    output = 'hello, world!'

    def test2():
      self.do_runf(test_file('hello_libcxx.cpp'), output)

    # in standalone more there is more usage of WASI APIs, which mode 2 is
    # needed to avoid in order to fully optimize, so do not test mode 1 in
    # that mode.
    if not self.get_setting('STANDALONE_WASM'):
      do_test(test2, level=1, prefix='hello_libcxx')

    do_test(test2, level=2, prefix='hello_libcxx')

  def test_embind(self):
    # Verify that both the old `--bind` arg and the new `-lembind` arg work
    for args in [['-lembind'], ['--bind']]:
      create_file('test_embind.cpp', r'''
      #include <stdio.h>
      #include <emscripten/val.h>

      using namespace emscripten;

      int main() {
        val Math = val::global("Math");

        // two ways to call Math.abs
        printf("abs(-10): %d\n", Math.call<int>("abs", -10));
        printf("abs(-11): %d\n", Math["abs"](-11).as<int>());

        return 0;
      }
      ''')
      self.do_runf('test_embind.cpp', 'abs(-10): 10\nabs(-11): 11', emcc_args=args)

  @parameterized({
    '': ([],),
    'pthreads': (['-pthread', '-sPROXY_TO_PTHREAD', '-sEXIT_RUNTIME'],),
  })
  @node_pthreads
  def test_embind_2(self, args):
    self.emcc_args += ['-lembind', '--post-js', 'post.js'] + args
    create_file('post.js', '''
      function printLerp() {
        out('lerp ' + Module.lerp(100, 200, 66) + '.');
      }
    ''')
    create_file('test_embind_2.cpp', r'''
      #include <stdio.h>
      #include <emscripten.h>
      #include <emscripten/bind.h>
      #include <emscripten/console.h>
      using namespace emscripten;
      int lerp(int a, int b, int t) {
        return (100 - t) * a + t * b;
      }
      EMSCRIPTEN_BINDINGS(my_module) {
        emscripten_err("test bindings");
        function("lerp", &lerp);
      }
      int main(int argc, char **argv) {
        EM_ASM(printLerp());
        return 0;
      }
    ''')
    self.do_runf('test_embind_2.cpp', 'lerp 166')

  def test_embind_3(self):
    self.emcc_args += ['-lembind', '--post-js', 'post.js']
    create_file('post.js', '''
      function ready() {
        try {
          Module.compute(new Uint8Array([1,2,3]));
        } catch(e) {
          out(e);
        }
      }
    ''')
    create_file('test_embind_3.cpp', r'''
      #include <emscripten.h>
      #include <emscripten/bind.h>
      using namespace emscripten;
      int compute(int array[]) {
          return 0;
      }
      EMSCRIPTEN_BINDINGS(my_module) {
          function("compute", &compute, allow_raw_pointers());
      }
      int main(int argc, char **argv) {
          EM_ASM(ready());
          return 0;
      }
    ''')
    self.do_runf('test_embind_3.cpp', 'UnboundTypeError: Cannot call compute due to unbound types: Pi')

  def test_embind_4(self):
    self.emcc_args += ['-lembind', '--post-js', 'post.js']
    create_file('post.js', '''
      function printFirstElement() {
        out(Module.getBufferView()[0]);
      }
    ''')
    create_file('test_embind_4.cpp', r'''
      #include <emscripten.h>
      #include <emscripten/bind.h>
      #include <emscripten/val.h>
      #include <stdio.h>
      using namespace emscripten;

      const size_t kBufferSize = 1024;
      double buffer[kBufferSize];
      val getBufferView(void) {
          val v = val(typed_memory_view(kBufferSize, buffer));
          return v;
      }
      EMSCRIPTEN_BINDINGS(my_module) {
          function("getBufferView", &getBufferView);
      }

      int main(int argc, char **argv) {
        buffer[0] = 107;
        EM_ASM(printFirstElement());
        return 0;
      }
    ''')
    self.do_runf('test_embind_4.cpp', '107')

  def test_embind_5(self):
    self.emcc_args += ['-lembind']
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_embind_5.cpp')

  def test_embind_custom_marshal(self):
    self.emcc_args += ['-lembind', '--pre-js', test_file('embind/test_custom_marshal.js')]
    self.do_run_in_out_file_test('embind/test_custom_marshal.cpp', assert_identical=True)

  def test_embind_float_constants(self):
    self.emcc_args += ['-lembind']
    self.do_run_in_out_file_test('embind/test_float_constants.cpp')

  def test_embind_negative_constants(self):
    self.emcc_args += ['-lembind']
    self.do_run_in_out_file_test('embind/test_negative_constants.cpp')

  @also_with_wasm_bigint
  def test_embind_unsigned(self):
    self.emcc_args += ['-lembind']
    self.do_run_in_out_file_test('embind/test_unsigned.cpp')

  def test_embind_val(self):
    self.emcc_args += ['-lembind']
    self.do_run_in_out_file_test('embind/test_val.cpp')

  def test_embind_val_assignment(self):
    err = self.expect_fail([EMCC, test_file('embind/test_val_assignment.cpp'), '-lembind', '-c'])
    self.assertContained('candidate function not viable: expects an lvalue for object argument', err)

  def test_embind_dynamic_initialization(self):
    self.emcc_args += ['-lembind']
    self.do_run_in_out_file_test('embind/test_dynamic_initialization.cpp')

  @no_wasm2js('wasm_bigint')
  def test_embind_i64_val(self):
    self.set_setting('WASM_BIGINT')
    self.emcc_args += ['-lembind']
    self.node_args += shared.node_bigint_flags()
    self.do_run_in_out_file_test('embind/test_i64_val.cpp', assert_identical=True)

  @no_wasm2js('wasm_bigint')
  def test_embind_i64_binding(self):
    self.set_setting('WASM_BIGINT')
    self.emcc_args += ['-lembind']
    self.node_args += shared.node_bigint_flags()
    self.do_run_in_out_file_test('embind/test_i64_binding.cpp', assert_identical=True)

  def test_embind_no_rtti(self):
    create_file('main.cpp', r'''
      #include <emscripten.h>
      #include <emscripten/bind.h>
      #include <emscripten/val.h>
      #include <stdio.h>

      EM_JS(void, calltest, (), {
        console.log("dotest returned: " + Module.dotest());
      });

      int main(int argc, char** argv){
        printf("418\n");
        calltest();
        return 0;
      }

      int test() {
        return 42;
      }

      EMSCRIPTEN_BINDINGS(my_module) {
        emscripten::function("dotest", &test);
      }
    ''')
    self.emcc_args += ['-lembind', '-fno-rtti', '-DEMSCRIPTEN_HAS_UNBOUND_TYPE_NAMES=0']
    self.do_runf('main.cpp', '418\ndotest returned: 42\n')

  def test_embind_polymorphic_class_no_rtti(self):
    self.emcc_args += ['-lembind', '-fno-rtti', '-DEMSCRIPTEN_HAS_UNBOUND_TYPE_NAMES=0']
    self.do_core_test('test_embind_polymorphic_class_no_rtti.cpp')

  def test_embind_no_rtti_followed_by_rtti(self):
    src = r'''
      #include <emscripten.h>
      #include <emscripten/bind.h>
      #include <emscripten/val.h>
      #include <stdio.h>

      EM_JS(void, calltest, (), {
        console.log("dotest returned: " + Module.dotest());
      });

      int main(int argc, char** argv){
        printf("418\n");
        calltest();
        return 0;
      }

      int test() {
        return 42;
      }

      EMSCRIPTEN_BINDINGS(my_module) {
        emscripten::function("dotest", &test);
      }
    '''
    self.emcc_args += ['-lembind', '-fno-rtti', '-frtti']
    self.do_run(src, '418\ndotest returned: 42\n')

  @no_wasm64('webidl not compatible with MEMORY64 yet')
  @parameterized({
    '': ('DEFAULT', False),
    'all': ('ALL', False),
    'fast': ('FAST', False),
    'default': ('DEFAULT', False),
    'all_growth': ('ALL', True),
  })
  def test_webidl(self, mode, allow_memory_growth):
    self.uses_es6 = True
    # TODO(): Remove once we make webidl output closure-warning free.
    self.ldflags.append('-Wno-error=closure')
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    if self.maybe_closure():
      # avoid closure minified names competing with our test code in the global name space
      self.set_setting('MODULARIZE')
      self.set_setting('EXPORT_NAME', 'createModule')
    else:
      self.set_setting('WASM_ASYNC_COMPILATION', 0)

    # Force IDL checks mode
    with env_modify({'IDL_CHECKS': mode}):
      self.run_process([WEBIDL_BINDER, test_file('webidl/test.idl'), 'glue'])
    self.assertExists('glue.cpp')
    self.assertExists('glue.js')

    post_js = '\n\n'
    if self.get_setting('MODULARIZE'):
      post_js += 'var TheModule = createModule();\n'
    else:
      post_js += 'var TheModule = Module;\n'
    post_js += '\n\n'
    if allow_memory_growth:
      post_js += "var isMemoryGrowthAllowed = true;\n"
    else:
      post_js += "var isMemoryGrowthAllowed = false;\n"
    post_js += read_file(test_file('webidl/post.js'))
    post_js += '\n\n'
    create_file('extern-post.js', post_js)

    # Export things on "TheModule". This matches the typical use pattern of the bound library
    # being used as Box2D.* or Ammo.*, and we cannot rely on "Module" being always present (closure may remove it).
    self.emcc_args += ['-sEXPORTED_FUNCTIONS=_malloc,_free', '--post-js=glue.js', '--extern-post-js=extern-post.js']
    if mode == 'ALL':
      self.emcc_args += ['-sASSERTIONS']
    if allow_memory_growth:
      self.set_setting('ALLOW_MEMORY_GROWTH')

    expected = test_file('webidl/output_%s.txt' % mode)
    self.do_run_from_file(test_file('webidl/test.cpp'), expected, includes=['.'])

  # Test that we can perform fully-synchronous initialization when combining WASM_ASYNC_COMPILATION=0 + PTHREAD_POOL_DELAY_LOAD=1.
  # Also checks that PTHREAD_POOL_DELAY_LOAD=1 adds a pthreadPoolReady promise that users can wait on for pthread initialization.
  @node_pthreads
  def test_embind_sync_if_pthread_delayed(self):
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    self.set_setting('PTHREAD_POOL_DELAY_LOAD', 1)
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    self.emcc_args += ['-lembind', '--post-js=' + test_file('core/pthread/test_embind_sync_if_pthread_delayed.post.js')]
    self.do_run_in_out_file_test(test_file('core/pthread/test_embind_sync_if_pthread_delayed.cpp'))

  ### Tests for tools

  @no_wasm2js('TODO: source maps in wasm2js')
  @parameterized({
    '': ([],),
    'minimal_runtime': (['-sMINIMAL_RUNTIME'],),
  })
  @requires_node
  def test_source_map(self, args):
    if '-g' not in self.emcc_args:
      self.emcc_args.append('-g')

    self.emcc_args += args

    src = '''
      #include <stdio.h>
      #include <assert.h>

      __attribute__((noinline)) int foo() {
        printf("hi"); // line 6
        return 1; // line 7
      }

      int main() {
        printf("%d", foo()); // line 11
        return 0; // line 12
      }
    '''
    create_file('src.cpp', src)

    out_filename = 'a.out.js'
    wasm_filename = 'a.out.wasm'
    no_maps_filename = 'no-maps.out.js'

    assert '-gsource-map' not in self.emcc_args
    self.emcc('src.cpp', output_filename=out_filename)
    # the file name may find its way into the generated code, so make sure we
    # can do an apples-to-apples comparison by compiling with the same file name
    shutil.move(out_filename, no_maps_filename)
    no_maps_file = read_file(no_maps_filename)
    no_maps_file = re.sub(' *//[@#].*$', '', no_maps_file, flags=re.MULTILINE)
    self.emcc_args.append('-gsource-map')

    self.emcc(os.path.abspath('src.cpp'),
              self.get_emcc_args(),
              out_filename)
    map_referent = out_filename if not self.is_wasm() else wasm_filename
    # after removing the @line and @sourceMappingURL comments, the build
    # result should be identical to the non-source-mapped debug version.
    # this is worth checking because the parser AST swaps strings for token
    # objects when generating source maps, so we want to make sure the
    # optimizer can deal with both types.
    map_filename = map_referent + '.map'

    data = json.load(open(map_filename))
    if hasattr(data, 'file'):
      # the file attribute is optional, but if it is present it needs to refer
      # the output file.
      self.assertPathsIdentical(map_referent, data['file'])
    self.assertGreater(len(data['sources']), 1)
    self.assertContained('src.cpp', data['sources'])
    src_index = data['sources'].index('src.cpp')
    if hasattr(data, 'sourcesContent'):
      # the sourcesContent attribute is optional, but if it is present it
      # needs to containt valid source text.
      self.assertTextDataIdentical(src, data['sourcesContent'][src_index])
    mappings = json.loads(self.run_js(
      path_from_root('test/sourcemap2json.js'),
      args=[map_filename]))
    seen_lines = set()
    for m in mappings:
      if m['source'] == 'src.cpp':
        seen_lines.add(m['originalLine'])
    # ensure that all the 'meaningful' lines in the original code get mapped
    # when optimizing, the binaryen optimizer may remove some of them (by inlining, etc.)
    if self.is_optimizing():
      self.assertTrue(seen_lines.issuperset([11, 12]), seen_lines)
    else:
      self.assertTrue(seen_lines.issuperset([6, 7, 11, 12]), seen_lines)

  @no_wasm2js('TODO: source maps in wasm2js')
  def test_dwarf(self):
    self.emcc_args.append('-g')

    js_filename = 'a.out.js'
    wasm_filename = 'a.out.wasm'
    shutil.copyfile(test_file('core/test_dwarf.c'), 'test_dwarf.c')

    self.emcc('test_dwarf.c', output_filename=js_filename)

    out = self.run_process([shared.LLVM_DWARFDUMP, wasm_filename, '-all'], stdout=PIPE).stdout

    # parse the sections
    sections = {}
    curr_section_name = ''
    curr_section_body = ''

    def add_section():
      if curr_section_name:
        sections[curr_section_name] = curr_section_body

    for line in out.splitlines():
      if ' contents:' in line:
        # a new section, a line like ".debug_str contents:"
        add_section()
        curr_section_name = line.split(' ')[0]
        curr_section_body = ''
      else:
        # possibly a line in a section
        if curr_section_name:
          curr_section_body += line + '\n'
    add_section()

    # make sure the right sections exist
    self.assertIn('.debug_abbrev', sections)
    self.assertIn('.debug_info', sections)
    self.assertIn('.debug_line', sections)
    self.assertIn('.debug_str', sections)
    self.assertIn('.debug_ranges', sections)

    # verify some content in the sections
    self.assertIn('"test_dwarf.c"', sections['.debug_info'])
    # the line section looks like this:
    # Address            Line   Column File   ISA Discriminator Flags
    # ------------------ ------ ------ ------ --- ------------- -------------
    # 0x000000000000000b      5      0      3   0             0  is_stmt
    src_to_addr = {}
    found_dwarf_c = False
    for line in sections['.debug_line'].splitlines():
      if 'name: "test_dwarf.c"' in line:
        found_dwarf_c = True
      if not found_dwarf_c:
        continue
      if 'debug_line' in line:
        break
      if line.startswith('0x'):
        while '  ' in line:
          line = line.replace('  ', ' ')
        addr, line, col = line.split(' ')[:3]
        key = (int(line), int(col))
        src_to_addr.setdefault(key, []).append(addr)

    # each of the calls must remain in the binary, and be mapped
    self.assertIn((6, 3), src_to_addr)
    self.assertIn((7, 3), src_to_addr)
    self.assertIn((8, 3), src_to_addr)

    def get_dwarf_addr(line, col):
      addrs = src_to_addr[(line, col)]
      # we assume the simple calls have one address
      self.assertEqual(len(addrs), 1)
      return int(addrs[0], 0)

    # the lines must appear in sequence (as calls to JS, the optimizer cannot
    # reorder them)
    self.assertLess(get_dwarf_addr(6, 3), get_dwarf_addr(7, 3))
    self.assertLess(get_dwarf_addr(7, 3), get_dwarf_addr(8, 3))

    # Get the wat, printing with -g which has binary offsets
    wat = self.run_process([Path(building.get_binaryen_bin(), 'wasm-opt'),
                           wasm_filename, '-g', '--print'], stdout=PIPE).stdout

    # We expect to see a pattern like this in optimized builds (there isn't
    # much that can change with such calls to JS (they can't be reordered or
    # anything else):
    #
    #   ;; code offset: 0x?
    #   (drop
    #    ;; code offset: 0x?
    #    (call $out_to_js
    #     ;; code offset: 0x?
    #     (local.get ?) or (i32.const ?)
    #    )
    #   )
    #
    # In the stacky stream of instructions form, it is
    #
    #   local.get or i32.const
    #   call $out_to_js
    #   drop
    #
    # However, in an unoptimized build the constant may be assigned earlier in
    # some other manner, so stop here.
    if not self.is_optimizing():
      return

    # get_wat_addr gets the address of one of the 3 interesting calls, by its
    # index (0,1,2).
    def get_wat_addr(call_index):
      # find the call_index-th call
      call_loc = -1
      for _ in range(call_index + 1):
        call_loc = wat.find('call $out_to_js', call_loc + 1)
        assert call_loc > 0
      # the call begins with the local.get/i32.const printed below it, which is
      # the first instruction in the stream, so it has the lowest address
      start_addr_loc = wat.find('0x', call_loc)
      assert start_addr_loc > 0
      start_addr_loc_end = wat.find('\n', start_addr_loc)
      start_addr = int(wat[start_addr_loc:start_addr_loc_end], 0)
      # the call ends with the drop, which is the last in the stream, at the
      # highest address
      end_addr_loc = wat.rfind('drop', 0, call_loc)
      assert end_addr_loc > 0
      end_addr_loc = wat.rfind('0x', 0, end_addr_loc)
      assert end_addr_loc > 0
      end_addr_loc_end = wat.find('\n', end_addr_loc)
      assert end_addr_loc_end > 0
      end_addr = int(wat[end_addr_loc:end_addr_loc_end], 0)
      return (start_addr, end_addr)

    # match up the DWARF and the wat
    for i in range(3):
      dwarf_addr = get_dwarf_addr(6 + i, 3)
      start_wat_addr, end_wat_addr = get_wat_addr(i)
      # the dwarf may match any of the 3 instructions that form the stream of
      # of instructions implementing the call in the source code, in theory
      self.assertLessEqual(start_wat_addr, dwarf_addr)
      self.assertLessEqual(dwarf_addr, end_wat_addr)

  def test_modularize_closure_pre(self):
    # test that the combination of modularize + closure + pre-js works. in that mode,
    # closure should not minify the Module object in a way that the pre-js cannot use it.
    create_file('post.js', 'var TheModule = Module();\n')
    if not self.is_wasm():
      # TODO(sbc): Fix closure warnings with MODULARIZE + WASM=0
      self.ldflags.append('-Wno-error=closure')

    self.emcc_args += [
      '--pre-js', test_file('core/modularize_closure_pre.js'),
      '--extern-post-js=post.js',
      '--closure=1',
      '-g1',
      '-sMODULARIZE',
    ]
    self.do_core_test('modularize_closure_pre.c')

  @no_wasm2js('symbol names look different wasm2js backtraces')
  @also_with_wasm_bigint
  def test_emscripten_log(self):
    self.set_setting('DEMANGLE_SUPPORT')
    if '-g' not in self.emcc_args:
      self.emcc_args.append('-g')
    self.emcc_args += ['-DRUN_FROM_JS_SHELL']
    self.do_run_in_out_file_test('emscripten_log/emscripten_log.cpp', interleaved_output=False)
    # test closure compiler as well
    if self.maybe_closure():
      self.emcc_args += ['-g1'] # extra testing
      self.do_run_in_out_file_test('emscripten_log/emscripten_log_with_closure.cpp', interleaved_output=False)

  def test_float_literals(self):
    self.do_run_in_out_file_test('test_float_literals.cpp')

  def test_exit_status(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    create_file('exit.c', r'''
      #include <stdio.h>
      #include <assert.h>
      #include <stdlib.h>
      #include <unistd.h>

      static void cleanup() {
        #ifndef NORMAL_EXIT
        assert(0 && "cleanup should only be called from normal exit()");
        #endif
        printf("cleanup\n");
      }

      int main() {
        atexit(cleanup); // this atexit should still be called
        printf("hello, world!\n");
        // Unusual exit status to make sure it's working!
        #ifdef CAPITAL_EXIT
          _Exit(118);
        #elif defined(UNDER_EXIT)
          _exit(118);
        #elif defined(NORMAL_EXIT)
          exit(118);
        #endif
      }
    ''')
    create_file('pre.js', '''
      Module.onExit = () => out('I see exit status: ' + EXITSTATUS);
    ''')
    self.emcc_args += ['--pre-js', 'pre.js']
    print('.. exit')
    self.do_runf('exit.c', 'hello, world!\ncleanup\nI see exit status: 118', assert_returncode=118, emcc_args=['-DNORMAL_EXIT'])
    print('.. _exit')
    self.do_runf('exit.c', 'hello, world!\nI see exit status: 118', assert_returncode=118, emcc_args=['-DUNDER_EXIT'])
    print('.. _Exit')
    self.do_runf('exit.c', 'hello, world!\nI see exit status: 118', assert_returncode=118, emcc_args=['-DCAPITAL_EXIT'])

  def test_noexitruntime(self):
    src = r'''
      #include <emscripten.h>
      #include <stdio.h>
      static int testPre = TEST_PRE;
      struct Global {
        Global() {
          printf("in Global()\n");
          if (testPre) { EM_ASM(noExitRuntime = true;); }
        }
        ~Global() { printf("ERROR: in ~Global()\n"); }
      } global;
      int main() {
        if (!testPre) { EM_ASM(noExitRuntime = true;); }
        printf("in main()\n");
      }
    '''
    self.do_run(src.replace('TEST_PRE', '0'), 'in Global()\nin main()')
    self.do_run(src.replace('TEST_PRE', '1'), 'in Global()\nin main()')

  def test_minmax(self):
    self.do_runf(test_file('test_minmax.c'), 'NAN != NAN\nSuccess!')

  def test_localeconv(self):
    self.do_run_in_out_file_test('core/test_localeconv.c')

  def test_newlocale(self):
    self.do_run_in_out_file_test('core/test_newlocale.c')

  def test_setlocale(self):
    self.do_run_in_out_file_test('core/test_setlocale.c')

  def test_vswprintf_utf8(self):
    self.do_core_test('test_vswprintf_utf8.c')

  # Test that a main with arguments is automatically asyncified.
  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_async_main(self):
    create_file('main.c',  r'''
#include <stdio.h>
#include <emscripten.h>
int main(int argc, char **argv) {
  emscripten_sleep(1);
  printf("argc=%d argv=%s\n", argc, argv[1]);
}
''')

    self.do_runf('main.c', 'argc=2 argv=hello', args=['hello'])

  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_async_hello(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')

    create_file('main.c',  r'''
#include <stdio.h>
#include <emscripten.h>
void f(void *p) {
  *(int*)p = 99;
  printf("!");
}
int main() {
  int i = 0;
  printf("Hello");
  emscripten_async_call(f, &i, 1);
  printf("World");
  emscripten_sleep(100);
  printf("%d\n", i);
}
''')

    self.do_runf('main.c', 'HelloWorld!99')

  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_async_loop(self):
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')

    create_file('main.c',  r'''
#include <stdio.h>
#include <emscripten.h>
int main() {
  for (int i = 0; i < 5; i++) {
    emscripten_sleep(1);
    printf("hello %d\n", i);
  }
}
''')

    self.do_runf('main.c', 'hello 0\nhello 1\nhello 2\nhello 3\nhello 4\n')

  @requires_v8
  @no_wasm64('TODO: asyncify for wasm64')
  def test_async_hello_v8(self):
    self.test_async_hello()

  @no_wasm64('TODO: asyncify for wasm64')
  def test_async_ccall_bad(self):
    # check bad ccall use
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$ccall'])
    self.set_setting('ASYNCIFY')
    self.set_setting('ASSERTIONS')
    self.set_setting('INVOKE_RUN', 0)
    create_file('main.c', r'''
#include <stdio.h>
#include <emscripten.h>
int main() {
  printf("Hello");
  emscripten_sleep(100);
  printf("World\n");
}
''')
    create_file('pre.js', '''
Module.onRuntimeInitialized = () => {
  try {
    ccall('main', 'number', ['number', 'string'], [2, 'waka']);
    var never = true;
  } catch(e) {
    out(e);
    assert(!never);
  }
};
''')
    self.emcc_args += ['--pre-js', 'pre.js']
    self.do_runf('main.c', 'The call to main is running asynchronously.')

  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_async_ccall_good(self):
    # check reasonable ccall use
    # needs to flush stdio streams
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('ASYNCIFY')
    self.set_setting('ASSERTIONS')
    self.set_setting('INVOKE_RUN', 0)
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$ccall'])
    create_file('main.c', r'''
#include <stdio.h>
#include <emscripten.h>
int main() {
  printf("Hello");
  emscripten_sleep(100);
  printf("World\n");
}
''')
    create_file('pre.js', '''
Module.onRuntimeInitialized = () => {
  ccall('main', null, ['number', 'string'], [2, 'waka'], { async: true });
};
''')
    self.emcc_args += ['--pre-js', 'pre.js']
    self.do_runf('main.c', 'HelloWorld')

  @parameterized({
    '': (False,),
    'exit_runtime': (True,),
  })
  @no_wasm64('TODO: asyncify for wasm64')
  def test_async_ccall_promise(self, exit_runtime):
    self.set_setting('ASYNCIFY')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('ASSERTIONS')
    self.set_setting('INVOKE_RUN', 0)
    self.set_setting('EXIT_RUNTIME', exit_runtime)
    self.set_setting('EXPORTED_FUNCTIONS', ['_stringf', '_floatf'])
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$maybeExit', '$ccall'])
    create_file('main.c', r'''
#include <stdio.h>
#include <emscripten.h>
const char* stringf(char* param) {
  emscripten_sleep(20);
  printf("stringf: %s", param);
  return "second";
}
double floatf() {
  emscripten_sleep(20);
  emscripten_sleep(20);
  return 6.4;
}
''')
    create_file('pre.js', r'''
Module.onRuntimeInitialized = () => {
  runtimeKeepalivePush();
  ccall('stringf', 'string', ['string'], ['first\n'], { async: true })
    .then(function(val) {
      console.log(val);
      ccall('floatf', 'number', null, null, { async: true }).then(function(arg) {
        console.log(arg);
        runtimeKeepalivePop();
        maybeExit();
      });
    });
};
''')
    self.emcc_args += ['--pre-js', 'pre.js']
    self.do_runf('main.c', 'stringf: first\nsecond\n6.4')

  @no_wasm64('TODO: asyncify for wasm64')
  def test_fibers_asyncify(self):
    self.set_setting('ASYNCIFY')
    self.maybe_closure()
    self.do_runf(test_file('test_fibers.cpp'), '*leaf-0-100-1-101-1-102-2-103-3-104-5-105-8-106-13-107-21-108-34-109-*')

  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_asyncify_unused(self):
    # test a program not using asyncify, but the pref is set
    self.do_core_test('test_hello_world.c')

  @parameterized({
    'normal': ([], True),
    'removelist_a': (['-sASYNCIFY_REMOVE=["foo(int, double)"]'], False),
    'removelist_b': (['-sASYNCIFY_REMOVE=["bar()"]'], True),
    'removelist_c': (['-sASYNCIFY_REMOVE=["baz()"]'], False),
    'onlylist_a': (['-sASYNCIFY_ONLY=["main","__original_main","foo(int, double)","baz()","c_baz","Structy::funcy()","bar()"]'], True),
    'onlylist_b': (['-sASYNCIFY_ONLY=["main","__original_main","foo(int, double)","baz()","c_baz","Structy::funcy()"]'], True),
    'onlylist_c': (['-sASYNCIFY_ONLY=["main","__original_main","foo(int, double)","baz()","c_baz"]'], False),
    'onlylist_d': (['-sASYNCIFY_ONLY=["foo(int, double)","baz()","c_baz","Structy::funcy()"]'], False),
    'onlylist_b_response': ([], True,  '["main","__original_main","foo(int, double)","baz()","c_baz","Structy::funcy()"]'),
    'onlylist_c_response': ([], False, '["main","__original_main","foo(int, double)","baz()","c_baz"]'),
  })
  @no_wasm64('TODO: asyncify for wasm64')
  def test_asyncify_lists(self, args, should_pass, response=None):
    if response is not None:
      create_file('response.file', response)
      self.set_setting('ASYNCIFY_ONLY', '@response.file')
    self.set_setting('ASYNCIFY')
    self.emcc_args += args

    if should_pass:
      self.do_core_test('test_asyncify_lists.cpp', assert_identical=True)
    else:
       self.do_runf(test_file('core/test_asyncify_lists.cpp'), ('RuntimeError', 'Thrown at'), assert_returncode=NON_ZERO)

    # use of ASYNCIFY_* options may require intermediate debug info. that should
    # not end up emitted in the final binary
    if self.is_wasm():
      filename = 'test_asyncify_lists.wasm'
      # there should be no name section. sanitizers, however, always enable that
      if not is_sanitizing(self.emcc_args) and '--profiling-funcs' not in self.emcc_args:
        with webassembly.Module(filename) as m:
          self.assertFalse(m.has_name_section())
      # in a fully-optimized build, imports and exports are minified too and we
      # can verify that our function names appear nowhere
      if '-O3' in self.emcc_args:
        binary = read_binary(filename)
        self.assertFalse(b'main' in binary)

  @parameterized({
    'normal': ([], True),
    'ignoreindirect': (['-sASYNCIFY_IGNORE_INDIRECT'], False),
    'add': (['-sASYNCIFY_IGNORE_INDIRECT', '-sASYNCIFY_ADD=["__original_main","main","virt()"]'], True),
  })
  @no_wasm64('TODO: asyncify for wasm64')
  def test_asyncify_indirect_lists(self, args, should_pass):
    self.set_setting('ASYNCIFY')
    self.emcc_args += args
    try:
      self.do_core_test('test_asyncify_indirect_lists.cpp', assert_identical=True)
      if not should_pass:
        should_pass = True
        raise Exception('should not have passed')
    except Exception:
      if should_pass:
        raise

  @needs_dylink
  @no_wasm64('TODO: asyncify for wasm64')
  def test_asyncify_side_module(self):
    self.set_setting('ASYNCIFY')
    self.set_setting('ASYNCIFY_IMPORTS', ['my_sleep'])
    self.dylink_test(r'''
      #include <stdio.h>
      #include "header.h"

      int main() {
        printf("before sleep\n");
        my_sleep(1);
        printf("after sleep\n");
        return 0;
      }
    ''', r'''
      #include <stdio.h>
      #include <emscripten.h>
      #include "header.h"

      void my_sleep(int milli_seconds) {
        // put variable onto stack
        volatile int value = 42;
        printf("%d\n", value);
        emscripten_sleep(milli_seconds);
        // variable on stack in side module function should be restored.
        printf("%d\n", value);
      }
    ''', 'before sleep\n42\n42\nafter sleep\n', header='void my_sleep(int);', force_c=True)

  @no_asan('asyncify stack operations confuse asan')
  @no_wasm64('TODO: asyncify for wasm64')
  def test_emscripten_scan_registers(self):
    self.set_setting('ASYNCIFY')
    self.do_core_test('test_emscripten_scan_registers.cpp')

  @no_wasm64('TODO: asyncify for wasm64')
  def test_asyncify_assertions(self):
    self.set_setting('ASYNCIFY')
    self.set_setting('ASYNCIFY_IMPORTS', ['suspend'])
    self.set_setting('ASSERTIONS')
    self.do_core_test('test_asyncify_assertions.c', assert_returncode=NON_ZERO)

  @no_lsan('leaks asyncify stack during exit')
  @no_asan('leaks asyncify stack during exit')
  @no_wasm64('TODO: asyncify for wasm64')
  def test_asyncify_during_exit(self):
    self.set_setting('ASYNCIFY')
    self.set_setting('ASSERTIONS')
    self.set_setting('EXIT_RUNTIME', 1)
    self.do_core_test('test_asyncify_during_exit.cpp', assert_returncode=NON_ZERO)
    print('NO_ASYNC')
    self.do_core_test('test_asyncify_during_exit.cpp', emcc_args=['-DNO_ASYNC'], out_suffix='_no_async')

  @no_asan('asyncify stack operations confuse asan')
  @no_lsan('undefined symbol __global_base')
  @no_wasm2js('dynamic linking support in wasm2js')
  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_asyncify_main_module(self):
    self.set_setting('MAIN_MODULE', 2)
    self.do_core_test('test_hello_world.c')

  # Test that pthread_join works correctly with asyncify.
  @requires_node_canary
  def test_pthread_join_and_asyncify(self):
    # TODO Test with ASYNCIFY=1 https://github.com/emscripten-core/emscripten/issues/17552
    self.require_jspi()
    self.do_runf(test_file('core/test_pthread_join_and_asyncify.c'), 'joining thread!\njoined thread!',
                 emcc_args=['-sASYNCIFY=2',
                            '-sASYNCIFY_EXPORTS=run_thread',
                            '-sEXIT_RUNTIME=1',
                            '-pthread', '-sPROXY_TO_PTHREAD',
                            '-Wno-experimental'])

  @no_asan('asyncify stack operations confuse asan')
  @no_wasm64('TODO: asyncify for wasm64')
  @no_wasm2js('TODO: lazy loading in wasm2js')
  @parameterized({
    'conditional': (True,),
    'unconditional': (False,),
  })
  def test_emscripten_lazy_load_code(self, conditional):
    if self.get_setting('STACK_OVERFLOW_CHECK'):
      self.skipTest('https://github.com/emscripten-core/emscripten/issues/16828')
    self.set_setting('ASYNCIFY_LAZY_LOAD_CODE')
    self.set_setting('ASYNCIFY_IGNORE_INDIRECT')
    self.set_setting('MALLOC', 'emmalloc')
    self.emcc_args += ['--profiling-funcs'] # so that we can find the functions for the changes below
    if conditional:
      self.emcc_args += ['-DCONDITIONAL']
    self.do_core_test('emscripten_lazy_load_code.cpp', args=['0'])

    first_size = os.path.getsize('emscripten_lazy_load_code.wasm')
    second_size = os.path.getsize('emscripten_lazy_load_code.wasm.lazy.wasm')
    print('first wasm size', first_size)
    print('second wasm size', second_size)

    # For the purposes of this test we don't consider O1 to be optimizing
    is_optimizing = self.is_optimizing() and '-O1' not in self.emcc_args

    if not conditional and is_optimizing and \
       '-g' not in self.emcc_args and \
       '-fsanitize=leak' not in self.emcc_args and \
       not self.get_setting('WASMFS'):
      # TODO: WasmFS has not yet been optimized for code size, and the general
      #       increase it causes mixes up code size measurements like this.
      #       See https://github.com/emscripten-core/emscripten/issues/16005
      # If the call to lazy-load is unconditional, then the optimizer can dce
      # out more than half
      self.assertLess(first_size, 0.6 * second_size)

    wasm1 = read_binary('emscripten_lazy_load_code.wasm')
    wasm2 = read_binary('emscripten_lazy_load_code.wasm.lazy.wasm')
    self.assertNotEqual(wasm1, wasm2)

    # attempts to "break" the wasm by adding an unreachable in $foo_end. returns whether we found it.
    def break_wasm(name):
      wat = self.get_wasm_text(name)
      lines = wat.splitlines()
      wat = None
      for i in range(len(lines)):
        if '(func $foo_end ' in lines[i]:
          j = i + 1
          while '(local ' in lines[j]:
            j += 1
          # we found the first line after the local defs
          lines[j] = '(unreachable)' + lines[j]
          wat = '\n'.join(lines)
          break
      if wat is None:
        # $foo_end is not present in the wasm, nothing to break
        shutil.copyfile(name, name + '.orig')
        return False
      create_file('wat.wat', wat)
      shutil.move(name, name + '.orig')
      self.run_process([Path(building.get_binaryen_bin(), 'wasm-as'), 'wat.wat', '-o', name, '-g'])
      return True

    def verify_working(args):
      self.assertContained('foo_end\n', self.run_js('emscripten_lazy_load_code.js', args=args))

    def verify_broken(args):
      self.assertNotContained('foo_end\n', self.run_js('emscripten_lazy_load_code.js', args=args, assert_returncode=NON_ZERO))

    # the first-loaded wasm will not reach the second call, since we call it after lazy-loading.
    # verify that by changing the first wasm to throw in that function
    found_foo_end = break_wasm('emscripten_lazy_load_code.wasm')
    if not conditional and is_optimizing:
      self.assertFalse(found_foo_end, 'should have optimized out $foo_end')
    verify_working(['0'])
    # but breaking the second wasm actually breaks us
    if not break_wasm('emscripten_lazy_load_code.wasm.lazy.wasm'):
      raise Exception('could not break lazy wasm - missing expected code')
    verify_broken(['0'])

    # restore
    shutil.copyfile('emscripten_lazy_load_code.wasm.orig', 'emscripten_lazy_load_code.wasm')
    shutil.copyfile('emscripten_lazy_load_code.wasm.lazy.wasm.orig', 'emscripten_lazy_load_code.wasm.lazy.wasm')
    verify_working(['0'])

    if conditional:
      # if we do not call the lazy load function, then we do not need the lazy wasm,
      # and we do the second call in the first wasm
      os.remove('emscripten_lazy_load_code.wasm.lazy.wasm')
      verify_broken(['0'])
      verify_working(['42'])
      break_wasm('emscripten_lazy_load_code.wasm')
      verify_broken(['0'])

  # Test basic wasm2js functionality in all core compilation modes.
  @no_sanitize('no wasm2js support yet in sanitizers')
  @no_wasm64('no wasm2js support yet with wasm64')
  def test_wasm2js(self):
    if not self.is_wasm():
      self.skipTest('redundant to test wasm2js in wasm2js* mode')
    self.set_setting('WASM', 0)
    self.do_core_test('test_hello_world.c')
    # a mem init file is emitted just like with JS
    expect_memory_init_file = self.uses_memory_init_file()
    if expect_memory_init_file:
      self.assertExists('test_hello_world.js.mem')
      mem = read_binary('test_hello_world.js.mem')
      self.assertTrue(mem[-1] != b'\0')
    else:
      self.assertNotExists('test_hello_world.js.mem')

  @no_sanitize('no wasm2js support yet in sanitizers')
  @no_wasm64('no wasm2js support yet with wasm64')
  def test_maybe_wasm2js(self):
    if not self.is_wasm():
      self.skipTest('redundant to test wasm2js in wasm2js* mode')
    self.set_setting('MAYBE_WASM2JS')
    # see that running as wasm works
    self.do_core_test('test_hello_world.c')
    # run wasm2js, bundle the code, and use the wasm2js path
    cmd = [PYTHON, path_from_root('tools/maybe_wasm2js.py'), 'test_hello_world.js', 'test_hello_world.wasm']
    if self.is_optimizing():
      cmd += ['-O2']
    self.run_process(cmd, stdout=open('do_wasm2js.js', 'w')).stdout
    # remove the wasm to make sure we never use it again
    os.remove('test_hello_world.wasm')
    # verify that it runs
    self.assertContained('hello, world!', self.run_js('do_wasm2js.js'))

  @no_asan('no wasm2js support yet in asan')
  @no_wasm64('no wasm2js support yet with wasm64')
  @parameterized({
    '': ([],),
    'minimal_runtime': (['-sMINIMAL_RUNTIME'],),
  })
  def test_wasm2js_fallback(self, args):
    if not self.is_wasm():
      self.skipTest('redundant to test wasm2js in wasm2js* mode')

    cmd = [EMCC, test_file('small_hello_world.c'), '-sWASM=2'] + args
    self.run_process(cmd)

    # First run with WebAssembly support enabled
    # Move the Wasm2js fallback away to test it is not accidentally getting loaded.
    os.rename('a.out.wasm.js', 'a.out.wasm.js.unused')
    self.assertContained('hello!', self.run_js('a.out.js'))
    os.rename('a.out.wasm.js.unused', 'a.out.wasm.js')

    # Then disable WebAssembly support in VM, and try again.. Should still work with Wasm2JS fallback.
    create_file('b.out.js', 'WebAssembly = undefined;\n' + read_file('a.out.js'))
    os.remove('a.out.wasm') # Also delete the Wasm file to test that it is not attempted to be loaded.
    self.assertContained('hello!', self.run_js('b.out.js'))

  def test_cxx_self_assign(self):
    # See https://github.com/emscripten-core/emscripten/pull/2688 and http://llvm.org/bugs/show_bug.cgi?id=18735
    self.do_run(r'''
      #include <map>
      #include <stdio.h>

      int main() {
        std::map<int, int> m;
        m[0] = 1;
        m = m;
        // size should still be one after self assignment
        if (m.size() == 1) {
          printf("ok.\n");
        }
      }
    ''', 'ok.')

  def test_memprof_requirements(self):
    # This test checks for the global variables required to run the memory
    # profiler.  It would fail if these variables were made no longer global
    # or if their identifiers were changed.
    create_file('main.c', '''
      int check_memprof_requirements();

      int main() {
        return check_memprof_requirements();
      }
    ''')
    create_file('lib.js', '''
      mergeInto(LibraryManager.library, {
        check_memprof_requirements: () => {
          if (typeof _emscripten_stack_get_base === 'function' &&
              typeof _emscripten_stack_get_end === 'function' &&
              typeof _emscripten_stack_get_current === 'function' &&
              typeof Module['___heap_base'] === 'number') {
             out('able to run memprof');
             return 0;
           } else {
             out('missing the required variables to run memprof');
             return 1;
           }
        }
      });
    ''')
    self.emcc_args += ['--memoryprofiler', '--js-library', 'lib.js']
    self.do_runf('main.c', 'able to run memprof')

  def test_fs_dict(self):
    self.set_setting('FORCE_FILESYSTEM')
    self.emcc_args += ['-lidbfs.js']
    self.emcc_args += ['-lnodefs.js']
    create_file('pre.js', '''
      Module.preRun = () => {
        out(typeof FS.filesystems['MEMFS']);
        out(typeof FS.filesystems['IDBFS']);
        out(typeof FS.filesystems['NODEFS']);
        // Globals
        console.log(typeof MEMFS);
        console.log(typeof IDBFS);
        console.log(typeof NODEFS);
      };
    ''')
    self.emcc_args += ['--pre-js', 'pre.js']
    self.do_run('int main() { return 0; }', 'object\nobject\nobject\nobject\nobject\nobject')

  def test_fs_dict_none(self):
    # if IDBFS and NODEFS are not enabled, they are not present.
    self.set_setting('FORCE_FILESYSTEM')
    self.set_setting('ASSERTIONS')
    create_file('pre.js', '''
      Module.preRun = () => {
        out(typeof FS.filesystems['MEMFS']);
        out(typeof FS.filesystems['IDBFS']);
        out(typeof FS.filesystems['NODEFS']);
        // Globals
        console.log(typeof MEMFS);
        console.log(IDBFS);
        console.log(NODEFS);
        FS.mkdir('/working1');
        try {
          FS.mount(IDBFS, {}, '/working1');
        } catch (e) {
          console.log('|' + e + '|');
        }
      };
    ''')
    self.emcc_args += ['--pre-js', 'pre.js']
    expected = '''\
object
undefined
undefined
object
IDBFS is no longer included by default; build with -lidbfs.js
NODEFS is no longer included by default; build with -lnodefs.js
|IDBFS is no longer included by default; build with -lidbfs.js|'''
    self.do_run('int main() { return 0; }', expected)

  def test_stack_overflow_check(self):
    self.set_setting('STACK_SIZE', 1048576)
    self.set_setting('STACK_OVERFLOW_CHECK', 2)
    self.do_runf(test_file('stack_overflow.cpp'), 'Aborted(stack overflow', assert_returncode=NON_ZERO)

    self.emcc_args += ['-DONE_BIG_STRING']
    self.do_runf(test_file('stack_overflow.cpp'), 'Aborted(stack overflow', assert_returncode=NON_ZERO)

    # ASSERTIONS=2 implies STACK_OVERFLOW_CHECK=2
    self.clear_setting('STACK_OVERFLOW_CHECK')
    self.set_setting('ASSERTIONS', 2)
    self.do_runf(test_file('stack_overflow.cpp'), 'Aborted(stack overflow', assert_returncode=NON_ZERO)

  @node_pthreads
  def test_binaryen_2170_emscripten_atomic_cas_u8(self):
    self.emcc_args.append('-pthread')
    self.do_run_in_out_file_test('binaryen_2170_emscripten_atomic_cas_u8.cpp')

  @also_with_standalone_wasm()
  def test_sbrk(self):
    self.do_runf(test_file('sbrk_brk.cpp'), 'OK.')

  def test_brk(self):
    self.emcc_args += ['-DTEST_BRK=1']
    self.do_runf(test_file('sbrk_brk.cpp'), 'OK.')

  # Tests that we can use the dlmalloc mallinfo() function to obtain information
  # about malloc()ed blocks and compute how much memory is used/freed.
  @no_asan('mallinfo is not part of ASan malloc')
  @no_lsan('mallinfo is not part of LSan malloc')
  def test_mallinfo(self):
    self.do_runf(test_file('mallinfo.cpp'), 'OK.')

  @no_asan('cannot replace malloc/free with ASan')
  @no_lsan('cannot replace malloc/free with LSan')
  def test_wrap_malloc(self):
    self.do_runf(test_file('core/test_wrap_malloc.c'), 'OK.')

  def test_environment(self):
    self.set_setting('ASSERTIONS')

    def test(assert_returncode=0):
      self.do_core_test('test_hello_world.c', assert_returncode=assert_returncode)
      js = read_file('test_hello_world.js')
      assert ('require(' in js) == ('node' in self.get_setting('ENVIRONMENT')), 'we should have require() calls only if node js specified'

    for engine in config.JS_ENGINES:
      print(f'engine: {engine}')
      # set us to test in just this engine
      self.require_engine(engine)
      # tell the compiler to build with just that engine
      if engine == config.NODE_JS:
        right = 'node'
        wrong = 'shell'
      else:
        right = 'shell'
        wrong = 'node'
      # test with the right env
      self.set_setting('ENVIRONMENT', right)
      print('ENVIRONMENT =', self.get_setting('ENVIRONMENT'))
      test()
      # test with the wrong env
      self.set_setting('ENVIRONMENT', wrong)
      print('ENVIRONMENT =', self.get_setting('ENVIRONMENT'))
      try:
        test(assert_returncode=NON_ZERO)
        raise Exception('unexpected success')
      except Exception as e:
        self.assertContained('not compiled for this environment', str(e))
      # test with a combined env
      self.set_setting('ENVIRONMENT', right + ',' + wrong)
      print('ENVIRONMENT =', self.get_setting('ENVIRONMENT'))
      test()

  @requires_node
  def test_postrun_exception(self):
    # verify that an exception thrown in postRun() will not trigger the
    # compilation failed handler, and will be printed to stderr.
    self.add_post_run('ThisFunctionDoesNotExist()')
    self.build(test_file('core/test_hello_world.c'))
    output = self.run_js('test_hello_world.js', assert_returncode=NON_ZERO)
    self.assertStartswith(output, 'hello, world!')
    self.assertContained('ThisFunctionDoesNotExist is not defined', output)

  def test_postrun_exit_runtime(self):
    create_file('post.js', '''
      addOnPostRun(() => err('post run\\n'));
    ''')
    self.set_setting('EXIT_RUNTIME')
    self.emcc_args.append('--post-js=post.js')
    self.do_runf(test_file('hello_world.c'), 'post run')

  # Tests that building with -sDECLARE_ASM_MODULE_EXPORTS=0 works
  def test_no_declare_asm_module_exports(self):
    self.set_setting('DECLARE_ASM_MODULE_EXPORTS', 0)
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    self.maybe_closure()
    self.do_runf(test_file('declare_asm_module_exports.c'), 'jsFunction: 1')
    js = read_file('declare_asm_module_exports.js')
    occurances = js.count('cFunction')
    if self.is_optimizing() and '-g' not in self.emcc_args:
      # In optimized builds only the single reference cFunction that exists in the EM_ASM should exist
      if self.is_wasm():
        self.assertEqual(occurances, 1)
      else:
        # With js the asm module itself also contains a reference for the cFunction name
        self.assertEqual(occurances, 2)
    else:
      print(occurances)

  # Tests that building with -sDECLARE_ASM_MODULE_EXPORTS=0 works
  @no_wasmfs('https://github.com/emscripten-core/emscripten/issues/16816')
  @no_asan('TODO: ASan support in minimal runtime')
  def test_minimal_runtime_no_declare_asm_module_exports(self):
    self.set_setting('DECLARE_ASM_MODULE_EXPORTS', 0)
    self.set_setting('WASM_ASYNC_COMPILATION', 0)
    self.maybe_closure()
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    self.do_runf(test_file('declare_asm_module_exports.c'), 'jsFunction: 1')

  # Tests that -sMINIMAL_RUNTIME works well in different build modes
  @no_wasmfs('https://github.com/emscripten-core/emscripten/issues/16816')
  @parameterized({
    'default': ([],),
    'streaming': (['-sMINIMAL_RUNTIME_STREAMING_WASM_COMPILATION'],),
    'streaming_inst': (['-sMINIMAL_RUNTIME_STREAMING_WASM_INSTANTIATION'],),
    'no_export': (['-sDECLARE_ASM_MODULE_EXPORTS=0'],)
  })
  @requires_node  # TODO: Support for non-Node.js shells under MINIMAL_RUNTIME
  def test_minimal_runtime_hello_world(self, args):
    self.emcc_args = args
    self.set_setting('MINIMAL_RUNTIME')
    self.maybe_closure()
    self.do_runf(test_file('small_hello_world.c'), 'hello')

  # Test that printf() works in MINIMAL_RUNTIME=1
  @no_wasmfs('https://github.com/emscripten-core/emscripten/issues/16816')
  @parameterized({
    'fs': ('FORCE_FILESYSTEM',),
    'nofs': ('NO_FILESYSTEM',),
  })
  @no_asan('TODO: ASan support in minimal runtime')
  def test_minimal_runtime_hello_printf(self, extra_setting):
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    self.set_setting(extra_setting)
    # $FS is not fully compatible with MINIMAL_RUNTIME so fails with closure
    # compiler.  lsan also pulls in $FS
    if '-fsanitize=leak' not in self.emcc_args and extra_setting != 'FORCE_FILESYSTEM':
      self.maybe_closure()
    self.do_runf(test_file('hello_world.c'), 'hello, world!')

  # Tests that -sMINIMAL_RUNTIME works well with SAFE_HEAP
  @no_wasmfs('https://github.com/emscripten-core/emscripten/issues/16816')
  @no_asan('TODO: ASan support in minimal runtime')
  def test_minimal_runtime_safe_heap(self):
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    self.set_setting('SAFE_HEAP')
    # $FS is not fully compatible with MINIMAL_RUNTIME so fails with closure
    # compiler.
    # lsan pulls in $FS
    if '-fsanitize=leak' not in self.emcc_args:
      self.maybe_closure()
    self.do_runf(test_file('small_hello_world.c'), 'hello')

  # Tests global initializer with -sMINIMAL_RUNTIME
  @no_wasmfs('https://github.com/emscripten-core/emscripten/issues/16816')
  @no_asan('TODO: ASan support in minimal runtime')
  def test_minimal_runtime_global_initializer(self):
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    self.maybe_closure()
    self.do_runf(test_file('test_global_initializer.cpp'), 't1 > t0: 1')

  @no_wasm2js('wasm2js does not support PROXY_TO_PTHREAD (custom section support)')
  def test_return_address(self):
    self.set_setting('USE_OFFSET_CONVERTER')
    self.do_runf(test_file('core/test_return_address.c'), 'passed')

  @no_wasm2js('TODO: sanitizers in wasm2js')
  @no_asan('-fsanitize-minimal-runtime cannot be used with ASan')
  @no_lsan('-fsanitize-minimal-runtime cannot be used with LSan')
  def test_ubsan_minimal_too_many_errors(self):
    self.emcc_args += ['-fsanitize=undefined', '-fsanitize-minimal-runtime']
    if not self.is_wasm():
      if self.is_optimizing():
        self.skipTest('test can only be run without optimizations on asm.js')
      # Need to use `-g` to get proper line numbers in asm.js
      self.emcc_args += ['-g']
    self.do_runf(test_file('core/test_ubsan_minimal_too_many_errors.c'),
                 expected_output='ubsan: add-overflow by 0x[0-9a-f]*\n' * 20 + 'ubsan: too many errors\n',
                 regex=True)

  @no_wasm2js('TODO: sanitizers in wasm2js')
  @no_asan('-fsanitize-minimal-runtime cannot be used with ASan')
  @no_lsan('-fsanitize-minimal-runtime cannot be used with LSan')
  def test_ubsan_minimal_errors_same_place(self):
    self.emcc_args += ['-fsanitize=undefined', '-fsanitize-minimal-runtime']
    if not self.is_wasm():
      if self.is_optimizing():
        self.skipTest('test can only be run without optimizations on asm.js')
      # Need to use `-g` to get proper line numbers in asm.js
      self.emcc_args += ['-g']
    self.do_runf(test_file('core/test_ubsan_minimal_errors_same_place.c'),
                 expected_output='ubsan: add-overflow by 0x[0-9a-z]*\n' * 5,
                 regex=True)

  @parameterized({
    'fsanitize_undefined': (['-fsanitize=undefined'],),
    'fsanitize_integer': (['-fsanitize=integer'],),
    'fsanitize_overflow': (['-fsanitize=signed-integer-overflow'],),
  })
  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_full_overflow(self, args):
    self.emcc_args += args
    self.do_runf(test_file('core/test_ubsan_full_overflow.c'),
                 assert_all=True, expected_output=[
      ".c:3:5: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'",
      ".c:7:7: runtime error: signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'",
    ])

  @parameterized({
    'fsanitize_undefined': (['-fsanitize=undefined'],),
    'fsanitize_return': (['-fsanitize=return'],),
  })
  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_full_no_return(self, args):
    self.emcc_args += ['-Wno-return-type'] + args
    self.do_runf(test_file('core/test_ubsan_full_no_return.cpp'),
                 expected_output='.cpp:1:5: runtime error: execution reached the end of a value-returning function without returning a value', assert_returncode=NON_ZERO)

  @parameterized({
    'fsanitize_undefined': (['-fsanitize=undefined'],),
    'fsanitize_integer': (['-fsanitize=integer'],),
    'fsanitize_shift': (['-fsanitize=shift'],),
  })
  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_full_left_shift(self, args):
    self.emcc_args += args
    self.do_runf(test_file('core/test_ubsan_full_left_shift.c'),
                 assert_all=True, expected_output=[
      '.c:3:5: runtime error: left shift of negative value -1',
      ".c:7:5: runtime error: left shift of 16 by 29 places cannot be represented in type 'int'"
    ])

  @parameterized({
    'fsanitize_undefined': (['-fsanitize=undefined'],),
    'fsanitize_null': (['-fsanitize=null'],),
    'dylink': (['-fsanitize=null', '-sMAIN_MODULE=2'],),
  })
  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_full_null_ref(self, args):
    if is_sanitizing(self.emcc_args):
      self.skipTest('test is specific to null sanitizer')
    self.emcc_args += args
    self.do_runf(test_file('core/test_ubsan_full_null_ref.cpp'),
                 assert_all=True, expected_output=[
      ".cpp:3:12: runtime error: reference binding to null pointer of type 'int'",
      ".cpp:4:13: runtime error: reference binding to null pointer of type 'int'",
      ".cpp:5:14: runtime error: reference binding to null pointer of type 'int'",
    ])

  @parameterized({
    'fsanitize_undefined': (['-fsanitize=undefined'],),
    'fsanitize_vptr': (['-fsanitize=vptr'],),
  })
  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_full_static_cast(self, args):
    self.emcc_args += args
    self.do_runf(test_file('core/test_ubsan_full_static_cast.cpp'),
                 assert_all=True, expected_output=[
      ".cpp:18:10: runtime error: downcast of address",
      "which does not point to an object of type 'R'",
    ])

  @parameterized({
    'g': ('-g', [
      ".cpp:3:12: runtime error: reference binding to null pointer of type 'int'",
      'in main',
    ]),
    'g4': ('-gsource-map', [
      ".cpp:3:12: runtime error: reference binding to null pointer of type 'int'",
      'in main ',
      '.cpp:3:8'
    ]),
  })
  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_full_stack_trace(self, g_flag, expected_output):
    if g_flag == '-gsource-map':
      if not self.is_wasm():
        self.skipTest('wasm2js has no source map support')
      elif self.get_setting('EVAL_CTORS'):
        self.skipTest('EVAL_CTORS does not support source maps')

    create_file('pre.js', 'Module.UBSAN_OPTIONS = "print_stacktrace=1";')
    self.emcc_args += ['-fsanitize=null', g_flag, '--pre-js=pre.js']
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.do_runf(test_file('core/test_ubsan_full_null_ref.cpp'),
                 assert_all=True, expected_output=expected_output)

  @no_wasm2js('TODO: sanitizers in wasm2js')
  def test_ubsan_typeinfo_eq(self):
    # https://github.com/emscripten-core/emscripten/issues/13330
    src = r'''
      #include <typeinfo>
      #include <stdio.h>
      int main() {
        int mismatch = typeid(int) != typeid(int);
        printf("ok\n");
        return mismatch;
      }
      '''
    self.emcc_args.append('-fsanitize=undefined')
    self.do_run(src, 'ok\n')

  def test_template_class_deduction(self):
    self.emcc_args += ['-std=c++17']
    self.do_core_test('test_template_class_deduction.cpp')

  @no_wasm2js('TODO: ASAN in wasm2js')
  @no_safe_heap('asan does not work with SAFE_HEAP')
  @no_wasm64('TODO: ASAN in memory64')
  @parameterized({
    'c': ['test_asan_no_error.c'],
    'cpp': ['test_asan_no_error.cpp'],
  })
  def test_asan_no_error(self, name):
    self.emcc_args.append('-fsanitize=address')
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.set_setting('INITIAL_MEMORY', '300mb')
    self.do_runf(test_file('core', name), '', assert_returncode=NON_ZERO)

  # note: these tests have things like -fno-builtin-memset in order to avoid
  # clang optimizing things away. for example, a memset might be optimized into
  # stores, and then the stores identified as dead, which leaves nothing for
  # asan to test. here we want to test asan itself, so we work around that.
  @no_safe_heap('asan does not work with SAFE_HEAP')
  @no_wasm64('TODO: ASAN in memory64')
  @parameterized({
    'use_after_free_c': ('test_asan_use_after_free.c', [
      'AddressSanitizer: heap-use-after-free on address',
    ]),
    'use_after_free_cpp': ('test_asan_use_after_free.cpp', [
      'AddressSanitizer: heap-use-after-free on address',
    ]),
    'use_after_return': ('test_asan_use_after_return.c', [
      'AddressSanitizer: stack-use-after-return on address',
    ], ['-Wno-return-stack-address']),
    'static_buffer_overflow': ('test_asan_static_buffer_overflow.c', [
      'AddressSanitizer: global-buffer-overflow on address',
    ], ['-fno-builtin-memset']),
    'heap_buffer_overflow_c': ('test_asan_heap_buffer_overflow.c', [
      'AddressSanitizer: heap-buffer-overflow on address',
    ], ['-fno-builtin-memset']),
    'heap_buffer_overflow_cpp': ('test_asan_heap_buffer_overflow.cpp', [
      'AddressSanitizer: heap-buffer-overflow on address',
    ], ['-fno-builtin-memset']),
    'stack_buffer_overflow': ('test_asan_stack_buffer_overflow.c', [
      'AddressSanitizer: stack-buffer-overflow'
    ], ['-fno-builtin-memset']),
    'stack_buffer_overflow_js': ('test_asan_stack_buffer_overflow_js.c', [
      'AddressSanitizer: stack-buffer-overflow'
    ], ['-fno-builtin-memset']),
    'bitfield_unround_size': ('test_asan_bitfield_unround_size.c', [
      'AddressSanitizer: stack-buffer-overflow'
    ], ['-fno-builtin-memset']),
    'bitfield_unround_offset': ('test_asan_bitfield_unround_offset.c', [
      'AddressSanitizer: stack-buffer-overflow'
    ], ['-fno-builtin-memset']),
    'bitfield_round': ('test_asan_bitfield_round.c', [
      'AddressSanitizer: stack-buffer-overflow'
    ], ['-fno-builtin-memset']),
    'memset_null': ('test_asan_memset_null.c', [
      'AddressSanitizer: null-pointer-dereference on address 0x00000001'
    ], ['-fno-builtin-memset']),
    'memset_freed': ('test_asan_memset_freed.c', [
      'AddressSanitizer: heap-use-after-free on address'
    ], ['-fno-builtin-memset']),
    'strcpy': ('test_asan_strcpy.c', [
      'AddressSanitizer: heap-buffer-overflow on address'
    ], ['-fno-builtin-strcpy']),
    'memcpy': ('test_asan_memcpy.c', [
      'AddressSanitizer: heap-buffer-overflow on address'
    ], ['-fno-builtin-memcpy']),
    'memchr': ('test_asan_memchr.c', [
      'AddressSanitizer: global-buffer-overflow on address'
    ], ['-fno-builtin-memchr']),
    'vector': ('test_asan_vector.cpp', [
      'AddressSanitizer: container-overflow on address'
    ]),
  })
  def test_asan(self, name, expected_output, cflags=None):
    if '-Oz' in self.emcc_args:
      self.skipTest('-Oz breaks source maps')

    if not self.is_wasm():
      self.skipTest('wasm2js has no ASan support')

    self.emcc_args.append('-fsanitize=address')
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.set_setting('INITIAL_MEMORY', '300mb')
    if cflags:
      self.emcc_args += cflags
    self.do_runf(test_file('core', name),
                 expected_output=expected_output, assert_all=True,
                 check_for_error=False, assert_returncode=NON_ZERO)

  @no_safe_heap('asan does not work with SAFE_HEAP')
  @no_wasm2js('TODO: ASAN in wasm2js')
  @no_wasm64('TODO: ASAN in memory64')
  def test_asan_js_stack_op(self):
    self.emcc_args.append('-fsanitize=address')
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.set_setting('INITIAL_MEMORY', '300mb')
    self.do_runf(test_file('core/test_asan_js_stack_op.c'),
                 expected_output='Hello, World!')

  @no_safe_heap('asan does not work with SAFE_HEAP')
  @no_wasm2js('TODO: ASAN in wasm2js')
  @no_wasm64('TODO: ASAN in memory64')
  def test_asan_api(self):
    self.emcc_args.append('-fsanitize=address')
    self.set_setting('INITIAL_MEMORY', '300mb')
    self.do_core_test('test_asan_api.c')

  @no_safe_heap('asan does not work with SAFE_HEAP')
  @no_wasm2js('TODO: ASAN in wasm2js')
  @no_wasm64('TODO: ASAN in memory64')
  def test_asan_modularized_with_closure(self):
    # the bug is that createModule() returns undefined, instead of the
    # proper Promise object.
    create_file('post.js', 'if (!(createModule() instanceof Promise)) throw `Promise was not returned (${typeof createModule()})`;\n')
    self.emcc_args += ['-fsanitize=address', '--extern-post-js=post.js']
    self.set_setting('MODULARIZE')
    self.set_setting('EXPORT_NAME', 'createModule')
    self.set_setting('USE_CLOSURE_COMPILER')
    self.set_setting('ALLOW_MEMORY_GROWTH')
    self.set_setting('INITIAL_MEMORY', '300mb')
    self.do_runf(test_file('hello_world.c'), expected_output='hello, world!')

  @no_asan('SAFE_HEAP cannot be used with ASan')
  def test_safe_heap_user_js(self):
    self.set_setting('SAFE_HEAP')
    self.do_runf(test_file('core/test_safe_heap_user_js.c'),
                 expected_output=['Aborted(segmentation fault storing 1 bytes to address 0)'], assert_returncode=NON_ZERO)

  def test_safe_stack(self):
    self.set_setting('STACK_OVERFLOW_CHECK', 2)
    self.set_setting('STACK_SIZE', 1024)
    if self.is_optimizing():
      expected = [r'Aborted\(stack overflow \(Attempt to set SP to 0x[0-9a-fA-F]+, with stack limits \[0x[0-9a-fA-F]+ - 0x[0-9a-fA-F]+\]\)']
    else:
      expected = [r'Aborted\(stack overflow \(Attempt to set SP to 0x[0-9a-fA-F]+, with stack limits \[0x[0-9a-fA-F]+ - 0x[0-9a-fA-F]+\]\)',
                  '__handle_stack_overflow']
    self.do_runf(test_file('core/test_safe_stack.c'),
                 expected_output=expected,
                 regex=True,
                 assert_all=True,
                 assert_returncode=NON_ZERO)

  @node_pthreads
  def test_safe_stack_pthread(self):
    self.set_setting('STACK_OVERFLOW_CHECK', 2)
    self.set_setting('STACK_SIZE', 65536)
    self.set_setting('PROXY_TO_PTHREAD')
    self.emcc_args.append('-pthread')
    if self.is_optimizing():
      expected = ['Aborted(stack overflow']
    else:
      expected = ['Aborted(stack overflow', '__handle_stack_overflow']
    self.do_runf(test_file('core/test_safe_stack.c'),
                 expected_output=expected,
                 assert_returncode=NON_ZERO, assert_all=True)

  def test_safe_stack_alloca(self):
    self.set_setting('STACK_OVERFLOW_CHECK', 2)
    self.set_setting('STACK_SIZE', 65536)
    if self.is_optimizing():
      expected = ['Aborted(stack overflow']
    else:
      expected = ['Aborted(stack overflow', '__handle_stack_overflow']
    self.do_runf(test_file('core/test_safe_stack_alloca.c'),
                 expected_output=expected,
                 assert_returncode=NON_ZERO, assert_all=True)

  @needs_dylink
  def test_safe_stack_dylink(self):
    self.set_setting('STACK_OVERFLOW_CHECK', 2)
    self.set_setting('STACK_SIZE', 65536)
    self.dylink_test(r'''
      #include <stdio.h>
      extern void sidey();
      int main() {
        sidey();
      }
    ''', '''
      #include <string.h>

      static long accumulator = 0;

      int f(int *b) {
        // Infinite recursion while recording stack pointer locations
        // so that compiler can't eliminate the stack allocs.
        accumulator += (long)b;
        int a[1024];
        return f(a);
      }

      void sidey() {
        f(NULL);
      }
    ''', ['Aborted(stack overflow', '__handle_stack_overflow'], assert_returncode=NON_ZERO, force_c=True)

  def test_fpic_static(self):
    self.emcc_args.append('-fPIC')
    self.do_core_test('test_hello_world.c')

  # Marked as impure since we don't have a wasi-threads is still
  # a WIP.
  # Test is disabled on standalone because of flakes, see
  # https://github.com/emscripten-core/emscripten/issues/18405
  # @also_with_standalone_wasm(impure=True)
  @node_pthreads
  def test_pthread_create(self):
    if not self.get_setting('STANDALONE_WASM'):
      self.set_setting('EXIT_RUNTIME')
    # test that the node environment can be specified by itself, and that still
    # works with pthreads (even though we did not specify 'node,worker')
    self.set_setting('ENVIRONMENT', 'node')
    self.do_run_in_out_file_test('core/pthread/create.cpp')

  @node_pthreads
  @parameterized({
    '': ([],),
    'pooled': (['-sPTHREAD_POOL_SIZE=1'],),
    'proxied': (['-sPROXY_TO_PTHREAD', '-sEXIT_RUNTIME'],),
  })
  def test_pthread_c11_threads(self, args):
    self.emcc_args += args
    self.set_setting('PTHREADS_DEBUG')
    if not self.has_changed_setting('INITIAL_MEMORY'):
      self.set_setting('INITIAL_MEMORY', '64mb')
    # test that the node and worker environments can be specified
    self.set_setting('ENVIRONMENT', 'node,worker')
    self.do_run_in_out_file_test('pthread/test_pthread_c11_threads.c')

  @node_pthreads
  @parameterized({
    '': (0,),
    'pooled': (1,),
  })
  def test_pthread_cxx_threads(self, pthread_pool_size):
    self.set_setting('PTHREAD_POOL_SIZE', pthread_pool_size)
    self.do_run_in_out_file_test('pthread/test_pthread_cxx_threads.cpp')

  @node_pthreads
  @parameterized({
    '': (0,),
    'pooled': (1,),
  })
  def test_pthread_busy_wait(self, pthread_pool_size):
    self.set_setting('PTHREAD_POOL_SIZE', pthread_pool_size)
    self.do_run_in_out_file_test('pthread/test_pthread_busy_wait.cpp')

  @node_pthreads
  def test_pthread_busy_wait_atexit(self):
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    self.set_setting('EXIT_RUNTIME')
    self.do_run_in_out_file_test('pthread/test_pthread_busy_wait_atexit.cpp')

  @node_pthreads
  def test_pthread_create_pool(self):
    # with a pool, we can synchronously depend on workers being available
    self.set_setting('PTHREAD_POOL_SIZE', 2)
    self.set_setting('EXIT_RUNTIME')
    self.emcc_args += ['-DALLOW_SYNC']
    self.do_run_in_out_file_test('core/pthread/create.cpp')

  @node_pthreads
  def test_pthread_create_proxy(self):
    # with PROXY_TO_PTHREAD, we can synchronously depend on workers being available
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.emcc_args += ['-DALLOW_SYNC']
    self.do_run_in_out_file_test('core/pthread/create.cpp')

  @node_pthreads
  def test_pthread_create_embind_stack_check(self):
    # embind should work with stack overflow checks (see #12356)
    self.set_setting('STACK_OVERFLOW_CHECK', 2)
    self.set_setting('EXIT_RUNTIME')
    self.emcc_args += ['-lembind']
    self.do_run_in_out_file_test('core/pthread/create.cpp')

  @node_pthreads
  def test_pthread_exceptions(self):
    self.set_setting('PTHREAD_POOL_SIZE', 2)
    self.set_setting('EXIT_RUNTIME')
    self.emcc_args += ['-fexceptions']
    self.do_run_in_out_file_test('core/pthread/exceptions.cpp')

  @node_pthreads
  def test_pthread_exit_process(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.emcc_args += ['-DEXIT_RUNTIME', '--pre-js', test_file('core/pthread/test_pthread_exit_runtime.pre.js')]
    self.do_run_in_out_file_test('core/pthread/test_pthread_exit_runtime.c', assert_returncode=42)

  @node_pthreads
  def test_pthread_keepalive(self):
    self.do_run_in_out_file_test('core/pthread/test_pthread_keepalive.c')

  @node_pthreads
  def test_pthread_weak_ref(self):
    self.do_run_in_out_file_test('core/pthread/test_pthread_weak_ref.c')

  @node_pthreads
  def test_pthread_exit_main(self):
    self.do_run_in_out_file_test('core/pthread/test_pthread_exit_main.c')

  def test_pthread_exit_main_stub(self):
    self.do_run_in_out_file_test('core/pthread/test_pthread_exit_main.c')

  @node_pthreads
  def test_pthread_unhandledrejection(self):
    # Check that an unhandled promise rejection is propagated to the main thread
    # as an error.
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('NODEJS_CATCH_EXIT', 0)
    self.emcc_args += ['--post-js', test_file('pthread/test_pthread_unhandledrejection.post.js')]
    self.do_runf(test_file('pthread/test_pthread_unhandledrejection.c'), 'passed')

  @node_pthreads
  @no_wasm2js('wasm2js does not support PROXY_TO_PTHREAD (custom section support)')
  def test_pthread_offset_converter(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('USE_OFFSET_CONVERTER')
    if '-g' in self.emcc_args:
      self.emcc_args += ['-DDEBUG']
    self.do_runf(test_file('core/test_return_address.c'), 'passed')

  @node_pthreads
  @no_wasm2js('wasm2js does not support PROXY_TO_PTHREAD (custom section support)')
  def test_pthread_offset_converter_modularize(self):
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('USE_OFFSET_CONVERTER')
    self.set_setting('MODULARIZE')
    create_file('post.js', 'var m = require("./test_return_address.js"); m();')
    self.emcc_args += ['--extern-post-js', 'post.js', '-sEXPORT_NAME=foo']
    if '-g' in self.emcc_args:
      self.emcc_args += ['-DDEBUG']
    self.do_runf(test_file('core/test_return_address.c'), 'passed')

  def test_emscripten_atomics_stub(self):
    self.do_run_in_out_file_test('core/pthread/emscripten_atomics.c')

  @no_asan('incompatibility with atomics')
  @node_pthreads
  def test_emscripten_atomics(self):
    self.emcc_args.append('-pthread')
    self.do_run_in_out_file_test('core/pthread/emscripten_atomics.c')

  @no_asan('incompatibility with atomics')
  @node_pthreads
  def test_emscripten_futexes(self):
    self.emcc_args.append('-pthread')
    self.emcc_args += ['-Wno-nonnull'] # This test explicitly checks behavior of passing NULL to emscripten_futex_wake().
    self.do_run_in_out_file_test('core/pthread/emscripten_futexes.c')

  @node_pthreads
  def test_stdio_locking(self):
    self.set_setting('PTHREAD_POOL_SIZE', '2')
    self.do_run_in_out_file_test('core/test_stdio_locking.c')

  @needs_dylink
  @node_pthreads
  def test_pthread_dylink_basics(self):
    self.emcc_args.append('-Wno-experimental')
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.do_basic_dylink_test()

  @needs_dylink
  @node_pthreads
  def test_pthread_dylink(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.set_setting('PTHREAD_POOL_SIZE', 2)
    main = test_file('core/pthread/test_pthread_dylink.c')

    # test with a long .so name, as a regression test for
    # https://github.com/emscripten-core/emscripten/issues/14833
    # where we had a bug with long names + TextDecoder + pthreads + dylink
    very_long_name = 'very_very_very_very_very_very_very_very_very_long.so'

    self.dylink_testf(main, so_name=very_long_name,
                      need_reverse=False)

  @parameterized({
    '': (['-sNO_AUTOLOAD_DYLIBS'],),
    'autoload': ([],)
  })
  @needs_dylink
  @node_pthreads
  def test_pthread_dylink_entry_point(self, args):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    main = test_file('core/pthread/test_pthread_dylink_entry_point.c')
    self.dylink_testf(main, need_reverse=False, emcc_args=args)

  @needs_dylink
  @node_pthreads
  def test_pthread_dylink_exceptions(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.emcc_args.append('-fexceptions')
    self.dylink_testf(test_file('core/pthread/test_pthread_dylink_exceptions.cpp'))

  @needs_dylink
  @node_pthreads
  def test_pthread_dlopen(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.build_dlfcn_lib(test_file('core/pthread/test_pthread_dlopen_side.c'))

    self.emcc_args += ['--embed-file', 'liblib.so@libside.so']
    self.prep_dlfcn_main()
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('PROXY_TO_PTHREAD')
    self.do_runf(test_file('core/pthread/test_pthread_dlopen.c'),
                 ['side module ctor', 'done join', 'side module atexit'],
                 assert_all=True)

  @needs_dylink
  @node_pthreads
  def test_pthread_dlopen_many(self):
    if self.is_wasm64():
     self.skipTest('https://github.com/emscripten-core/emscripten/issues/18887')

    nthreads = 10
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.build_dlfcn_lib(test_file('core/pthread/test_pthread_dlopen_side.c'))
    for i in range(nthreads):
      shutil.copyfile('liblib.so', f'liblib{i}.so')

    self.prep_dlfcn_main()
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', 'jslib_func')
    create_file('lib.js', r'''
      mergeInto(LibraryManager.library, {
        jslib_func__sig: 'v',
        jslib_func: () => err('hello from js')
      });
    ''')
    self.emcc_args.append('--js-library=lib.js')
    self.do_runf(test_file('core/pthread/test_pthread_dlopen_many.c'),
                 ['side module ctor', 'main done', 'side module atexit'],
                 emcc_args=[f'-DNUM_THREADS={nthreads}'],
                 assert_all=True)

  @needs_dylink
  @node_pthreads
  def test_pthread_dlsym(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.build_dlfcn_lib(test_file('core/pthread/test_pthread_dlsym_side.c'))

    self.prep_dlfcn_main()
    self.set_setting('EXIT_RUNTIME')
    self.set_setting('PROXY_TO_PTHREAD')
    self.do_runf(test_file('core/pthread/test_pthread_dlsym.c'))

  @needs_dylink
  @node_pthreads
  def test_pthread_dylink_tls(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.set_setting('PTHREAD_POOL_SIZE', 1)
    main = test_file('core/pthread/test_pthread_dylink_tls.c')
    self.dylink_testf(main, need_reverse=False)

  @needs_dylink
  @node_pthreads
  def test_pthread_dylink_longjmp(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.set_setting('PTHREAD_POOL_SIZE=1')
    main = test_file('core/pthread/test_pthread_dylink_longjmp.c')
    self.dylink_testf(main, need_reverse=False)

  @needs_dylink
  @node_pthreads
  def test_pthread_dylink_main_module_1(self):
    self.emcc_args += ['-Wno-experimental', '-pthread']
    self.set_setting('MAIN_MODULE')
    self.do_runf(test_file('hello_world.c'))

  @needs_dylink
  @parameterized({
    '': ([],),
    'pthreads': (['-sPROXY_TO_PTHREAD', '-sEXIT_RUNTIME', '-pthread', '-Wno-experimental'],)
  })
  def test_Module_dynamicLibraries(self, args):
    # test that Module.dynamicLibraries works with pthreads
    self.emcc_args += args
    self.emcc_args += ['--pre-js', 'pre.js']
    # This test is for setting dynamicLibraries at runtime so we don't
    # want emscripten loading `liblib.so` automatically (which it would
    # do without this setting.
    self.set_setting('NO_AUTOLOAD_DYLIBS')

    create_file('pre.js', '''
      Module['dynamicLibraries'] = ['liblib.so'];
    ''')

    if args:
      self.setup_node_pthreads()
      create_file('post.js', '''
        if (ENVIRONMENT_IS_PTHREAD) {
          err('sharedModules: ' + Object.keys(sharedModules));
          assert('liblib.so' in sharedModules);
          assert(sharedModules['liblib.so'] instanceof WebAssembly.Module);
        }
      ''')
      self.emcc_args += ['--post-js', 'post.js']

    self.dylink_test(
      r'''
        #include <stdio.h>
        int side();
        int main() {
          printf("result is %d\n", side());
          return 0;
        }
      ''',
      r'''
        int side() { return 42; }
      ''',
      'result is 42')

  # Tests the emscripten_get_exported_function() API.
  def test_emscripten_get_exported_function(self):
    self.set_setting('ALLOW_TABLE_GROWTH')
    self.emcc_args += ['-lexports.js']
    self.do_core_test('test_get_exported_function.cpp')

  # Tests the emscripten_get_exported_function() API.
  @no_asan('TODO: ASan support in minimal runtime')
  def test_minimal_runtime_emscripten_get_exported_function(self):
    self.set_setting('ALLOW_TABLE_GROWTH')
    self.set_setting('MINIMAL_RUNTIME')
    self.emcc_args += ['--pre-js', test_file('minimal_runtime_exit_handling.js')]
    self.emcc_args += ['-lexports.js']
    self.do_core_test('test_get_exported_function.cpp')

  # Marked as impure since the WASI reactor modules (modules without main)
  # are not yet suppored by the wasm engines we test against.
  @also_with_standalone_wasm(impure=True)
  def test_undefined_main(self):
    if self.get_setting('STANDALONE_WASM'):
      # In standalone we don't support implicitly building without main.  The user has to explicitly
      # opt out (see below).
      err = self.expect_fail([EMCC, test_file('core/test_ctors_no_main.cpp')] + self.get_emcc_args())
      self.assertContained('undefined symbol: main', err)
    elif not self.get_setting('STRICT'):
      # Traditionally in emscripten we allow main to be implicitly undefined.  This allows programs
      # with a main and libraries without a main to be compiled identically.
      # However we are trying to move away from that model to a more explicit opt-out model. See:
      # https://github.com/emscripten-core/emscripten/issues/9640
      self.do_core_test('test_ctors_no_main.cpp')

      # Disabling IGNORE_MISSING_MAIN should cause link to fail due to missing main
      self.set_setting('IGNORE_MISSING_MAIN', 0)
      err = self.expect_fail([EMCC, test_file('core/test_ctors_no_main.cpp')] + self.get_emcc_args())
      self.assertContained('error: entry symbol not defined (pass --no-entry to suppress): main', err)

      # In non-standalone mode exporting an empty list of functions signal that we don't
      # have a main and so should not generate an error.
      self.set_setting('EXPORTED_FUNCTIONS', [])
      self.do_core_test('test_ctors_no_main.cpp')
      self.clear_setting('EXPORTED_FUNCTIONS')

  # Marked as impure since the WASI reactor modules (modules without main)
  # are not yet supported by the wasm engines we test against.
  @also_with_standalone_wasm(impure=True)
  def test_undefined_main_explicit(self):
    # If we pass --no-entry this test should compile without issue
    self.emcc_args.append('--no-entry')
    self.do_core_test('test_ctors_no_main.cpp')

  def test_undefined_main_wasm_output(self):
    if not can_do_standalone(self):
      self.skipTest('standalone mode only')
    err = self.expect_fail([EMCC, '-o', 'out.wasm', test_file('core/test_ctors_no_main.cpp')] + self.get_emcc_args())
    self.assertContained('undefined symbol: main', err)

  def test_export_start(self):
    if not can_do_standalone(self):
      self.skipTest('standalone mode only')
    self.set_setting('STANDALONE_WASM')
    self.set_setting('EXPORTED_FUNCTIONS', ['__start'])
    self.do_core_test('test_hello_world.c')

  # Tests the operation of API found in #include <emscripten/math.h>
  def test_emscripten_math(self):
    self.do_core_test('test_emscripten_math.c')

  # Tests that users can pass custom JS options from command line using
  # the -jsDfoo=val syntax:
  # See https://github.com/emscripten-core/emscripten/issues/10580.
  def test_custom_js_options(self):
    self.emcc_args += ['--js-library', test_file('core/test_custom_js_settings.js'), '-jsDCUSTOM_JS_OPTION=1']
    self.do_core_test('test_custom_js_settings.c')

    self.assertContained('cannot change built-in settings values with a -jsD directive', self.expect_fail([EMCC, '-jsDWASM=0']))

  # Tests <emscripten/stack.h> API
  @no_asan('stack allocation sizes are no longer predictable')
  def test_emscripten_stack(self):
    self.set_setting('STACK_SIZE', 4 * 1024 * 1024)
    self.do_core_test('test_stack_get_free.c')

  # Tests settings.ABORT_ON_WASM_EXCEPTIONS
  def test_abort_on_exceptions(self):
    self.set_setting('ABORT_ON_WASM_EXCEPTIONS')
    self.set_setting('ALLOW_TABLE_GROWTH')
    self.set_setting('EXPORTED_RUNTIME_METHODS', ['ccall', 'cwrap'])
    self.set_setting('DEFAULT_LIBRARY_FUNCS_TO_INCLUDE', ['$addFunction'])
    self.emcc_args += ['-lembind', '--post-js', test_file('core/test_abort_on_exceptions_post.js')]
    self.do_core_test('test_abort_on_exceptions.cpp', interleaved_output=False)

  def test_abort_on_exceptions_main(self):
    # The unhandled exception wrappers should not kick in for exceptions thrown during main
    self.set_setting('ABORT_ON_WASM_EXCEPTIONS')
    self.emcc_args.append('--minify=0')
    output = self.do_runf(test_file('core/test_abort_on_exceptions_main.c'), assert_returncode=NON_ZERO)
    # The exception should make it all the way out
    self.assertContained('Error: crash', output)
    # And not be translated into abort by makeAbortWrapper
    self.assertNotContained('unhandled exception', output)
    self.assertNotContained('Aborted', output)

  @node_pthreads
  def test_abort_on_exceptions_pthreads(self):
    self.set_setting('ABORT_ON_WASM_EXCEPTIONS')
    self.set_setting('PROXY_TO_PTHREAD')
    self.set_setting('EXIT_RUNTIME')
    self.do_core_test('test_hello_world.c')

  @needs_dylink
  def test_gl_main_module(self):
    self.set_setting('MAIN_MODULE')
    self.do_runf(test_file('core/test_gl_get_proc_address.c'))

  @needs_dylink
  def test_main_module_js_symbol(self):
    self.set_setting('MAIN_MODULE', 2)
    self.emcc_args += ['--js-library', test_file('core/test_main_module_js_symbol.js')]
    self.do_runf(test_file('core/test_main_module_js_symbol.c'))

  def test_emscripten_async_call(self):
    self.set_setting('EXIT_RUNTIME')
    self.do_run_in_out_file_test(test_file('core/test_emscripten_async_call.c'))

  @no_asan('asyncify stack operations confuse asan')
  @no_wasm64('TODO: asyncify for wasm64')
  @parameterized({
    '': ([],),
    'no_dynamic_execution': (['-sDYNAMIC_EXECUTION=0'],)
  })
  def test_embind_lib_with_asyncify(self, args):
    self.uses_es6 = True
    self.emcc_args += [
      '-lembind',
      '-sASYNCIFY',
      '-sASYNCIFY_IMPORTS=["sleep_and_return"]',
      '-sDEFAULT_LIBRARY_FUNCS_TO_INCLUDE=$ASSERTIONS',
      '--post-js', test_file('core/embind_lib_with_asyncify.test.js'),
    ]
    self.emcc_args += args
    self.do_core_test('embind_lib_with_asyncify.cpp')

  @no_asan('asyncify stack operations confuse asan')
  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_em_async_js(self):
    self.uses_es6 = True
    if not self.get_setting('ASYNCIFY'):
      self.set_setting('ASYNCIFY')
    self.set_setting('EXPORTED_RUNTIME_METHODS', 'ccall')
    self.maybe_closure()
    self.do_core_test('test_em_async_js.c')

  @requires_v8
  @no_wasm2js('wasm2js does not support reference types')
  def test_externref(self):
    self.run_process([EMCC, '-c', test_file('core/test_externref.s'), '-o', 'asm.o'] + self.get_emcc_args(ldflags=False))
    self.emcc_args += ['--js-library', test_file('core/test_externref.js')]
    self.emcc_args += ['-mreference-types']
    self.do_core_test('test_externref.c', libraries=['asm.o'])

  def test_syscall_intercept(self):
    self.do_core_test('test_syscall_intercept.c')

  @also_with_wasm_bigint
  def test_js_library_i64_params(self):
    # Tests the defineI64Param and receiveI64ParamAsI53 helpers that are
    # used to recieve i64 argument in syscalls.
    self.emcc_args += ['--js-library=' + test_file('core/js_library_i64_params.js')]
    self.do_core_test('js_library_i64_params.c')

  def test_main_reads_args(self):
    self.run_process([EMCC, '-c', test_file('core/test_main_reads_args_real.c'), '-o', 'real.o'] + self.get_emcc_args(ldflags=False))
    self.do_core_test('test_main_reads_args.c', emcc_args=['real.o', '-sEXIT_RUNTIME'], regex=True)

  @requires_node
  def test_promise(self):
    # This test depends on Promise.any, which in turn requires a modern target.  Check that it
    # fails to even build without bumping the min versions:
    err = self.expect_fail([EMCC, test_file('core/test_promise.c')])
    self.assertContained('error: emscripten_promise_any used, but Promise.any is not supported by the current runtime configuration', err)
    self.set_setting('MIN_NODE_VERSION', '150000')
    self.set_setting('MIN_SAFARI_VERSION', '150000')
    self.set_setting('MIN_FIREFOX_VERSION', '79')
    self.set_setting('MIN_CHROME_VERSION', '85')
    self.do_core_test('test_promise.c')

  @no_wasm64('TODO: asyncify for wasm64')
  @with_asyncify_and_jspi
  def test_promise_await(self):
    self.do_core_test('test_promise_await.c')

  def test_promise_await_error(self):
    # Check that the API is not available when ASYNCIFY is not set
    self.do_runf(test_file('core/test_promise_await.c'), 'Aborted(emscripten_promise_await is only available with ASYNCIFY)',
                 assert_returncode=NON_ZERO)

  def test_emscripten_async_load_script(self):
    create_file('script1.js', 'Module._set(456);''')
    create_file('file1.txt', 'first')
    create_file('file2.txt', 'second')
    # `--from-emcc` needed here otherwise the output defines `var Module =` which will shadow the
    # global `Module`.
    self.run_process([FILE_PACKAGER, 'test.data', '--preload', 'file1.txt', 'file2.txt', '--from-emcc', '--js-output=script2.js'])
    self.do_runf(test_file('test_emscripten_async_load_script.c'), emcc_args=['-sFORCE_FILESYSTEM'])

  @node_pthreads
  def test_wasm_worker_hello(self):
    self.do_runf(test_file('wasm_worker/hello_wasm_worker.c'), emcc_args=['-sWASM_WORKERS'])

  @node_pthreads
  def test_wasm_worker_malloc(self):
    self.do_runf(test_file('wasm_worker/malloc_wasm_worker.c'), emcc_args=['-sWASM_WORKERS'])


# Generate tests for everything
def make_run(name, emcc_args, settings=None, env=None,
             require_v8=False, v8_args=None,
             require_node=False, node_args=None,
             require_wasm64=False):
  if env is None:
    env = {}
  if settings is None:
    settings = {}
  if settings:
    # Until we create a way to specify link-time settings separately from compile-time settings
    # we need to pass this flag here to avoid warnings from compile-only commands.
    emcc_args.append('-Wno-unused-command-line-argument')

  TT = type(name, (TestCoreBase,), dict(run_name=name, env=env, __module__=__name__))  # noqa

  def tearDown(self):
    try:
      super(TT, self).tearDown()
    finally:
      for k in self.env.keys():
        del os.environ[k]

  TT.tearDown = tearDown

  def setUp(self):
    super(TT, self).setUp()
    for k, v in self.env.items():
      assert k not in os.environ, k + ' should not be in environment'
      os.environ[k] = v

    os.chdir(self.get_dir()) # Ensure the directory exists and go there

    for k, v in settings.items():
      self.set_setting(k, v)

    self.emcc_args += emcc_args

    if node_args:
      self.node_args += node_args

    if v8_args:
      self.v8_args += v8_args

    if require_v8:
      self.require_v8()
    elif require_node:
      self.require_node()

    if require_wasm64:
      self.require_wasm64()

  TT.setUp = setUp

  return TT


# Note: We add --profiling-funcs to many of these modes (especially
# modes under active development) since it makes debugging test
# failures easier.  The downside of this approach is that we are not
# testing the default mode (i.e. without `--profiling-funcs`).  See:
# https://github.com/emscripten-core/emscripten/pull/15480

# Main wasm test modes
core0 = make_run('core0', emcc_args=['-O0'])
core0g = make_run('core0g', emcc_args=['-O0', '-g'])
core1 = make_run('core1', emcc_args=['-O1'])
core2 = make_run('core2', emcc_args=['-O2'])
core2g = make_run('core2g', emcc_args=['-O2', '-g'])
core3 = make_run('core3', emcc_args=['-O3'])
cores = make_run('cores', emcc_args=['-Os'])
corez = make_run('corez', emcc_args=['-Oz'])

# MEMORY64=1
wasm64 = make_run('wasm64', emcc_args=['-O1', '-Wno-experimental', '--profiling-funcs'],
                  settings={'MEMORY64': 1}, require_wasm64=True, require_node=True)
wasm64_v8 = make_run('wasm64_v8', emcc_args=['-Wno-experimental', '--profiling-funcs'],
                     settings={'MEMORY64': 1}, require_wasm64=True, require_v8=True)
# MEMORY64=2, or "lowered"
wasm64l = make_run('wasm64l', emcc_args=['-O1', '-Wno-experimental', '--profiling-funcs'],
                   settings={'MEMORY64': 2},
                   node_args=shared.node_bigint_flags())

lto0 = make_run('lto0', emcc_args=['-flto', '-O0'])
lto1 = make_run('lto1', emcc_args=['-flto', '-O1'])
lto2 = make_run('lto2', emcc_args=['-flto', '-O2'])
lto3 = make_run('lto3', emcc_args=['-flto', '-O3'])
ltos = make_run('ltos', emcc_args=['-flto', '-Os'])
ltoz = make_run('ltoz', emcc_args=['-flto', '-Oz'])

thinlto0 = make_run('thinlto0', emcc_args=['-flto=thin', '-O0'])
thinlto1 = make_run('thinlto1', emcc_args=['-flto=thin', '-O1'])
thinlto2 = make_run('thinlto2', emcc_args=['-flto=thin', '-O2'])
thinlto3 = make_run('thinlto3', emcc_args=['-flto=thin', '-O3'])
thinltos = make_run('thinltos', emcc_args=['-flto=thin', '-Os'])
thinltoz = make_run('thinltoz', emcc_args=['-flto=thin', '-Oz'])

wasm2js0 = make_run('wasm2js0', emcc_args=['-O0'], settings={'WASM': 0})
wasm2js1 = make_run('wasm2js1', emcc_args=['-O1'], settings={'WASM': 0})
wasm2js2 = make_run('wasm2js2', emcc_args=['-O2'], settings={'WASM': 0})
wasm2js3 = make_run('wasm2js3', emcc_args=['-O3'], settings={'WASM': 0})
wasm2jss = make_run('wasm2jss', emcc_args=['-Os'], settings={'WASM': 0})
wasm2jsz = make_run('wasm2jsz', emcc_args=['-Oz'], settings={'WASM': 0})

# Secondary test modes - run directly when there is a specific need

# features

simd2 = make_run('simd2', emcc_args=['-O2', '-msimd128'])
bulkmem2 = make_run('bulkmem2', emcc_args=['-O2', '-mbulk-memory'])
wasmfs = make_run('wasmfs', emcc_args=['-O2', '-DWASMFS'], settings={'WASMFS': 1})

# SAFE_HEAP/STACK_OVERFLOW_CHECK
core0s = make_run('core2s', emcc_args=['-g'], settings={'SAFE_HEAP': 1})
core2s = make_run('core2s', emcc_args=['-O2'], settings={'SAFE_HEAP': 1})
core2ss = make_run('core2ss', emcc_args=['-O2'], settings={'STACK_OVERFLOW_CHECK': 2})

bigint = make_run('bigint', emcc_args=['--profiling-funcs'], settings={'WASM_BIGINT': 1},
                  node_args=shared.node_bigint_flags())

# Add DEFAULT_TO_CXX=0
strict = make_run('strict', emcc_args=[], settings={'STRICT': 1})

ubsan = make_run('ubsan', emcc_args=['-fsanitize=undefined', '--profiling'])
lsan = make_run('lsan', emcc_args=['-fsanitize=leak', '--profiling'], settings={'ALLOW_MEMORY_GROWTH': 1})
asan = make_run('asan', emcc_args=['-fsanitize=address', '--profiling'], settings={'ALLOW_MEMORY_GROWTH': 1})
asani = make_run('asani', emcc_args=['-fsanitize=address', '--profiling', '--pre-js', os.path.join(os.path.dirname(__file__), 'asan-no-leak.js')],
                 settings={'ALLOW_MEMORY_GROWTH': 1})

# Experimental modes (not tested by CI)
minimal0 = make_run('minimal0', emcc_args=['-g'], settings={'MINIMAL_RUNTIME': 1})

# TestCoreBase is just a shape for the specific subclasses, we don't test it itself
del TestCoreBase # noqa
