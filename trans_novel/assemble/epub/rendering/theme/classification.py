from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from trans_novel.assemble.epub.rendering.theme.contracts import (
    RoleAssignment,
    ThemeBundle,
    ThemeError,
)

_MEMORY_LIMIT = 64 * 1024 * 1024
_STACK_LIMIT = 512 * 1024
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_RESULT_BYTES = 4 * 1024 * 1024
_MAX_NODES = 50_000
_ROLE = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")

_BOOTSTRAP = r"""
const __tnHost = (() => {
  "use strict";
  const parse = JSON.parse.bind(JSON);
  const stringify = JSON.stringify.bind(JSON);
  const freeze = Object.freeze.bind(Object);
  const isFrozen = Object.isFrozen.bind(Object);
  const values = Object.values.bind(Object);
  const getPrototypeOf = Object.getPrototypeOf.bind(Object);
  const getOwnPropertyDescriptor = Object.getOwnPropertyDescriptor.bind(Object);
  const ownKeys = Reflect.ownKeys.bind(Reflect);
  const deleteProperty = Reflect.deleteProperty.bind(Reflect);
  const isArray = Array.isArray.bind(Array);
  const isInteger = Number.isInteger.bind(Number);
  const regexTest = Function.prototype.call.bind(RegExp.prototype.test);
  const objectPrototype = Object.prototype;
  const internalErrorPrototype = InternalError.prototype;
  const rangeErrorPrototype = RangeError.prototype;
  const rolePattern = /^[a-z][a-z0-9-]{0,31}$/;

  const deepFreeze = value => {
    if (value !== null && typeof value === "object" && !isFrozen(value)) {
      freeze(value);
      const children = values(value);
      for (let index = 0; index < children.length; index += 1) deepFreeze(children[index]);
    }
    return value;
  };

  const validate = value => {
    if (value === null) return null;
    if (typeof value !== "object" || isArray(value)) return false;
    const prototype = getPrototypeOf(value);
    if (prototype !== objectPrototype && prototype !== null) return false;
    const keys = ownKeys(value);
    if (keys.length < 1 || keys.length > 2) return false;
    let role;
    let level;
    let hasRole = false;
    let hasLevel = false;
    for (let index = 0; index < keys.length; index += 1) {
      const key = keys[index];
      if (typeof key !== "string" || (key !== "role" && key !== "level")) return false;
      const descriptor = getOwnPropertyDescriptor(value, key);
      if (descriptor === undefined || !("value" in descriptor)) return false;
      if (key === "role") {
        hasRole = true;
        role = descriptor.value;
      } else {
        hasLevel = true;
        level = descriptor.value;
      }
    }
    if (!hasRole || typeof role !== "string" || !regexTest(rolePattern, role)) return false;
    if (hasLevel && (!isInteger(level) || level < 1 || level > 6 || role !== "heading")) {
      return false;
    }
    return {role, level: hasLevel ? level : null};
  };

  const run = (classifier, source) => {
    const document = deepFreeze(parse(source));
    if (document === null || typeof document !== "object" || !isArray(document.nodes)) {
      return '{"error":"invalid_result","nodeId":null}';
    }
    let output = "[";
    for (let index = 0; index < document.nodes.length; index += 1) {
      let raw;
      try {
        raw = classifier(document.nodes[index], document);
      } catch (error) {
        let detail = "classification_failed";
        if (error !== null && typeof error === "object") {
          const prototype = getPrototypeOf(error);
          const message = getOwnPropertyDescriptor(error, "message");
          if (message && (prototype === internalErrorPrototype || prototype === rangeErrorPrototype)) {
            if (message.value === "out of memory") detail = "memory_limit";
            if (message.value === "stack overflow" || message.value === "Maximum call stack size exceeded") {
              detail = "stack_limit";
            }
          }
        }
        return '{"error":"' + detail + '","nodeId":' + index + "}";
      }
      let checked;
      try {
        checked = validate(raw);
      } catch (_) {
        return '{"error":"invalid_result","nodeId":' + index + "}";
      }
      if (checked === false) {
        return '{"error":"invalid_result","nodeId":' + index + "}";
      }
      if (index > 0) output += ",";
      output += checked === null
        ? "null"
        : '{"role":"' + checked.role + '"' +
          (checked.level === null ? "" : ',"level":' + checked.level) + "}";
    }
    return output + "]";
  };

  if (!deleteProperty(globalThis, "Date") ||
      !deleteProperty(Math, "random") ||
      !deleteProperty(globalThis, "Promise")) {
    throw new Error("runtime initialization failed");
  }
  // ponytail: 当前绑定不支持 rope 字符串；JSON 往返将其扁平化，绑定支持后移除。
  return freeze({run: (classifier, source) => parse(stringify(run(classifier, source)))});
})();
"""


def _engine_error(error: BaseException, fallback: str) -> ThemeError:
    message = str(error).lower()
    if "interrupted" in message:
        return ThemeError("theme_limit", "cpu_limit")
    if "stack overflow" in message or "maximum call stack size exceeded" in message:
        return ThemeError("theme_limit", "stack_limit")
    if "out of memory" in message:
        return ThemeError("theme_limit", "memory_limit")
    return ThemeError("theme_script", fallback)


def _script_source(bundle: ThemeBundle) -> str:
    assert bundle.script is not None
    try:
        return bundle.script.decode("utf-8")
    except UnicodeDecodeError:
        raise ThemeError("theme_script", "initialization_failed") from None


def _new_context(bundle: ThemeBundle) -> Any:
    try:
        import quickjs

        context = quickjs.Context()
        context.set_memory_limit(_MEMORY_LIMIT)
        context.set_max_stack_size(_STACK_LIMIT)
        context.set_time_limit(1)
        context.eval(_BOOTSTRAP)
        source = (
            '"use strict";\n'
            + _script_source(bundle)
            + "\n;typeof classify === 'function' ? classify : null"
        )
        missing = context.eval(
            "const __tnClassifier = (0, eval)("
            + json.dumps(source, ensure_ascii=True)
            + "); __tnClassifier === null;"
        )
    except ThemeError:
        raise
    except Exception as error:
        raise _engine_error(error, "initialization_failed") from None
    if missing is not False:
        raise ThemeError("theme_script", "missing_classify")
    return context


def validate_script(bundle: ThemeBundle) -> None:
    if bundle.script is None:
        return
    _new_context(bundle)


def _encode_snapshot(snapshot: Mapping[str, Any]) -> tuple[str, int]:
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list):
        raise ThemeError("theme_script", "invalid_result")
    if len(nodes) > _MAX_NODES:
        raise ThemeError("theme_limit", "snapshot_nodes")
    if any(
        not isinstance(node, dict) or node.get("id") != index for index, node in enumerate(nodes)
    ):
        raise ThemeError("theme_script", "invalid_result")
    try:
        encoded = json.dumps(
            dict(snapshot), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise ThemeError("theme_script", "invalid_result") from None
    if len(encoded.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise ThemeError("theme_limit", "snapshot_bytes")
    return encoded, len(nodes)


def _decode_results(value: Any, node_count: int) -> tuple[RoleAssignment, ...]:
    if isinstance(value, dict) and value.get("error") in {
        "invalid_result",
        "classification_failed",
        "memory_limit",
        "stack_limit",
    }:
        node_id = value.get("nodeId")
        if (
            not isinstance(node_id, int)
            or isinstance(node_id, bool)
            or not 0 <= node_id < node_count
        ):
            node_id = None
        code = (
            "theme_limit" if value["error"] in {"memory_limit", "stack_limit"} else "theme_script"
        )
        raise ThemeError(code, value["error"], node_id=node_id)
    if not isinstance(value, list) or len(value) != node_count:
        raise ThemeError("theme_script", "invalid_result")
    assignments: list[RoleAssignment] = []
    for node_id, item in enumerate(value):
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) not in ({"role"}, {"role", "level"}):
            raise ThemeError("theme_script", "invalid_result", node_id=node_id)
        role = item.get("role")
        level = item.get("level")
        if (
            not isinstance(role, str)
            or _ROLE.fullmatch(role) is None
            or (
                "level" in item
                and (
                    not isinstance(level, int)
                    or isinstance(level, bool)
                    or not 1 <= level <= 6
                    or role != "heading"
                )
            )
        ):
            raise ThemeError("theme_script", "invalid_result", node_id=node_id)
        assignments.append(RoleAssignment(node_id, role, level))
    return tuple(assignments)


def classify_resource(
    snapshot: Mapping[str, Any], bundle: ThemeBundle
) -> tuple[RoleAssignment, ...]:
    if bundle.script is None:
        return ()
    encoded, node_count = _encode_snapshot(snapshot)
    context = _new_context(bundle)
    try:
        context.set_time_limit(2)
        result_json = context.eval(
            "__tnHost.run(__tnClassifier, " + json.dumps(encoded, ensure_ascii=True) + ")"
        )
    except Exception as error:
        raise _engine_error(error, "classification_failed") from None
    if not isinstance(result_json, str):
        raise ThemeError("theme_script", "invalid_result")
    if len(result_json.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise ThemeError("theme_limit", "result_bytes")
    try:
        result = json.loads(result_json)
    except (TypeError, ValueError):
        raise ThemeError("theme_script", "invalid_result") from None
    return _decode_results(result, node_count)
