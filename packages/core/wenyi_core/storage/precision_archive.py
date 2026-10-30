"""Precision archive schema v1, using only the artifact persistence port.

Objects are SHA-256 addressed envelopes containing JSON values. Glossary version
names hash the ordered term-object references, independent of revision ancestry.
Versions contain either a base reference list or a parent plus source-keyed
changes, removals and appended sources (an explicit order only for reordering).
Bases occur at most every 32 edges. Term objects hold raw dataclass metadata and
the historical rendered line. Message recipes reference immutable literal
fragments and encoded JSON fields; they never require a current prompt template.

The sole mutable record is glossary/head.json, an optimization, not replay data.
Callers must serialize writes using their storage state lock: ArtifactStorage
does not offer compare-and-swap. No record is overwritten on a detected conflict.
These artifacts contain private source text and belong only in run-state storage.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..agents import prompts
from ..glossary.store import GlossaryTerm
from .protocol import ArtifactStorage

_ROOT = "precision/shared/"
_OBJECTS = _ROOT + "objects/"
_VERSIONS = _ROOT + "glossary/versions/"
_HEAD = _ROOT + "glossary/head.json"


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(_json(value))


class PrecisionArchive:
    """Capture source-filtered prompt lines while replaying historical records verbatim."""

    def __init__(self, store: ArtifactStorage, *, source_lang: str = "auto"):
        self.store = store
        self.source_lang = source_lang
        self._capture_depth = 0
        self._terms: dict[str, dict] = {}
        self._revisions: dict[str, tuple[list[str], int]] = {}
        self._texts: dict[str, str] = {}

    @contextmanager
    def _capture(self):
        """Cache immutable inputs during capture, never during explicit replay.

        Captured records must not be externally mutated while this instance is
        writing a frozen batch. Replay APIs and get() always revalidate storage;
        they detect missing/corrupt records even after a successful capture.
        """
        self._capture_depth += 1
        try:
            yield
        finally:
            self._capture_depth -= 1

    @contextmanager
    def _replay(self):
        """Validate each distinct immutable record once per explicit restore.

        Fresh operation-local caches detect external mutations on the next
        restore without multiplying backend reads by glossary chain depth.
        Internal reconstruction during capture reuses its validated inputs.
        """
        if self._capture_depth:
            yield
            return
        saved = self._terms, self._revisions, self._texts
        self._terms, self._revisions, self._texts = {}, {}, {}
        try:
            with self._capture():
                yield
        finally:
            self._terms, self._revisions, self._texts = saved

    def _read(self, key: str) -> dict:
        value = self.store.read_artifact(key)
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError(f"Missing or invalid precision archive record: {key}")
        return value

    def _write(self, key: str, value: dict) -> None:
        existing = self.store.read_artifact(key)
        if existing is not None:
            if existing != value:
                raise ValueError(f"Conflicting precision archive record: {key}")
            return
        self.store.write_artifact(key, value)
        if self.store.read_artifact(key) != value:
            raise ValueError(f"Precision archive write verification failed: {key}")

    def put(self, value: Any) -> str:
        value = _copy(value)
        key = f"{_OBJECTS}{_hash(value)}.json"
        self._write(key, {"schema": 1, "kind": "object", "value": value})
        return key

    def get(self, ref: str) -> Any:
        if not isinstance(ref, str) or not re.fullmatch(
            re.escape(_OBJECTS) + r"[0-9a-f]{64}\.json", ref
        ):
            raise ValueError(f"Invalid precision object reference: {ref}")
        record = self._read(ref)
        if record.get("kind") != "object" or "value" not in record:
            raise ValueError(f"Invalid precision object: {ref}")
        value = record["value"]
        if ref != f"{_OBJECTS}{_hash(value)}.json":
            raise ValueError(f"Precision object hash mismatch: {ref}")
        return _copy(value)

    def _revision(self, ref: str, seen: set[str] | None = None) -> tuple[list[str], int]:
        if not isinstance(ref, str) or not re.fullmatch(
            re.escape(_VERSIONS) + r"[0-9a-f]{64}\.json", ref
        ):
            raise ValueError(f"Invalid glossary reference: {ref}")
        if self._capture_depth and ref in self._revisions:
            refs, depth = self._revisions[ref]
            return list(refs), depth
        seen = set() if seen is None else seen
        if ref in seen or len(seen) > 32:
            raise ValueError(f"Cyclic or unbounded glossary revision: {ref}")
        seen.add(ref)
        record = self._read(ref)
        try:
            if record["kind"] != "glossary":
                raise ValueError("Wrong record kind")
            if "base" in record:
                refs = record["base"]
                depth = 0
            else:
                previous, depth = self._revision(record["parent"], seen)
                index = {self._term(item)["raw"]["source"]: item for item in previous}
                for source in record["removed"]:
                    del index[source]
                index.update(record["changed"])
                order = record.get("order")
                if order is None:
                    order = [
                        self._term(item)["raw"]["source"]
                        for item in previous
                        if self._term(item)["raw"]["source"] not in record["removed"]
                    ] + record["appended"]
                if len(order) != len(set(order)) or set(order) != set(index):
                    raise ValueError("Invalid glossary order")
                refs = [index[source] for source in order]
                depth += 1
            if not isinstance(refs, list):
                raise ValueError("Invalid glossary base")
            sources = [self._term(item)["raw"]["source"] for item in refs]
            if len(sources) != len(set(sources)):
                raise ValueError("Duplicate glossary source")
            if ref != f"{_VERSIONS}{_hash(refs)}.json":
                raise ValueError("Glossary state hash mismatch")
            if record["depth"] != depth:
                raise ValueError("Glossary depth mismatch")
            if self._capture_depth:
                self._revisions[ref] = (list(refs), depth)
                if len(self._revisions) > 32:
                    self._revisions.pop(next(iter(self._revisions)))
            return refs, depth
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Corrupt glossary revision: {ref}") from exc

    def _term(self, ref: str) -> dict:
        if self._capture_depth and ref in self._terms:
            return self._terms[ref]
        value = self.get(ref)
        if (
            not isinstance(value, dict)
            or value.get("kind") != "term"
            or not isinstance(value.get("raw"), dict)
            or not isinstance(value.get("prompt_line"), str)
        ):
            raise ValueError(f"Invalid glossary term record: {ref}")
        try:
            GlossaryTerm(**value["raw"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid glossary metadata: {ref}") from exc
        if self._capture_depth:
            self._terms[ref] = value
        return value

    def glossary(self, terms: tuple[GlossaryTerm, ...]) -> str:
        with self._capture():
            return self._glossary(terms)

    def _glossary(self, terms: tuple[GlossaryTerm, ...]) -> str:
        refs = []
        for term in terms:
            value = {
                "kind": "term",
                "raw": asdict(term),
                "prompt_line": prompts.render_glossary([term], source_lang=self.source_lang),
            }
            ref = self.put(value)
            self._terms[ref] = _copy(value)
            refs.append(ref)
        sources = [term.source for term in terms]
        if len(sources) != len(set(sources)):
            raise ValueError("Duplicate glossary source")
        key = f"{_VERSIONS}{_hash(refs)}.json"
        index = dict(zip(sources, refs))
        existing = self.store.read_artifact(key)
        if existing is not None:
            self._revision(key)
        else:
            head = self.store.read_artifact(_HEAD)
            record: dict = {"schema": 1, "kind": "glossary", "depth": 0}
            if head is None:
                record["base"] = refs
            else:
                if not isinstance(head, dict) or head.get("schema") != 1:
                    raise ValueError("Corrupt glossary head")
                # New heads include ordered refs validated at commit. Verify that
                # index against its state hash and term identities, avoiding an
                # ancestry walk on a fresh archive instance. Legacy heads replay.
                if "order" in head:
                    order = head["order"]
                    old_index = head.get("index")
                    if (
                        not isinstance(order, list)
                        or not isinstance(old_index, dict)
                        or not all(isinstance(source, str) for source in order)
                        or len(order) != len(set(order))
                        or set(order) != set(old_index)
                    ):
                        raise ValueError("Corrupt glossary head order")
                    ordered_refs = [old_index[source] for source in order]
                    if head.get("ref") != f"{_VERSIONS}{_hash(ordered_refs)}.json":
                        raise ValueError("Glossary head hash mismatch")
                    for source, term_ref in old_index.items():
                        if self._term(term_ref)["raw"]["source"] != source:
                            raise ValueError("Glossary head source mismatch")
                    version = self._read(head["ref"])
                    depth = version.get("depth")
                    if (
                        version.get("kind") != "glossary"
                        or type(depth) is not int
                        or not 0 <= depth <= 31
                    ):
                        raise ValueError("Corrupt glossary head version")
                    self._revisions[head["ref"]] = (ordered_refs, depth)
                head_ref = head.get("ref")
                if not isinstance(head_ref, str):
                    raise ValueError("Invalid glossary head reference")
                previous, depth = self._revision(head_ref)
                old_sources = [self._term(item)["raw"]["source"] for item in previous]
                old = dict(zip(old_sources, previous))
                if head.get("index") != old:
                    raise ValueError("Glossary head index mismatch")
                if depth >= 31:
                    record["base"] = refs
                else:
                    appended = [source for source in sources if source not in old]
                    removed = [source for source in old_sources if source not in index]
                    record.update(
                        parent=head["ref"],
                        depth=depth + 1,
                        changed={s: r for s, r in index.items() if old.get(s) != r},
                        removed=removed,
                        appended=appended,
                    )
                    natural = [s for s in old_sources if s in index] + appended
                    if sources != natural:
                        record["order"] = sources
            self._write(key, record)
            self._revision(key)
        self.store.write_artifact(
            _HEAD, {"schema": 1, "ref": key, "index": index, "order": sources}
        )
        self._terms = {ref: self._terms[ref] for ref in refs if ref in self._terms}
        return key

    def load_glossary(self, ref: str) -> tuple[GlossaryTerm, ...]:
        with self._replay():
            refs, _ = self._revision(ref)
            return tuple(GlossaryTerm(**self._term(item)["raw"]) for item in refs)

    def glossary_text(self, ref: str) -> str:
        with self._replay():
            return self._glossary_text(ref)

    def _glossary_text(self, ref: str) -> str:
        if self._capture_depth and ref in self._texts:
            return self._texts[ref]
        refs, _ = self._revision(ref)
        text = "\n".join(self._term(item)["prompt_line"] for item in refs) if refs else "(none)"
        if self._capture_depth:
            self._texts.clear()
            self._texts[ref] = text
        return text

    def freeze_plan(self, plan: dict) -> dict:
        return {
            "fields": {key: self.put(value) for key, value in plan.items() if key != "terms"},
            "glossary_ref": self.glossary(tuple(GlossaryTerm(**term) for term in plan["terms"])),
        }

    def thaw_plan(self, refdict: dict) -> dict:
        with self._replay():
            result = {key: self.get(ref) for key, ref in refdict["fields"].items()}
            refs, _ = self._revision(refdict["glossary_ref"])
            result["terms"] = [self._term(ref)["raw"] for ref in refs]
            return result

    def _message_parts(self, text: str, glossary_ref: str) -> list[dict] | None:
        """Recognize canonical JSON packets without relying on current templates."""
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            start = match.start()
            try:
                packet, length = decoder.raw_decode(text[start:])
            except ValueError:
                continue
            if not isinstance(packet, dict):
                continue
            drafts = packet.get("drafts")
            has_drafts = (
                isinstance(drafts, list)
                and all(isinstance(draft, list) for draft in drafts)
                and all(isinstance(text, str) for draft in drafts for text in draft)
            )
            targets = packet.get("translations")
            has_targets = isinstance(targets, list) and all(
                isinstance(target, str) for target in targets
            )
            glossary = None
            if isinstance(packet.get("glossary"), str):
                glossary = self.glossary_text(glossary_ref)
            if (
                not has_targets
                and not has_drafts
                and not any(isinstance(v, str) and v == glossary for v in packet.values())
            ):
                continue
            original = text[start : start + length]
            # Only canonical encodings are split; unusual spacing/escaping stays literal.
            for indent in (None, 2):
                for ascii_only in (False, True):
                    encoded = json.dumps(packet, ensure_ascii=ascii_only, indent=indent)
                    if encoded != original:
                        continue
                    parts: list[dict[str, Any]] = [{"literal": self.put(text[:start])}]
                    cursor = 0
                    for name, value in packet.items():
                        needle = json.dumps(name, ensure_ascii=ascii_only) + ": "
                        position = encoded.find(needle, cursor)
                        if position < 0:
                            return None
                        value_start = position + len(needle)
                        value_text = json.dumps(value, ensure_ascii=ascii_only, indent=indent)
                        if indent is not None:
                            value_text = value_text.replace("\n", "\n" + " " * indent)
                        # Nested values include the outer object's field indentation.
                        if not encoded.startswith(value_text, value_start):
                            return None
                        parts.append({"literal": self.put(encoded[cursor:value_start])})
                        if isinstance(value, str) and value == glossary:
                            parts.append({"glossary_ref": glossary_ref, "ensure_ascii": ascii_only})
                        elif name == "drafts" and has_drafts:
                            parts.append(
                                {
                                    "draft_refs": [self.put(draft) for draft in value],
                                    "ensure_ascii": ascii_only,
                                    "indent": indent,
                                    "offset": indent or 0,
                                }
                            )
                        else:
                            value_ref = self.put(value)
                            replay_text = json.dumps(
                                self.get(value_ref), ensure_ascii=ascii_only, indent=indent
                            )
                            if indent is not None:
                                replay_text = replay_text.replace("\n", "\n" + " " * indent)
                            # Canonical object hashing ignores mapping insertion order.
                            # Preserve encoded bytes when a nested mapping has a different order.
                            if replay_text != value_text:
                                parts.append({"literal": self.put(value_text)})
                                cursor = value_start + len(value_text)
                                continue
                            parts.append(
                                {
                                    "target_ref"
                                    if name == "translations" and has_targets
                                    else "json_ref": value_ref,
                                    "ensure_ascii": ascii_only,
                                    "indent": indent,
                                    "offset": indent or 0,
                                }
                            )
                        cursor = value_start + len(value_text)
                    parts.append({"literal": self.put(encoded[cursor:] + text[start + length :])})
                    return parts
        return None

    def messages(self, messages: list[dict[str, str]], glossary_ref: str) -> list[dict]:
        with self._capture():
            return self._messages(messages, glossary_ref)

    def _messages(self, messages: list[dict[str, str]], glossary_ref: str) -> list[dict]:
        if not isinstance(glossary_ref, str) or not re.fullmatch(
            re.escape(_VERSIONS) + r"[0-9a-f]{64}\.json", glossary_ref
        ):
            raise ValueError(f"Invalid glossary reference: {glossary_ref}")
        # Non-glossary recipes have no dependency on the glossary contents.
        # Check existence without replaying thousands of unrelated term objects.
        if glossary_ref not in self._revisions:
            if self._read(glossary_ref).get("kind") != "glossary":
                raise ValueError(f"Invalid glossary record: {glossary_ref}")
        recipes = []
        for message in messages:
            recipe = {
                "schema": 1,
                "fields": {
                    key: self._message_parts(value, glossary_ref) or [{"literal": self.put(value)}]
                    for key, value in message.items()
                },
            }
            recipes.append(recipe)
        if self.load_messages(recipes) != messages:
            raise ValueError("Precision message recipe does not exactly reconstruct input")
        return recipes

    def load_messages(self, recipe: list[dict]) -> list[dict[str, str]]:
        with self._replay():
            return self._load_messages(recipe)

    def _load_messages(self, recipe: list[dict]) -> list[dict[str, str]]:
        objects: dict[str, Any] = {}

        def object_value(ref: str) -> Any:
            if ref not in objects:
                objects[ref] = self.get(ref)
            return objects[ref]

        messages = []
        for message in recipe:
            if message.get("schema") != 1 or not isinstance(message.get("fields"), dict):
                raise ValueError("Invalid precision message recipe")
            fields = {}
            for key, parts in message["fields"].items():
                chunks = []
                for part in parts:
                    if set(part) == {"literal"}:
                        value = object_value(part["literal"])
                        if not isinstance(value, str):
                            raise ValueError("Message literal must be a string")
                        chunks.append(value)
                    elif set(part) == {"glossary_ref", "ensure_ascii"}:
                        if not isinstance(part["ensure_ascii"], bool):
                            raise ValueError("Invalid glossary string encoding")
                        chunks.append(
                            json.dumps(
                                self.glossary_text(part["glossary_ref"]),
                                ensure_ascii=part["ensure_ascii"],
                            )
                        )
                    elif set(part) in (
                        {"draft_refs", "ensure_ascii", "indent"},
                        {"draft_refs", "ensure_ascii", "indent", "offset"},
                    ):
                        if (
                            not isinstance(part["draft_refs"], list)
                            or not isinstance(part["ensure_ascii"], bool)
                            or part["indent"] not in (None, 2)
                        ):
                            raise ValueError("Invalid draft string encoding")
                        drafts = [object_value(ref) for ref in part["draft_refs"]]
                        if not all(
                            isinstance(draft, list) and all(isinstance(text, str) for text in draft)
                            for draft in drafts
                        ):
                            raise ValueError("Invalid draft snapshot")
                        chunks.append(
                            json.dumps(
                                drafts, ensure_ascii=part["ensure_ascii"], indent=part["indent"]
                            ).replace("\n", "\n" + " " * part.get("offset", 0))
                        )
                    elif set(part) in (
                        {"json_ref", "ensure_ascii", "indent", "offset"},
                        {"target_ref", "ensure_ascii", "indent", "offset"},
                    ):
                        if (
                            not isinstance(part["ensure_ascii"], bool)
                            or part["indent"] not in (None, 2)
                            or part["offset"] not in (0, 2)
                        ):
                            raise ValueError("Invalid JSON field encoding")
                        ref_name = "target_ref" if "target_ref" in part else "json_ref"
                        value = object_value(part[ref_name])
                        if ref_name == "target_ref" and not (
                            isinstance(value, list)
                            and all(isinstance(target, str) for target in value)
                        ):
                            raise ValueError("Invalid translation targets")
                        chunks.append(
                            json.dumps(
                                value, ensure_ascii=part["ensure_ascii"], indent=part["indent"]
                            ).replace("\n", "\n" + " " * part["offset"])
                        )
                    else:
                        raise ValueError("Invalid precision message fragment")
                fields[key] = "".join(chunks)
            messages.append(fields)
        return messages


def safe_model_snapshot(value: dict) -> dict:
    """Recursively sanitize configuration and report explicit redaction paths.

    The result is ``{"config": sanitized_configuration, "redacted": [paths]}``.
    Environment-variable names (api_key_env) remain available for replay setup.
    Actual archived prompt prose must not be passed to this configuration-only
    function. Configuration subtrees named prompt/messages are not exempt.
    """
    redacted: list[str] = []

    def sensitive(key: str) -> bool:
        key = re.sub(r"[^a-z0-9]", "", key.lower())
        return key not in {
            "apikeyenv",
            "maxtokens",
            "maxcompletiontokens",
            "maxoutputtokens",
            "tokenlimit",
            "tokenbudget",
            "tokenizer",
        } and (
            key in {"headers", "extraheaders", "defaultheaders", "authorization", "auth"}
            or any(
                word in key
                for word in ("apikey", "token", "password", "secret", "credential", "header")
            )
        )

    def clean(item: Any, path: str) -> Any:
        if isinstance(item, dict):
            result = {}
            for key, entry in item.items():
                child = f"{path}.{key}" if path else key
                if sensitive(key):
                    redacted.append(child)
                else:
                    result[key] = clean(entry, child)
            return result
        if isinstance(item, list):
            return [clean(entry, f"{path}[{i}]") for i, entry in enumerate(item)]
        if isinstance(item, str) and re.match(r"^[a-z][a-z0-9+.-]*://", item, re.I):
            try:
                url = urlsplit(item)
                netloc = url.netloc.rsplit("@", 1)[-1]
                if netloc != url.netloc:
                    redacted.append(path + ".userinfo")

                def scrub_parameters(parameters: str, component: str) -> str:
                    pairs = []
                    changed = False
                    for key, entry in parse_qsl(parameters, keep_blank_values=True):
                        if sensitive(key) or key.lower() in {"key", "sig", "signature"}:
                            redacted.append(path + "." + component + "." + key)
                            entry = "[REDACTED]"
                            changed = True
                        pairs.append((key, entry))
                    return urlencode(pairs) if changed else parameters

                query = scrub_parameters(url.query, "query")
                route, separator, parameters = url.fragment.partition("?")
                fragment = (
                    route + separator + scrub_parameters(parameters, "fragment")
                    if separator
                    else scrub_parameters(url.fragment, "fragment")
                )
                return urlunsplit((url.scheme, netloc, url.path, query, fragment))
            except ValueError:
                redacted.append(path)
                return "[REDACTED]"
        return item

    return {"config": clean(_copy(value), ""), "redacted": redacted}
