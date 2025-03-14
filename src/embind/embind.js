// Copyright 2012 The Emscripten Authors.  All rights reserved.
// Emscripten is available under two separate licenses, the MIT license and the
// University of Illinois/NCSA Open Source License.  Both these licenses can be
// found in the LICENSE file.

/*global LibraryManager, mergeInto*/

/*global Module, asm*/
/*global _malloc, _free, _memcpy*/
/*global FUNCTION_TABLE, HEAP8, HEAPU8, HEAP16, HEAPU16, HEAP32, HEAPU32, HEAPF32, HEAPF64*/
/*global readLatin1String*/
/*global Emval, emval_handle_array, __emval_decref*/
/*jslint sub:true*/ /* The symbols 'fromWireType' and 'toWireType' must be accessed via array notation to be closure-safe since craftInvokerFunction crafts functions as strings that can't be closured. */

// -- jshint doesn't understand library syntax, so we need to specifically tell it about the symbols we define
/*global typeDependencies, flushPendingDeletes, getTypeName, getBasestPointer, throwBindingError, UnboundTypeError, embindRepr, registeredInstances, registeredTypes, getShiftFromSize*/
/*global ensureOverloadTable, embind__requireFunction, awaitingDependencies, makeLegalFunctionName, embind_charCodes:true, registerType, createNamedFunction, RegisteredPointer, throwInternalError*/
/*global simpleReadValueFromPointer, floatReadValueFromPointer, integerReadValueFromPointer, enumReadValueFromPointer, replacePublicSymbol, craftInvokerFunction, tupleRegistrations*/
/*global finalizationRegistry, attachFinalizer, detachFinalizer, releaseClassHandle, runDestructor*/
/*global ClassHandle, makeClassHandle, structRegistrations, whenDependentTypesAreResolved, BindingError, deletionQueue, delayFunction:true, upcastPointer*/
/*global exposePublicSymbol, heap32VectorToArray, newFunc, RegisteredPointer_getPointee, RegisteredPointer_destructor, RegisteredPointer_deleteObject, char_0, char_9*/
/*global getInheritedInstanceCount, getLiveInheritedInstances, setDelayFunction, InternalError, runDestructors*/
/*global requireRegisteredType, unregisterInheritedInstance, registerInheritedInstance, PureVirtualError, throwUnboundTypeError*/
/*global assert, validateThis, downcastPointer, registeredPointers, RegisteredClass, getInheritedInstance, ClassHandle_isAliasOf, ClassHandle_clone, ClassHandle_isDeleted, ClassHandle_deleteLater*/
/*global throwInstanceAlreadyDeleted, shallowCopyInternalPointer*/
/*global RegisteredPointer_fromWireType, constNoSmartPtrRawPointerToWireType, nonConstNoSmartPtrRawPointerToWireType, genericPointerToWireType*/

#include "embind/embind_shared.js"

var LibraryEmbind = {
  $UnboundTypeError__postset: "UnboundTypeError = Module['UnboundTypeError'] = extendError(Error, 'UnboundTypeError');",
  $UnboundTypeError__deps: ['$extendError'],
  $UnboundTypeError: undefined,
  $PureVirtualError__postset: "PureVirtualError = Module['PureVirtualError'] = extendError(Error, 'PureVirtualError');",
  $PureVirtualError__deps: ['$extendError'],
  $PureVirtualError: undefined,

  $init_embind__deps: [
    '$getInheritedInstanceCount', '$getLiveInheritedInstances',
    '$flushPendingDeletes', '$setDelayFunction'],
  $init_embind__postset: 'init_embind();',
  $init_embind: function() {
    Module['getInheritedInstanceCount'] = getInheritedInstanceCount;
    Module['getLiveInheritedInstances'] = getLiveInheritedInstances;
    Module['flushPendingDeletes'] = flushPendingDeletes;
    Module['setDelayFunction'] = setDelayFunction;
  },

  $throwUnboundTypeError__deps: ['$registeredTypes', '$typeDependencies', '$UnboundTypeError', '$getTypeName'],
  $throwUnboundTypeError: function(message, types) {
    var unboundTypes = [];
    var seen = {};
    function visit(type) {
      if (seen[type]) {
        return;
      }
      if (registeredTypes[type]) {
        return;
      }
      if (typeDependencies[type]) {
        typeDependencies[type].forEach(visit);
        return;
      }
      unboundTypes.push(type);
      seen[type] = true;
    }
    types.forEach(visit);

    throw new UnboundTypeError(`${message}: ` + unboundTypes.map(getTypeName).join([', ']));
  },

  // Creates a function overload resolution table to the given method 'methodName' in the given prototype,
  // if the overload table doesn't yet exist.
  $ensureOverloadTable__deps: ['$throwBindingError'],
  $ensureOverloadTable: function(proto, methodName, humanName) {
    if (undefined === proto[methodName].overloadTable) {
      var prevFunc = proto[methodName];
      // Inject an overload resolver function that routes to the appropriate overload based on the number of arguments.
      proto[methodName] = function() {
        // TODO This check can be removed in -O3 level "unsafe" optimizations.
        if (!proto[methodName].overloadTable.hasOwnProperty(arguments.length)) {
            throwBindingError(`Function '${humanName}' called with an invalid number of arguments (${arguments.length}) - expects one of (${proto[methodName].overloadTable})!`);
        }
        return proto[methodName].overloadTable[arguments.length].apply(this, arguments);
      };
      // Move the previous function into the overload table.
      proto[methodName].overloadTable = [];
      proto[methodName].overloadTable[prevFunc.argCount] = prevFunc;
    }
  },

  /*
   Registers a symbol (function, class, enum, ...) as part of the Module JS object so that
   hand-written code is able to access that symbol via 'Module.name'.
   name: The name of the symbol that's being exposed.
   value: The object itself to expose (function, class, ...)
   numArguments: For functions, specifies the number of arguments the function takes in. For other types, unused and undefined.

   To implement support for multiple overloads of a function, an 'overload selector' function is used. That selector function chooses
   the appropriate overload to call from an function overload table. This selector function is only used if multiple overloads are
   actually registered, since it carries a slight performance penalty. */
  $exposePublicSymbol__deps: ['$ensureOverloadTable', '$throwBindingError'],
  $exposePublicSymbol__docs: '/** @param {number=} numArguments */',
  $exposePublicSymbol: function(name, value, numArguments) {
    if (Module.hasOwnProperty(name)) {
      if (undefined === numArguments || (undefined !== Module[name].overloadTable && undefined !== Module[name].overloadTable[numArguments])) {
        throwBindingError(`Cannot register public name '${name}' twice`);
      }

      // We are exposing a function with the same name as an existing function. Create an overload table and a function selector
      // that routes between the two.
      ensureOverloadTable(Module, name, name);
      if (Module.hasOwnProperty(numArguments)) {
        throwBindingError(`Cannot register multiple overloads of a function with the same number of arguments (${numArguments})!`);
      }
      // Add the new function into the overload table.
      Module[name].overloadTable[numArguments] = value;
    }
    else {
      Module[name] = value;
      if (undefined !== numArguments) {
        Module[name].numArguments = numArguments;
      }
    }
  },

  $replacePublicSymbol__deps: ['$throwInternalError'],
  $replacePublicSymbol__docs: '/** @param {number=} numArguments */',
  $replacePublicSymbol: function(name, value, numArguments) {
    if (!Module.hasOwnProperty(name)) {
      throwInternalError('Replacing nonexistant public symbol');
    }
    // If there's an overload table for this symbol, replace the symbol in the overload table instead.
    if (undefined !== Module[name].overloadTable && undefined !== numArguments) {
      Module[name].overloadTable[numArguments] = value;
    }
    else {
      Module[name] = value;
      Module[name].argCount = numArguments;
    }
  },

  // from https://github.com/imvu/imvujs/blob/master/src/error.js
  $extendError__deps: ['$createNamedFunction'],
  $extendError: function(baseErrorType, errorName) {
    var errorClass = createNamedFunction(errorName, function(message) {
      this.name = errorName;
      this.message = message;

      var stack = (new Error(message)).stack;
      if (stack !== undefined) {
        this.stack = this.toString() + '\n' +
            stack.replace(/^Error(:[^\n]*)?\n/, '');
      }
    });
    errorClass.prototype = Object.create(baseErrorType.prototype);
    errorClass.prototype.constructor = errorClass;
    errorClass.prototype.toString = function() {
      if (this.message === undefined) {
        return this.name;
      } else {
        return `${this.name}: ${this.message}`;
      }
    };

    return errorClass;
  },


  $createNamedFunction__deps: ['$makeLegalFunctionName'],
  $createNamedFunction: function(name, body) {
    name = makeLegalFunctionName(name);
    // Use an abject with a computed property name to create a new function with
    // a name specified at runtime, but without using `new Function` or `eval`.
    return {
      [name]: function() {
        return body.apply(this, arguments);
      }
    }[name];
  },

  $embindRepr: function(v) {
    if (v === null) {
        return 'null';
    }
    var t = typeof v;
    if (t === 'object' || t === 'array' || t === 'function') {
        return v.toString();
    } else {
        return '' + v;
    }
  },

#if EMBIND_ALLOW_UNICODE_IDENTIFIER
  $identifierToString__deps: ['$UTF8ToString'],
  $identifierToString: function(ptr) {
    return UTF8ToString(ptr)
  },
#else
  $identifierToString__deps: ['$readLatin1String'],
  $identifierToString: function(ptr) {
    return readLatin1String(ptr)
  },
#endif

  // raw pointer -> instance
  $registeredInstances__deps: ['$init_embind'],
  $registeredInstances: {},

  $getBasestPointer__deps: ['$throwBindingError'],
  $getBasestPointer: function(class_, ptr) {
    if (ptr === undefined) {
        throwBindingError('ptr should not be undefined');
    }
    while (class_.baseClass) {
        ptr = class_.upcast(ptr);
        class_ = class_.baseClass;
    }
    return ptr;
  },

  $registerInheritedInstance__deps: ['$registeredInstances', '$getBasestPointer', '$throwBindingError'],
  $registerInheritedInstance: function(class_, ptr, instance) {
    ptr = getBasestPointer(class_, ptr);
    if (registeredInstances.hasOwnProperty(ptr)) {
        throwBindingError(`Tried to register registered instance: ${ptr}`);
    } else {
        registeredInstances[ptr] = instance;
    }
  },

  $unregisterInheritedInstance__deps: ['$registeredInstances', '$getBasestPointer', '$throwBindingError'],
  $unregisterInheritedInstance: function(class_, ptr) {
    ptr = getBasestPointer(class_, ptr);
    if (registeredInstances.hasOwnProperty(ptr)) {
        delete registeredInstances[ptr];
    } else {
        throwBindingError(`Tried to unregister unregistered instance: ${ptr}`);
    }
  },

  $getInheritedInstance__deps: ['$registeredInstances', '$getBasestPointer'],
  $getInheritedInstance: function(class_, ptr) {
    ptr = getBasestPointer(class_, ptr);
    return registeredInstances[ptr];
  },

  $getInheritedInstanceCount__deps: ['$registeredInstances'],
  $getInheritedInstanceCount: function() {
    return Object.keys(registeredInstances).length;
  },

  $getLiveInheritedInstances__deps: ['$registeredInstances'],
  $getLiveInheritedInstances: function() {
    var rv = [];
    for (var k in registeredInstances) {
      if (registeredInstances.hasOwnProperty(k)) {
        rv.push(registeredInstances[k]);
      }
    }
    return rv;
  },

  // class typeID -> {pointerType: ..., constPointerType: ...}
  $registeredPointers: {},

  $registerType__deps: ['$sharedRegisterType'],
  $registerType__docs: '/** @param {Object=} options */',
  $registerType: function(rawType, registeredInstance, options = {}) {
    if (!('argPackAdvance' in registeredInstance)) {
      throw new TypeError('registerType registeredInstance requires argPackAdvance');
    }
    return sharedRegisterType(rawType, registeredInstance, options);
  },

  _embind_register_void__deps: ['$readLatin1String', '$registerType'],
  _embind_register_void: function(rawType, name) {
    name = readLatin1String(name);
    registerType(rawType, {
        isVoid: true, // void return values can be optimized out sometimes
        name,
        'argPackAdvance': 0,
        'fromWireType': function() {
            return undefined;
        },
        'toWireType': function(destructors, o) {
            // TODO: assert if anything else is given?
            return undefined;
        },
    });
  },

  _embind_register_bool__deps: [
    '$getShiftFromSize', '$readLatin1String', '$registerType'],
  _embind_register_bool: function(rawType, name, size, trueValue, falseValue) {
    var shift = getShiftFromSize(size);

    name = readLatin1String(name);
    registerType(rawType, {
        name,
        'fromWireType': function(wt) {
            // ambiguous emscripten ABI: sometimes return values are
            // true or false, and sometimes integers (0 or 1)
            return !!wt;
        },
        'toWireType': function(destructors, o) {
            return o ? trueValue : falseValue;
        },
        'argPackAdvance': 8,
        'readValueFromPointer': function(pointer) {
            // TODO: if heap is fixed (like in asm.js) this could be executed outside
            var heap;
            if (size === 1) {
                heap = HEAP8;
            } else if (size === 2) {
                heap = HEAP16;
            } else if (size === 4) {
                heap = HEAP32;
            } else {
                throw new TypeError("Unknown boolean type size: " + name);
            }
            return this['fromWireType'](heap[pointer >> shift]);
        },
        destructorFunction: null, // This type does not need a destructor
    });
  },

  $getShiftFromSize__deps: [],
  $getShiftFromSize: function(size) {
    switch (size) {
        case 1: return 0;
        case 2: return 1;
        case 4: return 2;
        case 8: return 3;
        default:
            throw new TypeError(`Unknown type size: ${size}`);
    }
  },

  $integerReadValueFromPointer__deps: [],
  $integerReadValueFromPointer: function(name, shift, signed) {
    // integers are quite common, so generate very specialized functions
    switch (shift) {
        case 0: return signed ?
            function readS8FromPointer(pointer) { return HEAP8[pointer]; } :
            function readU8FromPointer(pointer) { return HEAPU8[pointer]; };
        case 1: return signed ?
            function readS16FromPointer(pointer) { return HEAP16[pointer >> 1]; } :
            function readU16FromPointer(pointer) { return HEAPU16[pointer >> 1]; };
        case 2: return signed ?
            function readS32FromPointer(pointer) { return HEAP32[pointer >> 2]; } :
            function readU32FromPointer(pointer) { return HEAPU32[pointer >> 2]; };
#if WASM_BIGINT
        case 3: return signed ?
            function readS64FromPointer(pointer) { return HEAP64[pointer >> 3]; } :
            function readU64FromPointer(pointer) { return HEAPU64[pointer >> 3]; };
#endif
        default:
            throw new TypeError("Unknown integer type: " + name);
    }
  },

  $enumReadValueFromPointer__deps: [],
  $enumReadValueFromPointer: function(name, shift, signed) {
    switch (shift) {
        case 0: return function(pointer) {
            var heap = signed ? HEAP8 : HEAPU8;
            return this['fromWireType'](heap[pointer]);
        };
        case 1: return function(pointer) {
            var heap = signed ? HEAP16 : HEAPU16;
            return this['fromWireType'](heap[pointer >> 1]);
        };
        case 2: return function(pointer) {
            var heap = signed ? HEAP32 : HEAPU32;
            return this['fromWireType'](heap[pointer >> 2]);
        };
        default:
            throw new TypeError("Unknown integer type: " + name);
    }
  },

  $floatReadValueFromPointer__deps: [],
  $floatReadValueFromPointer: function(name, shift) {
    switch (shift) {
        case 2: return function(pointer) {
            return this['fromWireType'](HEAPF32[pointer >> 2]);
        };
        case 3: return function(pointer) {
            return this['fromWireType'](HEAPF64[pointer >> 3]);
        };
        default:
            throw new TypeError("Unknown float type: " + name);
    }
  },

  // When converting a number from JS to C++ side, the valid range of the number is
  // [minRange, maxRange], inclusive.
  _embind_register_integer__deps: [
    '$embindRepr', '$getShiftFromSize', '$integerReadValueFromPointer',
    '$readLatin1String', '$registerType'],
  _embind_register_integer: function(primitiveType, name, size, minRange, maxRange) {
    name = readLatin1String(name);
    // LLVM doesn't have signed and unsigned 32-bit types, so u32 literals come
    // out as 'i32 -1'. Always treat those as max u32.
    if (maxRange === -1) {
        maxRange = 4294967295;
    }

    var shift = getShiftFromSize(size);

    var fromWireType = (value) => value;

    if (minRange === 0) {
        var bitshift = 32 - 8*size;
        fromWireType = (value) => (value << bitshift) >>> bitshift;
    }

    var isUnsignedType = (name.includes('unsigned'));
    var checkAssertions = (value, toTypeName) => {
#if ASSERTIONS
      if (typeof value != "number" && typeof value != "boolean") {
        throw new TypeError(`Cannot convert "${embindRepr(value)}" to ${toTypeName}`);
      }
      if (value < minRange || value > maxRange) {
        throw new TypeError(`Passing a number "${embindRepr(value)}" from JS side to C/C++ side to an argument of type "${name}", which is outside the valid range [${minRange}, ${maxRange}]!`);
      }
#endif
    }
    var toWireType;
    if (isUnsignedType) {
      toWireType = function(destructors, value) {
        checkAssertions(value, this.name);
        return value >>> 0;
      }
    } else {
      toWireType = function(destructors, value) {
        checkAssertions(value, this.name);
        // The VM will perform JS to Wasm value conversion, according to the spec:
        // https://www.w3.org/TR/wasm-js-api-1/#towebassemblyvalue
        return value;
      }
    }
    registerType(primitiveType, {
      name,
      'fromWireType': fromWireType,
      'toWireType': toWireType,
      'argPackAdvance': 8,
      'readValueFromPointer': integerReadValueFromPointer(name, shift, minRange !== 0),
      destructorFunction: null, // This type does not need a destructor
    });
  },

#if WASM_BIGINT
  _embind_register_bigint__deps: [
    '$embindRepr', '$readLatin1String', '$registerType', '$integerReadValueFromPointer'],
  _embind_register_bigint: function(primitiveType, name, size, minRange, maxRange) {
    name = readLatin1String(name);

    var shift = getShiftFromSize(size);

    var isUnsignedType = (name.indexOf('u') != -1);

    // maxRange comes through as -1 for uint64_t (see issue 13902). Work around that temporarily
    if (isUnsignedType) {
      maxRange = (1n << 64n) - 1n;
    }

    registerType(primitiveType, {
      name,
      'fromWireType': function (value) {
        return value;
      },
      'toWireType': function (destructors, value) {
        if (typeof value != "bigint" && typeof value != "number") {
          throw new TypeError(`Cannot convert "${embindRepr(value)}" to ${this.name}`);
        }
        if (value < minRange || value > maxRange) {
          throw new TypeError(`Passing a number "${embindRepr(value)}" from JS side to C/C++ side to an argument of type "${name}", which is outside the valid range [${minRange}, ${maxRange}]!`);
        }
        return value;
      },
      'argPackAdvance': 8,
      'readValueFromPointer': integerReadValueFromPointer(name, shift, !isUnsignedType),
      destructorFunction: null, // This type does not need a destructor
    });
  },
#else
  _embind_register_bigint__deps: [],
  _embind_register_bigint: function(primitiveType, name, size, minRange, maxRange) {},
#endif

  _embind_register_float__deps: [
    '$embindRepr', '$floatReadValueFromPointer', '$getShiftFromSize',
    '$readLatin1String', '$registerType'],
  _embind_register_float: function(rawType, name, size) {
    var shift = getShiftFromSize(size);
    name = readLatin1String(name);
    registerType(rawType, {
      name,
      'fromWireType': function(value) {
         return value;
      },
      'toWireType': function(destructors, value) {
#if ASSERTIONS
        if (typeof value != "number" && typeof value != "boolean") {
          throw new TypeError(`Cannot convert ${embindRepr(value)} to ${this.name}`);
        }
#endif
        // The VM will perform JS to Wasm value conversion, according to the spec:
        // https://www.w3.org/TR/wasm-js-api-1/#towebassemblyvalue
        return value;
      },
      'argPackAdvance': 8,
      'readValueFromPointer': floatReadValueFromPointer(name, shift),
      destructorFunction: null, // This type does not need a destructor
    });
  },

  // For types whose wire types are 32-bit pointers.
  $simpleReadValueFromPointer: function(pointer) {
    return this['fromWireType']({{{ makeGetValue('pointer', '0', 'i32') }}});
  },

  _embind_register_std_string__deps: [
    '$readLatin1String', '$registerType',
    '$simpleReadValueFromPointer', '$throwBindingError',
    '$stringToUTF8', '$lengthBytesUTF8', 'malloc', 'free'],
  _embind_register_std_string: function(rawType, name) {
    name = readLatin1String(name);
    var stdStringIsUTF8
#if EMBIND_STD_STRING_IS_UTF8
    //process only std::string bindings with UTF8 support, in contrast to e.g. std::basic_string<unsigned char>
    = (name === "std::string");
#else
    = false;
#endif

    registerType(rawType, {
      name,
      'fromWireType': function(value) {
        var length = {{{ makeGetValue('value', '0', SIZE_TYPE) }}};
        var payload = value + {{{ POINTER_SIZE }}};

        var str;
        if (stdStringIsUTF8) {
          var decodeStartPtr = payload;
          // Looping here to support possible embedded '0' bytes
          for (var i = 0; i <= length; ++i) {
            var currentBytePtr = payload + i;
            if (i == length || HEAPU8[currentBytePtr] == 0) {
              var maxRead = currentBytePtr - decodeStartPtr;
              var stringSegment = UTF8ToString(decodeStartPtr, maxRead);
              if (str === undefined) {
                str = stringSegment;
              } else {
                str += String.fromCharCode(0);
                str += stringSegment;
              }
              decodeStartPtr = currentBytePtr + 1;
            }
          }
        } else {
          var a = new Array(length);
          for (var i = 0; i < length; ++i) {
            a[i] = String.fromCharCode(HEAPU8[payload + i]);
          }
          str = a.join('');
        }

        _free(value);

        return str;
      },
      'toWireType': function(destructors, value) {
        if (value instanceof ArrayBuffer) {
          value = new Uint8Array(value);
        }

        var length;
        var valueIsOfTypeString = (typeof value == 'string');

        if (!(valueIsOfTypeString || value instanceof Uint8Array || value instanceof Uint8ClampedArray || value instanceof Int8Array)) {
          throwBindingError('Cannot pass non-string to std::string');
        }
        if (stdStringIsUTF8 && valueIsOfTypeString) {
          length = lengthBytesUTF8(value);
        } else {
          length = value.length;
        }

        // assumes 4-byte alignment
        var base = _malloc({{{ POINTER_SIZE }}} + length + 1);
        var ptr = base + {{{ POINTER_SIZE }}};
        {{{ makeSetValue('base', '0', 'length', SIZE_TYPE) }}};
        if (stdStringIsUTF8 && valueIsOfTypeString) {
          stringToUTF8(value, ptr, length + 1);
        } else {
          if (valueIsOfTypeString) {
            for (var i = 0; i < length; ++i) {
              var charCode = value.charCodeAt(i);
              if (charCode > 255) {
                _free(ptr);
                throwBindingError('String has UTF-16 code units that do not fit in 8 bits');
              }
              HEAPU8[ptr + i] = charCode;
            }
          } else {
            for (var i = 0; i < length; ++i) {
              HEAPU8[ptr + i] = value[i];
            }
          }
        }

        if (destructors !== null) {
          destructors.push(_free, base);
        }
        return base;
      },
      'argPackAdvance': 8,
      'readValueFromPointer': simpleReadValueFromPointer,
      destructorFunction: function(ptr) { _free(ptr); },
    });
  },

  _embind_register_std_wstring__deps: [
    '$readLatin1String', '$registerType', '$simpleReadValueFromPointer',
    '$UTF16ToString', '$stringToUTF16', '$lengthBytesUTF16',
    '$UTF32ToString', '$stringToUTF32', '$lengthBytesUTF32',
    ],
  _embind_register_std_wstring: function(rawType, charSize, name) {
    name = readLatin1String(name);
    var decodeString, encodeString, getHeap, lengthBytesUTF, shift;
    if (charSize === 2) {
      decodeString = UTF16ToString;
      encodeString = stringToUTF16;
      lengthBytesUTF = lengthBytesUTF16;
      getHeap = () => HEAPU16;
      shift = 1;
    } else if (charSize === 4) {
      decodeString = UTF32ToString;
      encodeString = stringToUTF32;
      lengthBytesUTF = lengthBytesUTF32;
      getHeap = () => HEAPU32;
      shift = 2;
    }
    registerType(rawType, {
      name,
      'fromWireType': function(value) {
        // Code mostly taken from _embind_register_std_string fromWireType
        var length = HEAPU32[value >> 2];
        var HEAP = getHeap();
        var str;

        var decodeStartPtr = value + 4;
        // Looping here to support possible embedded '0' bytes
        for (var i = 0; i <= length; ++i) {
          var currentBytePtr = value + 4 + i * charSize;
          if (i == length || HEAP[currentBytePtr >> shift] == 0) {
            var maxReadBytes = currentBytePtr - decodeStartPtr;
            var stringSegment = decodeString(decodeStartPtr, maxReadBytes);
            if (str === undefined) {
              str = stringSegment;
            } else {
              str += String.fromCharCode(0);
              str += stringSegment;
            }
            decodeStartPtr = currentBytePtr + charSize;
          }
        }

        _free(value);

        return str;
      },
      'toWireType': function(destructors, value) {
        if (!(typeof value == 'string')) {
          throwBindingError(`Cannot pass non-string to C++ string type ${name}`);
        }

        // assumes 4-byte alignment
        var length = lengthBytesUTF(value);
        var ptr = _malloc(4 + length + charSize);
        HEAPU32[ptr >> 2] = length >> shift;

        encodeString(value, ptr + 4, length + charSize);

        if (destructors !== null) {
          destructors.push(_free, ptr);
        }
        return ptr;
      },
      'argPackAdvance': 8,
      'readValueFromPointer': simpleReadValueFromPointer,
      destructorFunction: function(ptr) { _free(ptr); },
    });
  },

  _embind_register_emval__deps: [
    '_emval_decref', '$Emval',
    '$readLatin1String', '$registerType', '$simpleReadValueFromPointer'],
  _embind_register_emval: function(rawType, name) {
    name = readLatin1String(name);
    registerType(rawType, {
      name,
      'fromWireType': function(handle) {
        var rv = Emval.toValue(handle);
        __emval_decref(handle);
        return rv;
      },
      'toWireType': function(destructors, value) {
        return Emval.toHandle(value);
      },
      'argPackAdvance': 8,
      'readValueFromPointer': simpleReadValueFromPointer,
      destructorFunction: null, // This type does not need a destructor

      // TODO: do we need a deleteObject here?  write a test where
      // emval is passed into JS via an interface
    });
  },

  _embind_register_memory_view__deps: ['$readLatin1String', '$registerType'],
  _embind_register_memory_view: function(rawType, dataTypeIndex, name) {
    var typeMapping = [
      Int8Array,
      Uint8Array,
      Int16Array,
      Uint16Array,
      Int32Array,
      Uint32Array,
      Float32Array,
      Float64Array,
#if WASM_BIGINT
      BigInt64Array,
      BigUint64Array,
#endif
    ];

    var TA = typeMapping[dataTypeIndex];

    function decodeMemoryView(handle) {
      handle = handle >> 2;
      var heap = HEAPU32;
      var size = heap[handle]; // in elements
      var data = heap[handle + 1]; // byte offset into emscripten heap
      return new TA(heap.buffer, data, size);
    }

    name = readLatin1String(name);
    registerType(rawType, {
      name,
      'fromWireType': decodeMemoryView,
      'argPackAdvance': 8,
      'readValueFromPointer': decodeMemoryView,
    }, {
      ignoreDuplicateRegistrations: true,
    });
  },

  $runDestructors: function(destructors) {
    while (destructors.length) {
      var ptr = destructors.pop();
      var del = destructors.pop();
      del(ptr);
    }
  },

#if DYNAMIC_EXECUTION
  $newFunc__deps: ['$createNamedFunction'],
  $newFunc: function(constructor, argumentList) {
    if (!(constructor instanceof Function)) {
      throw new TypeError(`new_ called with constructor type ${typeof(constructor)} which is not a function`);
    }
    /*
     * Previously, the following line was just:
     *   function dummy() {};
     * Unfortunately, Chrome was preserving 'dummy' as the object's name, even
     * though at creation, the 'dummy' has the correct constructor name.  Thus,
     * objects created with IMVU.new would show up in the debugger as 'dummy',
     * which isn't very helpful.  Using IMVU.createNamedFunction addresses the
     * issue.  Doublely-unfortunately, there's no way to write a test for this
     * behavior.  -NRD 2013.02.22
     */
    var dummy = createNamedFunction(constructor.name || 'unknownFunctionName', function(){});
    dummy.prototype = constructor.prototype;
    var obj = new dummy;

    var r = constructor.apply(obj, argumentList);
    return (r instanceof Object) ? r : obj;
  },
#endif

  // The path to interop from JS code to C++ code:
  // (hand-written JS code) -> (autogenerated JS invoker) -> (template-generated C++ invoker) -> (target C++ function)
  // craftInvokerFunction generates the JS invoker function for each function exposed to JS through embind.
  $craftInvokerFunction__deps: [
    '$makeLegalFunctionName', '$runDestructors', '$throwBindingError',
#if DYNAMIC_EXECUTION
    '$newFunc',
#endif
#if ASYNCIFY
    '$Asyncify',
#endif
  ],
  $craftInvokerFunction: function(humanName, argTypes, classType, cppInvokerFunc, cppTargetFunc, /** boolean= */ isAsync) {
    // humanName: a human-readable string name for the function to be generated.
    // argTypes: An array that contains the embind type objects for all types in the function signature.
    //    argTypes[0] is the type object for the function return value.
    //    argTypes[1] is the type object for function this object/class type, or null if not crafting an invoker for a class method.
    //    argTypes[2...] are the actual function parameters.
    // classType: The embind type object for the class to be bound, or null if this is not a method of a class.
    // cppInvokerFunc: JS Function object to the C++-side function that interops into C++ code.
    // cppTargetFunc: Function pointer (an integer to FUNCTION_TABLE) to the target C++ function the cppInvokerFunc will end up calling.
    // isAsync: Optional. If true, returns an async function. Async bindings are only supported with JSPI.
    var argCount = argTypes.length;

    if (argCount < 2) {
      throwBindingError("argTypes array size mismatch! Must at least get return value and 'this' types!");
    }

#if ASSERTIONS && ASYNCIFY != 2
    assert(!isAsync, 'Async bindings are only supported with JSPI.');
#endif

    var isClassMethodFunc = (argTypes[1] !== null && classType !== null);

    // Free functions with signature "void function()" do not need an invoker that marshalls between wire types.
// TODO: This omits argument count check - enable only at -O3 or similar.
//    if (ENABLE_UNSAFE_OPTS && argCount == 2 && argTypes[0].name == "void" && !isClassMethodFunc) {
//       return FUNCTION_TABLE[fn];
//    }


    // Determine if we need to use a dynamic stack to store the destructors for the function parameters.
    // TODO: Remove this completely once all function invokers are being dynamically generated.
    var needsDestructorStack = false;

    for (var i = 1; i < argTypes.length; ++i) { // Skip return value at index 0 - it's not deleted here.
      if (argTypes[i] !== null && argTypes[i].destructorFunction === undefined) { // The type does not define a destructor function - must use dynamic stack
        needsDestructorStack = true;
        break;
      }
    }

    var returns = (argTypes[0].name !== "void");

#if DYNAMIC_EXECUTION == 0
    var expectedArgCount = argCount - 2;
    var argsWired = new Array(expectedArgCount);
    var invokerFuncArgs = [];
    var destructors = [];
    return function() {
      if (arguments.length !== expectedArgCount) {
        throwBindingError(`function ${humanName} called with ${arguments.length} arguments, expected ${expectedArgCount} args!`);
      }
#if EMSCRIPTEN_TRACING
      Module.emscripten_trace_enter_context(`embind::${humanName}`);
#endif
      destructors.length = 0;
      var thisWired;
      invokerFuncArgs.length = isClassMethodFunc ? 2 : 1;
      invokerFuncArgs[0] = cppTargetFunc;
      if (isClassMethodFunc) {
        thisWired = argTypes[1]['toWireType'](destructors, this);
        invokerFuncArgs[1] = thisWired;
      }
      for (var i = 0; i < expectedArgCount; ++i) {
        argsWired[i] = argTypes[i + 2]['toWireType'](destructors, arguments[i]);
        invokerFuncArgs.push(argsWired[i]);
      }

      var rv = cppInvokerFunc.apply(null, invokerFuncArgs);

      function onDone(rv) {
        if (needsDestructorStack) {
          runDestructors(destructors);
        } else {
          for (var i = isClassMethodFunc ? 1 : 2; i < argTypes.length; i++) {
            var param = i === 1 ? thisWired : argsWired[i - 2];
            if (argTypes[i].destructorFunction !== null) {
              argTypes[i].destructorFunction(param);
            }
          }
        }

  #if EMSCRIPTEN_TRACING
        Module.emscripten_trace_exit_context();
  #endif

        if (returns) {
          return argTypes[0]['fromWireType'](rv);
        }
      }

#if ASYNCIFY == 1
      if (Asyncify.currData) {
        return Asyncify.whenDone().then(onDone);
      }
#elif ASYNCIFY == 2
      if (isAsync) {
        return rv.then(onDone);
      }
#endif

      return onDone(rv);
    };
#else
    var argsList = "";
    var argsListWired = "";
    for (var i = 0; i < argCount - 2; ++i) {
      argsList += (i!==0?", ":"")+"arg"+i;
      argsListWired += (i!==0?", ":"")+"arg"+i+"Wired";
    }

    var invokerFnBody = `
      return function ${makeLegalFunctionName(humanName)}(${argsList}) {
      if (arguments.length !== ${argCount - 2}) {
        throwBindingError('function ${humanName} called with ${arguments.length} arguments, expected ${argCount - 2} args!');
      }`;

#if EMSCRIPTEN_TRACING
    invokerFnBody += `Module.emscripten_trace_enter_context('embind::${humanName}');\n`;
#endif

    if (needsDestructorStack) {
      invokerFnBody += "var destructors = [];\n";
    }

    var dtorStack = needsDestructorStack ? "destructors" : "null";
    var args1 = ["throwBindingError", "invoker", "fn", "runDestructors", "retType", "classParam"];
    var args2 = [throwBindingError, cppInvokerFunc, cppTargetFunc, runDestructors, argTypes[0], argTypes[1]];

#if EMSCRIPTEN_TRACING
    args1.push("Module");
    args2.push(Module);
#endif

    if (isClassMethodFunc) {
      invokerFnBody += "var thisWired = classParam.toWireType("+dtorStack+", this);\n";
    }

    for (var i = 0; i < argCount - 2; ++i) {
      invokerFnBody += "var arg"+i+"Wired = argType"+i+".toWireType("+dtorStack+", arg"+i+"); // "+argTypes[i+2].name+"\n";
      args1.push("argType"+i);
      args2.push(argTypes[i+2]);
    }

    if (isClassMethodFunc) {
      argsListWired = "thisWired" + (argsListWired.length > 0 ? ", " : "") + argsListWired;
    }

    invokerFnBody +=
        (returns || isAsync ? "var rv = ":"") + "invoker(fn"+(argsListWired.length>0?", ":"")+argsListWired+");\n";

#if ASYNCIFY == 1
    args1.push("Asyncify");
    args2.push(Asyncify);
#endif
#if ASYNCIFY
    invokerFnBody += "function onDone(" + (returns ? "rv" : "") + ") {\n";
#endif

    if (needsDestructorStack) {
      invokerFnBody += "runDestructors(destructors);\n";
    } else {
      for (var i = isClassMethodFunc?1:2; i < argTypes.length; ++i) { // Skip return value at index 0 - it's not deleted here. Also skip class type if not a method.
        var paramName = (i === 1 ? "thisWired" : ("arg"+(i - 2)+"Wired"));
        if (argTypes[i].destructorFunction !== null) {
          invokerFnBody += paramName+"_dtor("+paramName+"); // "+argTypes[i].name+"\n";
          args1.push(paramName+"_dtor");
          args2.push(argTypes[i].destructorFunction);
        }
      }
    }

    if (returns) {
      invokerFnBody += "var ret = retType.fromWireType(rv);\n" +
#if EMSCRIPTEN_TRACING
                       "Module.emscripten_trace_exit_context();\n" +
#endif
                       "return ret;\n";
    } else {
#if EMSCRIPTEN_TRACING
      invokerFnBody += "Module.emscripten_trace_exit_context();\n";
#endif
    }

#if ASYNCIFY == 1
    invokerFnBody += "}\n";
    invokerFnBody += "return Asyncify.currData ? Asyncify.whenDone().then(onDone) : onDone(" + (returns ? "rv" : "") +");\n"
#elif ASYNCIFY == 2
    invokerFnBody += "}\n";
    invokerFnBody += "return " + (isAsync ? "rv.then(onDone)" : "onDone(" + (returns ? "rv" : "") + ")") + ";";
#endif

    invokerFnBody += "}\n";

    args1.push(invokerFnBody);

    return newFunc(Function, args1).apply(null, args2);
#endif
  },

  $embind__requireFunction__deps: ['$readLatin1String', '$throwBindingError'
#if DYNCALLS || !WASM_BIGINT || MEMORY64
    , '$getDynCaller'
#endif
  ],
  $embind__requireFunction: function(signature, rawFunction) {
    signature = readLatin1String(signature);

    function makeDynCaller() {
#if DYNCALLS
      return getDynCaller(signature, rawFunction);
#else
#if !WASM_BIGINT
      if (signature.includes('j')) {
        return getDynCaller(signature, rawFunction);
      }
#elif MEMORY64
      if (signature.includes('p')) {
        return getDynCaller(signature, rawFunction);
      }
#endif
      return getWasmTableEntry(rawFunction);
#endif
    }

    var fp = makeDynCaller();
    if (typeof fp != "function") {
        throwBindingError(`unknown function pointer with signature ${signature}: ${rawFunction}`);
    }
    return fp;
  },

  _embind_register_function__deps: [
    '$craftInvokerFunction', '$exposePublicSymbol', '$heap32VectorToArray',
    '$readLatin1String', '$replacePublicSymbol', '$embind__requireFunction',
    '$throwUnboundTypeError', '$whenDependentTypesAreResolved'],
  _embind_register_function: function(name, argCount, rawArgTypesAddr, signature, rawInvoker, fn, isAsync) {
    var argTypes = heap32VectorToArray(argCount, rawArgTypesAddr);
    name = readLatin1String(name);

    rawInvoker = embind__requireFunction(signature, rawInvoker);

    exposePublicSymbol(name, function() {
      throwUnboundTypeError(`Cannot call ${name} due to unbound types`, argTypes);
    }, argCount - 1);

    whenDependentTypesAreResolved([], argTypes, function(argTypes) {
      var invokerArgsArray = [argTypes[0] /* return value */, null /* no class 'this'*/].concat(argTypes.slice(1) /* actual params */);
      replacePublicSymbol(name, craftInvokerFunction(name, invokerArgsArray, null /* no class 'this'*/, rawInvoker, fn, isAsync), argCount - 1);
      return [];
    });
  },

  _embind_register_value_array__deps: [
    '$tupleRegistrations', '$readLatin1String', '$embind__requireFunction'],
  _embind_register_value_array: function(
    rawType,
    name,
    constructorSignature,
    rawConstructor,
    destructorSignature,
    rawDestructor
  ) {
    tupleRegistrations[rawType] = {
      name: readLatin1String(name),
      rawConstructor: embind__requireFunction(constructorSignature, rawConstructor),
      rawDestructor: embind__requireFunction(destructorSignature, rawDestructor),
      elements: [],
    };
  },

  _embind_register_value_array_element__deps: [
    '$tupleRegistrations', '$embind__requireFunction'],
  _embind_register_value_array_element: function(
    rawTupleType,
    getterReturnType,
    getterSignature,
    getter,
    getterContext,
    setterArgumentType,
    setterSignature,
    setter,
    setterContext
  ) {
    tupleRegistrations[rawTupleType].elements.push({
      getterReturnType,
      getter: embind__requireFunction(getterSignature, getter),
      getterContext,
      setterArgumentType,
      setter: embind__requireFunction(setterSignature, setter),
      setterContext,
    });
  },

  _embind_finalize_value_array__deps: [
    '$tupleRegistrations', '$runDestructors',
    '$simpleReadValueFromPointer', '$whenDependentTypesAreResolved'],
  _embind_finalize_value_array: function(rawTupleType) {
    var reg = tupleRegistrations[rawTupleType];
    delete tupleRegistrations[rawTupleType];
    var elements = reg.elements;
    var elementsLength = elements.length;
    var elementTypes = elements.map(function(elt) { return elt.getterReturnType; }).
                concat(elements.map(function(elt) { return elt.setterArgumentType; }));

    var rawConstructor = reg.rawConstructor;
    var rawDestructor = reg.rawDestructor;

    whenDependentTypesAreResolved([rawTupleType], elementTypes, function(elementTypes) {
      elements.forEach((elt, i) => {
        var getterReturnType = elementTypes[i];
        var getter = elt.getter;
        var getterContext = elt.getterContext;
        var setterArgumentType = elementTypes[i + elementsLength];
        var setter = elt.setter;
        var setterContext = elt.setterContext;
        elt.read = (ptr) => {
          return getterReturnType['fromWireType'](getter(getterContext, ptr));
        };
        elt.write = (ptr, o) => {
          var destructors = [];
          setter(setterContext, ptr, setterArgumentType['toWireType'](destructors, o));
          runDestructors(destructors);
        };
      });

      return [{
        name: reg.name,
        'fromWireType': function(ptr) {
          var rv = new Array(elementsLength);
          for (var i = 0; i < elementsLength; ++i) {
            rv[i] = elements[i].read(ptr);
          }
          rawDestructor(ptr);
          return rv;
        },
        'toWireType': function(destructors, o) {
          if (elementsLength !== o.length) {
            throw new TypeError(`Incorrect number of tuple elements for ${reg.name}: expected=${elementsLength}, actual=${o.length}`);
          }
          var ptr = rawConstructor();
          for (var i = 0; i < elementsLength; ++i) {
            elements[i].write(ptr, o[i]);
          }
          if (destructors !== null) {
            destructors.push(rawDestructor, ptr);
          }
          return ptr;
        },
        'argPackAdvance': 8,
        'readValueFromPointer': simpleReadValueFromPointer,
        destructorFunction: rawDestructor,
      }];
    });
  },

  _embind_register_value_object__deps: [
    '$structRegistrations', '$readLatin1String', '$embind__requireFunction'],
  _embind_register_value_object: function(
    rawType,
    name,
    constructorSignature,
    rawConstructor,
    destructorSignature,
    rawDestructor
  ) {
    structRegistrations[rawType] = {
      name: readLatin1String(name),
      rawConstructor: embind__requireFunction(constructorSignature, rawConstructor),
      rawDestructor: embind__requireFunction(destructorSignature, rawDestructor),
      fields: [],
    };
  },

  _embind_register_value_object_field__deps: [
    '$structRegistrations', '$readLatin1String', '$embind__requireFunction'],
  _embind_register_value_object_field: function(
    structType,
    fieldName,
    getterReturnType,
    getterSignature,
    getter,
    getterContext,
    setterArgumentType,
    setterSignature,
    setter,
    setterContext
  ) {
    structRegistrations[structType].fields.push({
      fieldName: readLatin1String(fieldName),
      getterReturnType,
      getter: embind__requireFunction(getterSignature, getter),
      getterContext,
      setterArgumentType,
      setter: embind__requireFunction(setterSignature, setter),
      setterContext,
    });
  },

  _embind_finalize_value_object__deps: [
    '$structRegistrations', '$runDestructors',
    '$simpleReadValueFromPointer', '$whenDependentTypesAreResolved'],
  _embind_finalize_value_object: function(structType) {
    var reg = structRegistrations[structType];
    delete structRegistrations[structType];

    var rawConstructor = reg.rawConstructor;
    var rawDestructor = reg.rawDestructor;
    var fieldRecords = reg.fields;
    var fieldTypes = fieldRecords.map((field) => field.getterReturnType).
              concat(fieldRecords.map((field) => field.setterArgumentType));
    whenDependentTypesAreResolved([structType], fieldTypes, (fieldTypes) => {
      var fields = {};
      fieldRecords.forEach((field, i) => {
        var fieldName = field.fieldName;
        var getterReturnType = fieldTypes[i];
        var getter = field.getter;
        var getterContext = field.getterContext;
        var setterArgumentType = fieldTypes[i + fieldRecords.length];
        var setter = field.setter;
        var setterContext = field.setterContext;
        fields[fieldName] = {
          read: (ptr) => {
            return getterReturnType['fromWireType'](
                getter(getterContext, ptr));
          },
          write: (ptr, o) => {
            var destructors = [];
            setter(setterContext, ptr, setterArgumentType['toWireType'](destructors, o));
            runDestructors(destructors);
          }
        };
      });

      return [{
        name: reg.name,
        'fromWireType': function(ptr) {
          var rv = {};
          for (var i in fields) {
            rv[i] = fields[i].read(ptr);
          }
          rawDestructor(ptr);
          return rv;
        },
        'toWireType': function(destructors, o) {
          // todo: Here we have an opportunity for -O3 level "unsafe" optimizations:
          // assume all fields are present without checking.
          for (var fieldName in fields) {
            if (!(fieldName in o)) {
              throw new TypeError(`Missing field: "${fieldName}"`);
            }
          }
          var ptr = rawConstructor();
          for (fieldName in fields) {
            fields[fieldName].write(ptr, o[fieldName]);
          }
          if (destructors !== null) {
            destructors.push(rawDestructor, ptr);
          }
          return ptr;
        },
        'argPackAdvance': 8,
        'readValueFromPointer': simpleReadValueFromPointer,
        destructorFunction: rawDestructor,
      }];
    });
  },

  $genericPointerToWireType__deps: ['$throwBindingError', '$upcastPointer'],
  $genericPointerToWireType: function(destructors, handle) {
    var ptr;
    if (handle === null) {
      if (this.isReference) {
        throwBindingError(`null is not a valid ${this.name}`);
      }

      if (this.isSmartPointer) {
        ptr = this.rawConstructor();
        if (destructors !== null) {
          destructors.push(this.rawDestructor, ptr);
        }
        return ptr;
      } else {
        return 0;
      }
    }

    if (!handle.$$) {
      throwBindingError(`Cannot pass "${embindRepr(handle)}" as a ${this.name}`);
    }
    if (!handle.$$.ptr) {
      throwBindingError(`Cannot pass deleted object as a pointer of type ${this.name}`);
    }
    if (!this.isConst && handle.$$.ptrType.isConst) {
      throwBindingError(`Cannot convert argument of type ${(handle.$$.smartPtrType ? handle.$$.smartPtrType.name : handle.$$.ptrType.name)} to parameter type ${this.name}`);
    }
    var handleClass = handle.$$.ptrType.registeredClass;
    ptr = upcastPointer(handle.$$.ptr, handleClass, this.registeredClass);

    if (this.isSmartPointer) {
      // TODO: this is not strictly true
      // We could support BY_EMVAL conversions from raw pointers to smart pointers
      // because the smart pointer can hold a reference to the handle
      if (undefined === handle.$$.smartPtr) {
        throwBindingError('Passing raw pointer to smart pointer is illegal');
      }

      switch (this.sharingPolicy) {
        case 0: // NONE
          // no upcasting
          if (handle.$$.smartPtrType === this) {
            ptr = handle.$$.smartPtr;
          } else {
            throwBindingError(`Cannot convert argument of type ${(handle.$$.smartPtrType ? handle.$$.smartPtrType.name : handle.$$.ptrType.name)} to parameter type ${this.name}`);
          }
          break;

        case 1: // INTRUSIVE
          ptr = handle.$$.smartPtr;
          break;

        case 2: // BY_EMVAL
          if (handle.$$.smartPtrType === this) {
            ptr = handle.$$.smartPtr;
          } else {
            var clonedHandle = handle['clone']();
            ptr = this.rawShare(
              ptr,
              Emval.toHandle(function() {
                clonedHandle['delete']();
              })
            );
            if (destructors !== null) {
              destructors.push(this.rawDestructor, ptr);
            }
          }
          break;

        default:
          throwBindingError('Unsupporting sharing policy');
      }
    }
    return ptr;
  },

  // If we know a pointer type is not going to have SmartPtr logic in it, we can
  // special-case optimize it a bit (compare to genericPointerToWireType)
  $constNoSmartPtrRawPointerToWireType__deps: ['$throwBindingError', '$upcastPointer'],
  $constNoSmartPtrRawPointerToWireType: function(destructors, handle) {
    if (handle === null) {
      if (this.isReference) {
        throwBindingError(`null is not a valid ${this.name}`);
      }
      return 0;
    }

    if (!handle.$$) {
      throwBindingError(`Cannot pass "${embindRepr(handle)}" as a ${this.name}`);
    }
    if (!handle.$$.ptr) {
      throwBindingError(`Cannot pass deleted object as a pointer of type ${this.name}`);
    }
    var handleClass = handle.$$.ptrType.registeredClass;
    var ptr = upcastPointer(handle.$$.ptr, handleClass, this.registeredClass);
    return ptr;
  },

  // An optimized version for non-const method accesses - there we must additionally restrict that
  // the pointer is not a const-pointer.
  $nonConstNoSmartPtrRawPointerToWireType__deps: ['$throwBindingError', '$upcastPointer'],
  $nonConstNoSmartPtrRawPointerToWireType: function(destructors, handle) {
    if (handle === null) {
      if (this.isReference) {
        throwBindingError(`null is not a valid ${this.name}`);
      }
      return 0;
    }

    if (!handle.$$) {
      throwBindingError(`Cannot pass "${embindRepr(handle)}" as a ${this.name}`);
    }
    if (!handle.$$.ptr) {
      throwBindingError(`Cannot pass deleted object as a pointer of type ${this.name}`);
    }
    if (handle.$$.ptrType.isConst) {
        throwBindingError(`Cannot convert argument of type ${handle.$$.ptrType.name} to parameter type ${this.name}`);
    }
    var handleClass = handle.$$.ptrType.registeredClass;
    var ptr = upcastPointer(handle.$$.ptr, handleClass, this.registeredClass);
    return ptr;
  },

  $init_RegisteredPointer__deps: [
    '$simpleReadValueFromPointer',
    '$RegisteredPointer_getPointee', '$RegisteredPointer_destructor',
    '$RegisteredPointer_deleteObject', '$RegisteredPointer_fromWireType'],
  $init_RegisteredPointer: function() {
    RegisteredPointer.prototype.getPointee = RegisteredPointer_getPointee;
    RegisteredPointer.prototype.destructor = RegisteredPointer_destructor;
    RegisteredPointer.prototype['argPackAdvance'] = 8;
    RegisteredPointer.prototype['readValueFromPointer'] = simpleReadValueFromPointer;
    RegisteredPointer.prototype['deleteObject'] = RegisteredPointer_deleteObject;
    RegisteredPointer.prototype['fromWireType'] = RegisteredPointer_fromWireType;
  },

  $RegisteredPointer__docs: `/** @constructor
    @param {*=} pointeeType,
    @param {*=} sharingPolicy,
    @param {*=} rawGetPointee,
    @param {*=} rawConstructor,
    @param {*=} rawShare,
    @param {*=} rawDestructor,
     */`,
  $RegisteredPointer__deps: [
    '$constNoSmartPtrRawPointerToWireType', '$genericPointerToWireType',
    '$nonConstNoSmartPtrRawPointerToWireType', '$init_RegisteredPointer'],
  $RegisteredPointer__postset: 'init_RegisteredPointer()',
  $RegisteredPointer: function(
    name,
    registeredClass,
    isReference,
    isConst,

    // smart pointer properties
    isSmartPointer,
    pointeeType,
    sharingPolicy,
    rawGetPointee,
    rawConstructor,
    rawShare,
    rawDestructor
  ) {
    this.name = name;
    this.registeredClass = registeredClass;
    this.isReference = isReference;
    this.isConst = isConst;

    // smart pointer properties
    this.isSmartPointer = isSmartPointer;
    this.pointeeType = pointeeType;
    this.sharingPolicy = sharingPolicy;
    this.rawGetPointee = rawGetPointee;
    this.rawConstructor = rawConstructor;
    this.rawShare = rawShare;
    this.rawDestructor = rawDestructor;

    if (!isSmartPointer && registeredClass.baseClass === undefined) {
      if (isConst) {
        this['toWireType'] = constNoSmartPtrRawPointerToWireType;
        this.destructorFunction = null;
      } else {
        this['toWireType'] = nonConstNoSmartPtrRawPointerToWireType;
        this.destructorFunction = null;
      }
    } else {
      this['toWireType'] = genericPointerToWireType;
      // Here we must leave this.destructorFunction undefined, since whether genericPointerToWireType returns
      // a pointer that needs to be freed up is runtime-dependent, and cannot be evaluated at registration time.
      // TODO: Create an alternative mechanism that allows removing the use of var destructors = []; array in
      //       craftInvokerFunction altogether.
    }
  },

  $RegisteredPointer_getPointee: function(ptr) {
    if (this.rawGetPointee) {
      ptr = this.rawGetPointee(ptr);
    }
    return ptr;
  },

  $RegisteredPointer_destructor: function(ptr) {
    if (this.rawDestructor) {
      this.rawDestructor(ptr);
    }
  },

  $RegisteredPointer_deleteObject: function(handle) {
    if (handle !== null) {
      handle['delete']();
    }
  },

  $RegisteredPointer_fromWireType__deps: [
    '$downcastPointer', '$registeredPointers',
    '$getInheritedInstance', '$makeClassHandle',
#if MEMORY64
    '$bigintToI53Checked'
#endif
  ],
  $RegisteredPointer_fromWireType: function(ptr) {
    // ptr is a raw pointer (or a raw smartpointer)
#if MEMORY64
    ptr = bigintToI53Checked(ptr);
#if ASSERTIONS
    assert(Number.isSafeInteger(ptr));
#endif
#endif

    // rawPointer is a maybe-null raw pointer
    var rawPointer = this.getPointee(ptr);
    if (!rawPointer) {
      this.destructor(ptr);
      return null;
    }

    var registeredInstance = getInheritedInstance(this.registeredClass, rawPointer);
    if (undefined !== registeredInstance) {
      // JS object has been neutered, time to repopulate it
      if (0 === registeredInstance.$$.count.value) {
        registeredInstance.$$.ptr = rawPointer;
        registeredInstance.$$.smartPtr = ptr;
        return registeredInstance['clone']();
      } else {
        // else, just increment reference count on existing object
        // it already has a reference to the smart pointer
        var rv = registeredInstance['clone']();
        this.destructor(ptr);
        return rv;
      }
    }

    function makeDefaultHandle() {
      if (this.isSmartPointer) {
        return makeClassHandle(this.registeredClass.instancePrototype, {
          ptrType: this.pointeeType,
          ptr: rawPointer,
          smartPtrType: this,
          smartPtr: ptr,
        });
      } else {
        return makeClassHandle(this.registeredClass.instancePrototype, {
          ptrType: this,
          ptr,
        });
      }
    }

    var actualType = this.registeredClass.getActualType(rawPointer);
    var registeredPointerRecord = registeredPointers[actualType];
    if (!registeredPointerRecord) {
      return makeDefaultHandle.call(this);
    }

    var toType;
    if (this.isConst) {
      toType = registeredPointerRecord.constPointerType;
    } else {
      toType = registeredPointerRecord.pointerType;
    }
    var dp = downcastPointer(
        rawPointer,
        this.registeredClass,
        toType.registeredClass);
    if (dp === null) {
      return makeDefaultHandle.call(this);
    }
    if (this.isSmartPointer) {
      return makeClassHandle(toType.registeredClass.instancePrototype, {
        ptrType: toType,
        ptr: dp,
        smartPtrType: this,
        smartPtr: ptr,
      });
    } else {
      return makeClassHandle(toType.registeredClass.instancePrototype, {
        ptrType: toType,
        ptr: dp,
      });
    }
  },

  $runDestructor: function($$) {
    if ($$.smartPtr) {
      $$.smartPtrType.rawDestructor($$.smartPtr);
    } else {
      $$.ptrType.registeredClass.rawDestructor($$.ptr);
    }
  },

  $releaseClassHandle__deps: ['$runDestructor'],
  $releaseClassHandle: function($$) {
    $$.count.value -= 1;
    var toDelete = 0 === $$.count.value;
    if (toDelete) {
      runDestructor($$);
    }
  },

  $finalizationRegistry: false,

  $detachFinalizer_deps: ['$finalizationRegistry'],
  $detachFinalizer: function(handle) {},

  $attachFinalizer__deps: ['$finalizationRegistry', '$detachFinalizer',
                           '$releaseClassHandle', '$RegisteredPointer_fromWireType'],
  $attachFinalizer: function(handle) {
    if ('undefined' === typeof FinalizationRegistry) {
      attachFinalizer = (handle) => handle;
      return handle;
    }
    // If the running environment has a FinalizationRegistry (see
    // https://github.com/tc39/proposal-weakrefs), then attach finalizers
    // for class handles.  We check for the presence of FinalizationRegistry
    // at run-time, not build-time.
    finalizationRegistry = new FinalizationRegistry((info) => {
#if ASSERTIONS
      console.warn(info.leakWarning.stack.replace(/^Error: /, ''));
#endif
      releaseClassHandle(info.$$);
    });
    attachFinalizer = (handle) => {
      var $$ = handle.$$;
      var hasSmartPtr = !!$$.smartPtr;
      if (hasSmartPtr) {
        // We should not call the destructor on raw pointers in case other code expects the pointee to live
        var info = { $$: $$ };
#if ASSERTIONS
        // Create a warning as an Error instance in advance so that we can store
        // the current stacktrace and point to it when / if a leak is detected.
        // This is more useful than the empty stacktrace of `FinalizationRegistry`
        // callback.
        var cls = $$.ptrType.registeredClass;
        info.leakWarning = new Error(`Embind found a leaked C++ instance ${cls.name} <${ptrToString($$.ptr)}>.\n` +
        "We'll free it automatically in this case, but this functionality is not reliable across various environments.\n" +
        "Make sure to invoke .delete() manually once you're done with the instance instead.\n" +
        "Originally allocated"); // `.stack` will add "at ..." after this sentence
        if ('captureStackTrace' in Error) {
          Error.captureStackTrace(info.leakWarning, RegisteredPointer_fromWireType);
        }
#endif
        finalizationRegistry.register(handle, info, handle);
      }
      return handle;
    };
    detachFinalizer = (handle) => finalizationRegistry.unregister(handle);
    return attachFinalizer(handle);
  },

  $makeClassHandle__deps: ['$throwInternalError', '$attachFinalizer'],
  $makeClassHandle: function(prototype, record) {
    if (!record.ptrType || !record.ptr) {
      throwInternalError('makeClassHandle requires ptr and ptrType');
    }
    var hasSmartPtrType = !!record.smartPtrType;
    var hasSmartPtr = !!record.smartPtr;
    if (hasSmartPtrType !== hasSmartPtr) {
      throwInternalError('Both smartPtrType and smartPtr must be specified');
    }
    record.count = { value: 1 };
    return attachFinalizer(Object.create(prototype, {
      $$: {
          value: record,
      },
    }));
  },

  $init_ClassHandle__deps: [
    '$ClassHandle_isAliasOf', '$ClassHandle_clone', '$ClassHandle_delete',
    '$ClassHandle_isDeleted', '$ClassHandle_deleteLater'],
  $init_ClassHandle: function() {
    ClassHandle.prototype['isAliasOf'] = ClassHandle_isAliasOf;
    ClassHandle.prototype['clone'] = ClassHandle_clone;
    ClassHandle.prototype['delete'] = ClassHandle_delete;
    ClassHandle.prototype['isDeleted'] = ClassHandle_isDeleted;
    ClassHandle.prototype['deleteLater'] = ClassHandle_deleteLater;
  },

  $ClassHandle__deps: ['$init_ClassHandle'],
  $ClassHandle__postset: 'init_ClassHandle()',
  // root of all pointer and smart pointer handles in embind
  $ClassHandle: function() {
  },

  $ClassHandle_isAliasOf: function(other) {
    if (!(this instanceof ClassHandle)) {
      return false;
    }
    if (!(other instanceof ClassHandle)) {
      return false;
    }

    var leftClass = this.$$.ptrType.registeredClass;
    var left = this.$$.ptr;
    var rightClass = other.$$.ptrType.registeredClass;
    var right = other.$$.ptr;

    while (leftClass.baseClass) {
      left = leftClass.upcast(left);
      leftClass = leftClass.baseClass;
    }

    while (rightClass.baseClass) {
      right = rightClass.upcast(right);
      rightClass = rightClass.baseClass;
    }

    return leftClass === rightClass && left === right;
  },

  $throwInstanceAlreadyDeleted__deps: ['$throwBindingError'],
  $throwInstanceAlreadyDeleted: function(obj) {
    function getInstanceTypeName(handle) {
      return handle.$$.ptrType.registeredClass.name;
    }
    throwBindingError(getInstanceTypeName(obj) + ' instance already deleted');
  },

  $ClassHandle_clone__deps: ['$shallowCopyInternalPointer', '$throwInstanceAlreadyDeleted', '$attachFinalizer'],
  $ClassHandle_clone: function() {
    if (!this.$$.ptr) {
      throwInstanceAlreadyDeleted(this);
    }

    if (this.$$.preservePointerOnDelete) {
      this.$$.count.value += 1;
      return this;
    } else {
      var clone = attachFinalizer(Object.create(Object.getPrototypeOf(this), {
        $$: {
          value: shallowCopyInternalPointer(this.$$),
        }
      }));

      clone.$$.count.value += 1;
      clone.$$.deleteScheduled = false;
      return clone;
    }
  },

  $ClassHandle_delete__deps: ['$releaseClassHandle', '$throwBindingError',
                              '$detachFinalizer', '$throwInstanceAlreadyDeleted'],
  $ClassHandle_delete: function() {
    if (!this.$$.ptr) {
      throwInstanceAlreadyDeleted(this);
    }

    if (this.$$.deleteScheduled && !this.$$.preservePointerOnDelete) {
      throwBindingError('Object already scheduled for deletion');
    }

    detachFinalizer(this);
    releaseClassHandle(this.$$);

    if (!this.$$.preservePointerOnDelete) {
      this.$$.smartPtr = undefined;
      this.$$.ptr = undefined;
    }
  },

  $deletionQueue: [],

  $ClassHandle_isDeleted: function() {
    return !this.$$.ptr;
  },

  $ClassHandle_deleteLater__deps: [
    '$delayFunction', '$deletionQueue', '$flushPendingDeletes'],
  $ClassHandle_deleteLater: function() {
    if (!this.$$.ptr) {
      throwInstanceAlreadyDeleted(this);
    }
    if (this.$$.deleteScheduled && !this.$$.preservePointerOnDelete) {
      throwBindingError('Object already scheduled for deletion');
    }
    deletionQueue.push(this);
    if (deletionQueue.length === 1 && delayFunction) {
      delayFunction(flushPendingDeletes);
    }
    this.$$.deleteScheduled = true;
    return this;
  },

  $flushPendingDeletes__deps: ['$deletionQueue'],
  $flushPendingDeletes: function() {
    while (deletionQueue.length) {
      var obj = deletionQueue.pop();
      obj.$$.deleteScheduled = false;
      obj['delete']();
    }
  },

  $delayFunction: undefined,

  $setDelayFunction__deps: ['$delayFunction', '$deletionQueue', '$flushPendingDeletes'],
  $setDelayFunction: function(fn) {
    delayFunction = fn;
    if (deletionQueue.length && delayFunction) {
      delayFunction(flushPendingDeletes);
    }
  },

  $RegisteredClass__docs: '/** @constructor */',
  $RegisteredClass: function(name,
                             constructor,
                             instancePrototype,
                             rawDestructor,
                             baseClass,
                             getActualType,
                             upcast,
                             downcast) {
    this.name = name;
    this.constructor = constructor;
    this.instancePrototype = instancePrototype;
    this.rawDestructor = rawDestructor;
    this.baseClass = baseClass;
    this.getActualType = getActualType;
    this.upcast = upcast;
    this.downcast = downcast;
    this.pureVirtualFunctions = [];
  },

  $shallowCopyInternalPointer: function(o) {
    return {
      count: o.count,
      deleteScheduled: o.deleteScheduled,
      preservePointerOnDelete: o.preservePointerOnDelete,
      ptr: o.ptr,
      ptrType: o.ptrType,
      smartPtr: o.smartPtr,
      smartPtrType: o.smartPtrType,
    };
  },

  _embind_register_class__deps: [
    '$BindingError', '$ClassHandle', '$createNamedFunction',
    '$registeredPointers', '$exposePublicSymbol',
    '$makeLegalFunctionName', '$readLatin1String',
    '$RegisteredClass', '$RegisteredPointer', '$replacePublicSymbol',
    '$embind__requireFunction', '$throwUnboundTypeError',
    '$whenDependentTypesAreResolved'],
  _embind_register_class: function(rawType,
                                   rawPointerType,
                                   rawConstPointerType,
                                   baseClassRawType,
                                   getActualTypeSignature,
                                   getActualType,
                                   upcastSignature,
                                   upcast,
                                   downcastSignature,
                                   downcast,
                                   name,
                                   destructorSignature,
                                   rawDestructor) {
    name = readLatin1String(name);
    getActualType = embind__requireFunction(getActualTypeSignature, getActualType);
    if (upcast) {
      upcast = embind__requireFunction(upcastSignature, upcast);
    }
    if (downcast) {
      downcast = embind__requireFunction(downcastSignature, downcast);
    }
    rawDestructor = embind__requireFunction(destructorSignature, rawDestructor);
    var legalFunctionName = makeLegalFunctionName(name);

    exposePublicSymbol(legalFunctionName, function() {
      // this code cannot run if baseClassRawType is zero
      throwUnboundTypeError(`Cannot construct ${name} due to unbound types`, [baseClassRawType]);
    });

    whenDependentTypesAreResolved(
      [rawType, rawPointerType, rawConstPointerType],
      baseClassRawType ? [baseClassRawType] : [],
      function(base) {
        base = base[0];

        var baseClass;
        var basePrototype;
        if (baseClassRawType) {
          baseClass = base.registeredClass;
          basePrototype = baseClass.instancePrototype;
        } else {
          basePrototype = ClassHandle.prototype;
        }

        var constructor = createNamedFunction(legalFunctionName, function() {
          if (Object.getPrototypeOf(this) !== instancePrototype) {
            throw new BindingError("Use 'new' to construct " + name);
          }
          if (undefined === registeredClass.constructor_body) {
            throw new BindingError(name + " has no accessible constructor");
          }
          var body = registeredClass.constructor_body[arguments.length];
          if (undefined === body) {
            throw new BindingError(`Tried to invoke ctor of ${name} with invalid number of parameters (${arguments.length}) - expected (${Object.keys(registeredClass.constructor_body).toString()}) parameters instead!`);
          }
          return body.apply(this, arguments);
        });

        var instancePrototype = Object.create(basePrototype, {
          constructor: { value: constructor },
        });

        constructor.prototype = instancePrototype;

        var registeredClass = new RegisteredClass(name,
                                                  constructor,
                                                  instancePrototype,
                                                  rawDestructor,
                                                  baseClass,
                                                  getActualType,
                                                  upcast,
                                                  downcast);

        if (registeredClass.baseClass) {
          // Keep track of class hierarchy. Used to allow sub-classes to inherit class functions.
          if (registeredClass.baseClass.__derivedClasses === undefined) {
            registeredClass.baseClass.__derivedClasses = [];
          }

          registeredClass.baseClass.__derivedClasses.push(registeredClass);
        }

        var referenceConverter = new RegisteredPointer(name,
                                                       registeredClass,
                                                       true,
                                                       false,
                                                       false);

        var pointerConverter = new RegisteredPointer(name + '*',
                                                     registeredClass,
                                                     false,
                                                     false,
                                                     false);

        var constPointerConverter = new RegisteredPointer(name + ' const*',
                                                          registeredClass,
                                                          false,
                                                          true,
                                                          false);

        registeredPointers[rawType] = {
          pointerType: pointerConverter,
          constPointerType: constPointerConverter
        };

        replacePublicSymbol(legalFunctionName, constructor);

        return [referenceConverter, pointerConverter, constPointerConverter];
      }
    );
  },

  _embind_register_class_constructor__deps: [
    '$heap32VectorToArray', '$embind__requireFunction', '$runDestructors',
    '$throwBindingError', '$whenDependentTypesAreResolved', '$registeredTypes',
    '$craftInvokerFunction'],
  _embind_register_class_constructor: function(
    rawClassType,
    argCount,
    rawArgTypesAddr,
    invokerSignature,
    invoker,
    rawConstructor
  ) {
#if ASSERTIONS
    assert(argCount > 0);
#endif
    var rawArgTypes = heap32VectorToArray(argCount, rawArgTypesAddr);
    invoker = embind__requireFunction(invokerSignature, invoker);
    var args = [rawConstructor];
    var destructors = [];

    whenDependentTypesAreResolved([], [rawClassType], function(classType) {
      classType = classType[0];
      var humanName = `constructor ${classType.name}`;

      if (undefined === classType.registeredClass.constructor_body) {
        classType.registeredClass.constructor_body = [];
      }
      if (undefined !== classType.registeredClass.constructor_body[argCount - 1]) {
        throw new BindingError(`Cannot register multiple constructors with identical number of parameters (${argCount-1}) for class '${classType.name}'! Overload resolution is currently only performed using the parameter count, not actual type info!`);
      }
      classType.registeredClass.constructor_body[argCount - 1] = () => {
        throwUnboundTypeError(`Cannot construct ${classType.name} due to unbound types`, rawArgTypes);
      };

      whenDependentTypesAreResolved([], rawArgTypes, function(argTypes) {
        // Insert empty slot for context type (argTypes[1]).
        argTypes.splice(1, 0, null);
        classType.registeredClass.constructor_body[argCount - 1] = craftInvokerFunction(humanName, argTypes, null, invoker, rawConstructor);
        return [];
      });
      return [];
    });
  },

  $downcastPointer: function(ptr, ptrClass, desiredClass) {
    if (ptrClass === desiredClass) {
      return ptr;
    }
    if (undefined === desiredClass.baseClass) {
      return null; // no conversion
    }

    var rv = downcastPointer(ptr, ptrClass, desiredClass.baseClass);
    if (rv === null) {
      return null;
    }
    return desiredClass.downcast(rv);
  },

  $upcastPointer__deps: ['$throwBindingError'],
  $upcastPointer: function(ptr, ptrClass, desiredClass) {
    while (ptrClass !== desiredClass) {
      if (!ptrClass.upcast) {
        throwBindingError(`Expected null or instance of ${desiredClass.name}, got an instance of ${ptrClass.name}`);
      }
      ptr = ptrClass.upcast(ptr);
      ptrClass = ptrClass.baseClass;
    }
    return ptr;
  },

  $validateThis__deps: ['$throwBindingError', '$upcastPointer'],
  $validateThis: function(this_, classType, humanName) {
    if (!(this_ instanceof Object)) {
      throwBindingError(`${humanName} with invalid "this": ${this_}`);
    }
    if (!(this_ instanceof classType.registeredClass.constructor)) {
      throwBindingError(`${humanName} incompatible with "this" of type ${this_.constructor.name}`);
    }
    if (!this_.$$.ptr) {
      throwBindingError(`cannot call emscripten binding method ${humanName} on deleted object`);
    }

    // todo: kill this
    return upcastPointer(this_.$$.ptr,
                         this_.$$.ptrType.registeredClass,
                         classType.registeredClass);
  },

  _embind_register_class_function__deps: [
    '$craftInvokerFunction', '$heap32VectorToArray', '$readLatin1String',
    '$embind__requireFunction', '$throwUnboundTypeError',
    '$whenDependentTypesAreResolved'],
  _embind_register_class_function: function(rawClassType,
                                            methodName,
                                            argCount,
                                            rawArgTypesAddr, // [ReturnType, ThisType, Args...]
                                            invokerSignature,
                                            rawInvoker,
                                            context,
                                            isPureVirtual,
                                            isAsync) {
    var rawArgTypes = heap32VectorToArray(argCount, rawArgTypesAddr);
    methodName = readLatin1String(methodName);
    rawInvoker = embind__requireFunction(invokerSignature, rawInvoker);

    whenDependentTypesAreResolved([], [rawClassType], function(classType) {
      classType = classType[0];
      var humanName = `${classType.name}.${methodName}`;

      if (methodName.startsWith("@@")) {
        methodName = Symbol[methodName.substring(2)];
      }

      if (isPureVirtual) {
        classType.registeredClass.pureVirtualFunctions.push(methodName);
      }

      function unboundTypesHandler() {
        throwUnboundTypeError(`Cannot call ${humanName} due to unbound types`, rawArgTypes);
      }

      var proto = classType.registeredClass.instancePrototype;
      var method = proto[methodName];
      if (undefined === method || (undefined === method.overloadTable && method.className !== classType.name && method.argCount === argCount - 2)) {
        // This is the first overload to be registered, OR we are replacing a
        // function in the base class with a function in the derived class.
        unboundTypesHandler.argCount = argCount - 2;
        unboundTypesHandler.className = classType.name;
        proto[methodName] = unboundTypesHandler;
      } else {
        // There was an existing function with the same name registered. Set up
        // a function overload routing table.
        ensureOverloadTable(proto, methodName, humanName);
        proto[methodName].overloadTable[argCount - 2] = unboundTypesHandler;
      }

      whenDependentTypesAreResolved([], rawArgTypes, function(argTypes) {
        var memberFunction = craftInvokerFunction(humanName, argTypes, classType, rawInvoker, context, isAsync);

        // Replace the initial unbound-handler-stub function with the appropriate member function, now that all types
        // are resolved. If multiple overloads are registered for this function, the function goes into an overload table.
        if (undefined === proto[methodName].overloadTable) {
          // Set argCount in case an overload is registered later
          memberFunction.argCount = argCount - 2;
          proto[methodName] = memberFunction;
        } else {
          proto[methodName].overloadTable[argCount - 2] = memberFunction;
        }

        return [];
      });
      return [];
    });
  },

  _embind_register_class_property__deps: [
    '$readLatin1String', '$embind__requireFunction', '$runDestructors',
    '$throwBindingError', '$throwUnboundTypeError',
    '$whenDependentTypesAreResolved', '$validateThis'],
  _embind_register_class_property: function(classType,
                                            fieldName,
                                            getterReturnType,
                                            getterSignature,
                                            getter,
                                            getterContext,
                                            setterArgumentType,
                                            setterSignature,
                                            setter,
                                            setterContext) {
    fieldName = readLatin1String(fieldName);
    getter = embind__requireFunction(getterSignature, getter);

    whenDependentTypesAreResolved([], [classType], function(classType) {
      classType = classType[0];
      var humanName = `${classType.name}.${fieldName}`;
      var desc = {
        get: function() {
          throwUnboundTypeError(`Cannot access ${humanName} due to unbound types`, [getterReturnType, setterArgumentType]);
        },
        enumerable: true,
        configurable: true
      };
      if (setter) {
        desc.set = () => {
          throwUnboundTypeError(`Cannot access ${humanName} due to unbound types`, [getterReturnType, setterArgumentType]);
        };
      } else {
        desc.set = (v) => {
          throwBindingError(humanName + ' is a read-only property');
        };
      }

      Object.defineProperty(classType.registeredClass.instancePrototype, fieldName, desc);

      whenDependentTypesAreResolved(
        [],
        (setter ? [getterReturnType, setterArgumentType] : [getterReturnType]),
    function(types) {
        var getterReturnType = types[0];
        var desc = {
          get: function() {
            var ptr = validateThis(this, classType, humanName + ' getter');
            return getterReturnType['fromWireType'](getter(getterContext, ptr));
          },
          enumerable: true
        };

        if (setter) {
          setter = embind__requireFunction(setterSignature, setter);
          var setterArgumentType = types[1];
          desc.set = function(v) {
            var ptr = validateThis(this, classType, humanName + ' setter');
            var destructors = [];
            setter(setterContext, ptr, setterArgumentType['toWireType'](destructors, v));
            runDestructors(destructors);
          };
        }

        Object.defineProperty(classType.registeredClass.instancePrototype, fieldName, desc);
        return [];
      });

      return [];
    });
  },

  _embind_register_class_class_function__deps: [
    '$craftInvokerFunction', '$ensureOverloadTable', '$heap32VectorToArray',
    '$readLatin1String', '$embind__requireFunction', '$throwUnboundTypeError',
    '$whenDependentTypesAreResolved'],
  _embind_register_class_class_function: function(rawClassType,
                                                  methodName,
                                                  argCount,
                                                  rawArgTypesAddr,
                                                  invokerSignature,
                                                  rawInvoker,
                                                  fn,
                                                  isAsync) {
    var rawArgTypes = heap32VectorToArray(argCount, rawArgTypesAddr);
    methodName = readLatin1String(methodName);
    rawInvoker = embind__requireFunction(invokerSignature, rawInvoker);
    whenDependentTypesAreResolved([], [rawClassType], function(classType) {
      classType = classType[0];
      var humanName = `${classType.name}.${methodName}`;

      function unboundTypesHandler() {
        throwUnboundTypeError(`Cannot call ${humanName} due to unbound types`, rawArgTypes);
      }

      if (methodName.startsWith("@@")) {
        methodName = Symbol[methodName.substring(2)];
      }

      var proto = classType.registeredClass.constructor;
      if (undefined === proto[methodName]) {
        // This is the first function to be registered with this name.
        unboundTypesHandler.argCount = argCount-1;
        proto[methodName] = unboundTypesHandler;
      } else {
        // There was an existing function with the same name registered. Set up
        // a function overload routing table.
        ensureOverloadTable(proto, methodName, humanName);
        proto[methodName].overloadTable[argCount-1] = unboundTypesHandler;
      }

      whenDependentTypesAreResolved([], rawArgTypes, function(argTypes) {
        // Replace the initial unbound-types-handler stub with the proper
        // function. If multiple overloads are registered, the function handlers
        // go into an overload table.
        var invokerArgsArray = [argTypes[0] /* return value */, null /* no class 'this'*/].concat(argTypes.slice(1) /* actual params */);
        var func = craftInvokerFunction(humanName, invokerArgsArray, null /* no class 'this'*/, rawInvoker, fn, isAsync);
        if (undefined === proto[methodName].overloadTable) {
          func.argCount = argCount-1;
          proto[methodName] = func;
        } else {
          proto[methodName].overloadTable[argCount-1] = func;
        }

        if (classType.registeredClass.__derivedClasses) {
          for (const derivedClass of classType.registeredClass.__derivedClasses) {
            if (!derivedClass.constructor.hasOwnProperty(methodName)) {
              // TODO: Add support for overloads
              derivedClass.constructor[methodName] = func;
            }
          }
        }

        return [];
      });
      return [];
    });
  },

  _embind_register_class_class_property__deps: [
    '$readLatin1String', '$embind__requireFunction', '$runDestructors',
    '$throwBindingError', '$throwUnboundTypeError',
    '$whenDependentTypesAreResolved', '$validateThis'],
  _embind_register_class_class_property: function(rawClassType,
                                                  fieldName,
                                                  rawFieldType,
                                                  rawFieldPtr,
                                                  getterSignature,
                                                  getter,
                                                  setterSignature,
                                                  setter) {
    fieldName = readLatin1String(fieldName);
    getter = embind__requireFunction(getterSignature, getter);

    whenDependentTypesAreResolved([], [rawClassType], function(classType) {
      classType = classType[0];
      var humanName = `${classType.name}.${fieldName}`;
      var desc = {
        get: function() {
          throwUnboundTypeError(`Cannot access ${humanName} due to unbound types`, [rawFieldType]);
        },
        enumerable: true,
        configurable: true
      };
      if (setter) {
        desc.set = () => {
          throwUnboundTypeError(`Cannot access ${humanName} due to unbound types`, [rawFieldType]);
        };
      } else {
        desc.set = (v) => {
          throwBindingError(`${humanName} is a read-only property`);
        };
      }

      Object.defineProperty(classType.registeredClass.constructor, fieldName, desc);

      whenDependentTypesAreResolved([], [rawFieldType], function(fieldType) {
        fieldType = fieldType[0];
        var desc = {
          get: function() {
              return fieldType['fromWireType'](getter(rawFieldPtr));
          },
          enumerable: true
        };

        if (setter) {
          setter = embind__requireFunction(setterSignature, setter);
          desc.set = (v) => {
            var destructors = [];
            setter(rawFieldPtr, fieldType['toWireType'](destructors, v));
            runDestructors(destructors);
          };
        }

        Object.defineProperty(classType.registeredClass.constructor, fieldName, desc);
        return [];
      });

      return [];
    });
  },

  _embind_create_inheriting_constructor__deps: [
    '$createNamedFunction', '$Emval',
    '$PureVirtualError', '$readLatin1String',
    '$registerInheritedInstance',
    '$requireRegisteredType', '$throwBindingError',
    '$unregisterInheritedInstance', '$detachFinalizer', '$attachFinalizer'],
  _embind_create_inheriting_constructor: function(constructorName, wrapperType, properties) {
    constructorName = readLatin1String(constructorName);
    wrapperType = requireRegisteredType(wrapperType, 'wrapper');
    properties = Emval.toValue(properties);

    var arraySlice = [].slice;

    var registeredClass = wrapperType.registeredClass;
    var wrapperPrototype = registeredClass.instancePrototype;
    var baseClass = registeredClass.baseClass;
    var baseClassPrototype = baseClass.instancePrototype;
    var baseConstructor = registeredClass.baseClass.constructor;
    var ctor = createNamedFunction(constructorName, function() {
      registeredClass.baseClass.pureVirtualFunctions.forEach(function(name) {
          if (this[name] === baseClassPrototype[name]) {
              throw new PureVirtualError(`Pure virtual function ${name} must be implemented in JavaScript`);
          }
      }.bind(this));

      Object.defineProperty(this, '__parent', {
          value: wrapperPrototype
      });
      this["__construct"].apply(this, arraySlice.call(arguments));
    });

    // It's a little nasty that we're modifying the wrapper prototype here.

    wrapperPrototype["__construct"] = function __construct() {
      if (this === wrapperPrototype) {
        throwBindingError("Pass correct 'this' to __construct");
      }

      var inner = baseConstructor["implement"].apply(
        undefined,
        [this].concat(arraySlice.call(arguments)));
      detachFinalizer(inner);
      var $$ = inner.$$;
      inner["notifyOnDestruction"]();
      $$.preservePointerOnDelete = true;
      Object.defineProperties(this, { $$: {
          value: $$
      }});
      attachFinalizer(this);
      registerInheritedInstance(registeredClass, $$.ptr, this);
    };

    wrapperPrototype["__destruct"] = function __destruct() {
      if (this === wrapperPrototype) {
        throwBindingError("Pass correct 'this' to __destruct");
      }

      detachFinalizer(this);
      unregisterInheritedInstance(registeredClass, this.$$.ptr);
    };

    ctor.prototype = Object.create(wrapperPrototype);
    for (var p in properties) {
      ctor.prototype[p] = properties[p];
    }
    return Emval.toHandle(ctor);
  },

  $char_0: '0'.charCodeAt(0),
  $char_9: '9'.charCodeAt(0),
  $makeLegalFunctionName__deps: ['$char_0', '$char_9'],
  $makeLegalFunctionName: function(name) {
    if (undefined === name) {
      return '_unknown';
    }
    name = name.replace(/[^a-zA-Z0-9_]/g, '$');
    var f = name.charCodeAt(0);
    if (f >= char_0 && f <= char_9) {
      return `_${name}`;
    }
    return name;
  },

  _embind_register_smart_ptr__deps: ['$RegisteredPointer', '$embind__requireFunction', '$whenDependentTypesAreResolved'],
  _embind_register_smart_ptr: function(rawType,
                                       rawPointeeType,
                                       name,
                                       sharingPolicy,
                                       getPointeeSignature,
                                       rawGetPointee,
                                       constructorSignature,
                                       rawConstructor,
                                       shareSignature,
                                       rawShare,
                                       destructorSignature,
                                       rawDestructor) {
    name = readLatin1String(name);
    rawGetPointee = embind__requireFunction(getPointeeSignature, rawGetPointee);
    rawConstructor = embind__requireFunction(constructorSignature, rawConstructor);
    rawShare = embind__requireFunction(shareSignature, rawShare);
    rawDestructor = embind__requireFunction(destructorSignature, rawDestructor);

    whenDependentTypesAreResolved([rawType], [rawPointeeType], function(pointeeType) {
      pointeeType = pointeeType[0];

      var registeredPointer = new RegisteredPointer(name,
                                                    pointeeType.registeredClass,
                                                    false,
                                                    false,
                                                    // smart pointer properties
                                                    true,
                                                    pointeeType,
                                                    sharingPolicy,
                                                    rawGetPointee,
                                                    rawConstructor,
                                                    rawShare,
                                                    rawDestructor);
      return [registeredPointer];
    });
  },

  _embind_register_enum__deps: ['$exposePublicSymbol', '$getShiftFromSize', '$enumReadValueFromPointer',
    '$readLatin1String', '$registerType'],
  _embind_register_enum: function(rawType, name, size, isSigned) {
    var shift = getShiftFromSize(size);
    name = readLatin1String(name);

    function ctor() {}
    ctor.values = {};

    registerType(rawType, {
      name,
      constructor: ctor,
      'fromWireType': function(c) {
        return this.constructor.values[c];
      },
      'toWireType': function(destructors, c) {
        return c.value;
      },
      'argPackAdvance': 8,
      'readValueFromPointer': enumReadValueFromPointer(name, shift, isSigned),
      destructorFunction: null,
    });
    exposePublicSymbol(name, ctor);
  },

  _embind_register_enum_value__deps: ['$createNamedFunction', '$identifierToString', '$requireRegisteredType'],
  _embind_register_enum_value: function(rawEnumType, name, enumValue) {
    var enumType = requireRegisteredType(rawEnumType, 'enum');
    name = identifierToString(name);

    var Enum = enumType.constructor;

    var Value = Object.create(enumType.constructor.prototype, {
      value: {value: enumValue},
      constructor: {value: createNamedFunction(`${enumType.name}_${name}`, function() {})},
    });
    Enum.values[enumValue] = Value;
    Enum[name] = Value;
  },

  _embind_register_constant__deps: ['$readLatin1String', '$whenDependentTypesAreResolved'],
  _embind_register_constant: function(name, type, value) {
    name = readLatin1String(name);
    whenDependentTypesAreResolved([], [type], function(type) {
      type = type[0];
      Module[name] = type['fromWireType'](value);
      return [];
    });
  },
};

mergeInto(LibraryManager.library, LibraryEmbind);
