from __future__ import annotations

import hashlib
import os
import posixpath
import tempfile
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import copy
from urllib.parse import quote

from lxml import etree

from trans_novel.assemble.epub.rendering.bilingual import local_name
from trans_novel.assemble.epub.rendering.source_dom import (
    parse_source_markup,
    resolve_element_path,
    serialize_source_tree,
)
from trans_novel.assemble.epub.rendering.theme.classification import validate_script
from trans_novel.assemble.epub.rendering.theme.contracts import (
    ResourceThemePlan,
    ResourceThemeScope,
    ThemeBundle,
    ThemeError,
    ThemePlan,
)
from trans_novel.assemble.epub.rendering.theme.css import CssRule, parse_theme_css
from trans_novel.assemble.epub.rendering.theme.planning import (
    plan_resource,
    tree_sha256,
    validate_resource_plan,
)
from trans_novel.epub.archive import (
    MetadataZipFile,
    ZipSafetyError,
    preflight_zip,
    read_member,
)
from trans_novel.epub.navigation import resolve_epub_href
from trans_novel.epub.package import HTML_MEDIA, read_package

_RESERVED_ATTRIBUTES = {
    "data-tn-role",
    "data-tn-level",
    "data-tn-content",
    "data-tn-theme-node",
}
_FIXED = "pre-paginated"
_REFLOWABLE = "reflowable"
_UNKNOWN_LAYOUT = "unknown"
_PACKAGE_LAYOUT = "rendition:layout"
_FIXED_TOKEN = "rendition:layout-pre-paginated"
_REFLOWABLE_TOKEN = "rendition:layout-reflowable"
_COVER_TYPES = {"cover", "cover-page", "cover-image", "title-page", "titlepage"}


def _invalid_archive() -> ThemeError:
    return ThemeError("theme_css", "invalid_archive")


def _invalid_plan(resource: str | None = None) -> ThemeError:
    return ThemeError("theme_verify", "invalid_plan", resource=resource)


def _parse_xml(data: bytes, *, resource: str) -> etree._ElementTree:
    parser = etree.XMLParser(
        no_network=True,
        recover=False,
        resolve_entities=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False,
    )
    try:
        return etree.fromstring(data, parser).getroottree()
    except (etree.LxmlError, ValueError, TypeError):
        raise ThemeError("theme_css", "invalid_markup", resource=resource) from None


def _tokens(value: object) -> set[str]:
    return {token.lower() for token in str(value or "").split()}


def _opf_semantics(
    tree: etree._ElementTree,
    model: dict[str, object],
) -> tuple[set[str], set[str]]:
    root = tree.getroot()
    elements = [node for node in root.iter() if isinstance(node.tag, str)]
    metadata_layouts = [
        str(node.text or "").strip().lower()
        for node in elements
        if local_name(node.tag) == "meta"
        and str(node.get("property", "")).strip().lower() == _PACKAGE_LAYOUT
    ]
    package_layout = _layout_at_level(metadata_layouts, None)

    itemref_properties = {
        str(node.get("idref", "")).strip(): _tokens(node.get("properties"))
        for node in elements
        if local_name(node.tag) == "itemref" and str(node.get("idref", "")).strip()
    }
    protected = set(model.get("toc_paths", ()))
    protected.update(
        str(entry["resource_href"])
        for entry in model.get("guide_entries", ())
        if _tokens(entry.get("type")) & _COVER_TYPES
    )
    fixed: set[str] = set()
    for item in model.get("resolved", ()):
        if item.get("media") not in HTML_MEDIA or not isinstance(item.get("path"), str):
            continue
        path = item["path"]
        properties = _tokens(item.get("properties"))
        if properties & (_COVER_TYPES | {"nav"}):
            protected.add(path)
        item_layout = _layout_at_level((), properties)
        ref_layout = _layout_at_level((), itemref_properties.get(str(item.get("id", "")), set()))
        effective = ref_layout or item_layout or package_layout
        if effective == _FIXED:
            fixed.add(path)
    return protected, fixed


def _layout_at_level(values: Sequence[str], properties: set[str] | None) -> str | None:
    declared: set[str] = set()
    declared.update(value for value in values if value in {_FIXED, _REFLOWABLE})
    has_layout = bool(values)
    if properties is not None:
        layout_tokens = {token for token in properties if token.startswith("rendition:layout-")}
        has_layout = bool(layout_tokens)
        if _FIXED_TOKEN in properties:
            declared.add(_FIXED)
        if _REFLOWABLE_TOKEN in properties:
            declared.add(_REFLOWABLE)
    if len(declared) > 1:
        raise ThemeError("theme_css", "invalid_layout")
    if declared:
        return next(iter(declared))
    return _UNKNOWN_LAYOUT if has_layout else None


def _read_package_or_fail(archive: zipfile.ZipFile) -> dict[str, object]:
    failures: list[dict[str, str]] = []
    try:
        package = read_package(archive, failures)
    except (KeyError, OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
        raise _invalid_archive() from None
    if not package.get("valid") or failures:
        raise _invalid_archive()
    return package


def _member_ledger(
    archive: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo]
) -> tuple[tuple[str, str], ...]:
    ledger: list[tuple[str, str]] = []
    try:
        for info in infos:
            ledger.append((info.filename, hashlib.sha256(read_member(archive, info)).hexdigest()))
    except (KeyError, OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
        raise _invalid_archive() from None
    return tuple(ledger)


def _valid_mimetype(archive: zipfile.ZipFile, infos: Sequence[zipfile.ZipInfo]) -> bool:
    if not infos or infos[0].filename != "mimetype":
        return False
    try:
        return read_member(archive, infos[0]) == b"application/epub+zip"
    except (KeyError, OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
        return False


def _html_resources(model: dict[str, object]) -> list[dict[str, object]]:
    return [
        item
        for item in model.get("resolved", ())
        if item.get("media") in HTML_MEDIA and isinstance(item.get("path"), str)
    ]


def _check_collisions(
    archive: zipfile.ZipFile,
    infos: Sequence[zipfile.ZipInfo],
    model: dict[str, object],
    opf_dir: str,
) -> None:
    prefix = f"{opf_dir}/tn-theme/" if opf_dir else "tn-theme/"
    if any(info.filename.startswith(prefix) for info in infos):
        raise ThemeError("theme_collision", "reserved_resource")
    if any(
        str(item.get("id", "")).startswith("tn-theme-style-") for item in model.get("items", ())
    ):
        raise ThemeError("theme_collision", "reserved_id")
    for item in _html_resources(model):
        resource = str(item["path"])
        try:
            data = read_member(archive, archive.getinfo(resource))
            tree, _mode = parse_source_markup(data)
        except (
            KeyError,
            OSError,
            ValueError,
            ZipSafetyError,
            zipfile.BadZipFile,
            etree.LxmlError,
        ):
            raise ThemeError(
                "theme_css",
                "invalid_markup",
                resource=resource,
            ) from None
        for node in tree.getroot().iter():
            if not isinstance(node.tag, str):
                continue
            if node.get("id") == "tn-theme-never" or not _RESERVED_ATTRIBUTES.isdisjoint(
                node.attrib
            ):
                detail = "reserved_id" if node.get("id") == "tn-theme-never" else "reserved_marker"
                raise ThemeError("theme_collision", detail, resource=resource)
            if local_name(node.tag).lower() != "link":
                continue
            try:
                linked = resolve_epub_href(resource, str(node.get("href", "")))
            except ValueError:
                continue
            if not linked.external and linked.resource_href.startswith(prefix):
                raise ThemeError(
                    "theme_collision",
                    "reserved_resource",
                    resource=resource,
                )


def _manifest(root: etree._Element) -> etree._Element | None:
    return next(
        (
            node
            for node in root.iter()
            if isinstance(node.tag, str) and local_name(node.tag) == "manifest"
        ),
        None,
    )


def _namespace_tag(parent: etree._Element, name: str) -> str:
    tag = parent.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return f"{{{tag[1:].split('}', 1)[0]}}}{name}"
    return name


def _expected_link(plan: ResourceThemePlan) -> tuple[tuple[str, str], ...]:
    relative = quote(
        posixpath.relpath(plan.css_path, posixpath.dirname(plan.resource_href) or "."),
        safe="/",
    )
    return (("rel", "stylesheet"), ("type", "text/css"), ("href", relative))


def _validate_plan_shape(plan: ThemePlan, opf_dir: str) -> None:
    if not isinstance(plan.bilingual, bool):
        raise _invalid_plan()
    seen_resources: set[str] = set()
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    prefix = f"{opf_dir}/tn-theme" if opf_dir else "tn-theme"
    for ordinal, resource in enumerate(plan.resources):
        expected_path = f"{prefix}/style-{ordinal}.css"
        expected_id = f"tn-theme-style-{ordinal}"
        if (
            resource.resource_href in seen_resources
            or resource.css_path in seen_paths
            or resource.css_id in seen_ids
            or resource.css_path != expected_path
            or resource.css_id != expected_id
            or resource.link_attributes != _expected_link(resource)
        ):
            raise _invalid_plan(resource.resource_href)
        seen_resources.add(resource.resource_href)
        seen_paths.add(resource.css_path)
        seen_ids.add(resource.css_id)


def _apply_resource(tree: etree._ElementTree, plan: ResourceThemePlan) -> None:
    root = tree.getroot()
    for change in plan.markers:
        node = resolve_element_path(root, change.path)
        for name, value in change.attributes:
            node.set(name, value)
    for change in plan.inline_changes:
        resolve_element_path(root, change.path).set("style", change.after)
    head = resolve_element_path(root, plan.head_path)
    link = etree.Element(_namespace_tag(head, "link"))
    for name, value in plan.link_attributes:
        link.set(name, value)
    head.append(link)


def _apply_manifest(
    tree: etree._ElementTree, plans: Sequence[ResourceThemePlan], opf_dir: str
) -> None:
    manifest = _manifest(tree.getroot())
    if manifest is None:
        raise _invalid_plan()
    for plan in plans:
        item = etree.Element(_namespace_tag(manifest, "item"))
        item.set("id", plan.css_id)
        item.set(
            "href",
            quote(posixpath.relpath(plan.css_path, opf_dir or "."), safe="/"),
        )
        item.set("media-type", "text/css")
        manifest.append(item)


def _write_member(
    archive: MetadataZipFile,
    info: zipfile.ZipInfo,
    data: bytes,
    *,
    compress_type: int | None = None,
) -> None:
    copied = copy(info)
    if compress_type is not None:
        copied.compress_type = compress_type
    archive.writestr(copied, data)


def _write_css(archive: MetadataZipFile, plan: ResourceThemePlan) -> None:
    info = zipfile.ZipInfo(plan.css_path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o600 << 16
    archive.writestr(info, plan.css)


class ThemeService:
    """使用不可变主题资源生成针对具体元素的排版计划，验证后再应用到 EPUB。"""

    __slots__ = ("_bilingual_rules", "_general_rules", "bundle")

    def __init__(self, bundle: ThemeBundle) -> None:
        if (bundle.script is None) != (bundle.general_css is None):
            raise ThemeError("theme_config", "theme_pair_required")
        try:
            validate_script(bundle)
        except ThemeError as error:
            error.resource = "override_theme.rules"
            raise
        self.bundle = bundle
        self._general_rules = (
            parse_theme_css(bundle.general_css, resource="override_theme.styles")
            if bundle.general_css is not None
            else ()
        )
        self._bilingual_rules = (
            parse_theme_css(bundle.bilingual_css, resource="bilingual_styles")
            if bundle.bilingual_css is not None
            else ()
        )

    def _active_rules(self, bilingual: bool) -> tuple[tuple[CssRule, ...], tuple[CssRule, ...]]:
        return self._general_rules, self._bilingual_rules if bilingual else ()

    def plan_archive(
        self,
        path: str,
        scopes: Mapping[str, ResourceThemeScope],
        *,
        bilingual: bool,
    ) -> ThemePlan | None:
        general_rules, bilingual_rules = self._active_rules(bilingual)
        if self.bundle.script is None and not bilingual_rules:
            return None
        try:
            archive = zipfile.ZipFile(path, "r")
        except (OSError, ValueError, zipfile.BadZipFile):
            raise _invalid_archive() from None
        with archive:
            try:
                infos = preflight_zip(archive, read_members=False)
            except (OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
                raise _invalid_archive() from None
            if not _valid_mimetype(archive, infos):
                raise _invalid_archive()
            members = _member_ledger(archive, infos)
            package = _read_package_or_fail(archive)
            opf_path = str(package.get("opf_path", ""))
            if not opf_path or opf_path not in dict(members):
                raise _invalid_archive()
            model = package["model"]
            assert isinstance(model, dict)
            content_paths = {str(item["path"]) for item in _html_resources(model)}
            if any(key not in content_paths for key in scopes):
                raise ThemeError("theme_css", "invalid_scope")
            opf_dir = posixpath.dirname(opf_path)
            _check_collisions(archive, infos, model, opf_dir)
            opf_data = read_member(archive, archive.getinfo(opf_path))
            opf_tree = _parse_xml(opf_data, resource=opf_path)
            package_protected, fixed = _opf_semantics(opf_tree, model)

            resources: list[ResourceThemePlan] = []
            warnings: list[tuple[str, str]] = []
            roles: Counter[str] = Counter()
            protected_count = 0
            for item in _html_resources(model):
                resource = str(item["path"])
                data = read_member(archive, archive.getinfo(resource))
                scope = scopes.get(resource, ResourceThemeScope())
                ordinal = len(resources)
                css_path = (
                    f"{opf_dir}/tn-theme/style-{ordinal}.css"
                    if opf_dir
                    else f"tn-theme/style-{ordinal}.css"
                )
                try:
                    planned, resource_warnings, resource_roles, count = plan_resource(
                        archive,
                        data,
                        resource,
                        scope,
                        self.bundle,
                        general_rules,
                        bilingual_rules,
                        ordinal=ordinal,
                        css_path=css_path,
                        package_protected=resource in package_protected or resource in fixed,
                    )
                except ThemeError as error:
                    error.resource = error.resource or resource
                    raise
                if resource in fixed:
                    warnings.append(("fixed_layout", resource))
                if planned is not None:
                    resources.append(planned)
                warnings.extend(resource_warnings)
                roles.update(resource_roles)
                protected_count += count
            if general_rules and not roles:
                warnings.append(("zero_role_coverage", "<archive>"))
            plan = ThemePlan(
                opf_path=opf_path,
                opf_before_tree_sha256=tree_sha256(opf_tree, resource=opf_path),
                members=members,
                resources=tuple(resources),
                warnings=tuple(warnings),
                role_counts=tuple(sorted(roles.items())),
                protected_count=protected_count,
                bilingual=bilingual,
            )
            _validate_plan_shape(plan, opf_dir)
            return plan

    def _admit_resource(
        self,
        archive: zipfile.ZipFile,
        data: bytes,
        resource: ResourceThemePlan,
        ordinal: int,
        bilingual: bool,
        protected: bool,
    ) -> None:
        # 复用编译策略校验地址和内联优先级，不重新执行用户脚本。
        try:
            expected, _, _, _ = plan_resource(
                archive,
                data,
                resource.resource_href,
                resource.scope,
                self.bundle,
                *self._active_rules(bilingual),
                ordinal=ordinal,
                css_path=resource.css_path,
                package_protected=protected,
                admitted_markers=resource.markers,
            )
        except ThemeError:
            raise _invalid_plan(resource.resource_href) from None
        if expected != resource:
            raise _invalid_plan(resource.resource_href)

    def apply_archive(self, path: str, plan: ThemePlan) -> None:
        self._apply_archive(path, plan)

    def _apply_archive(self, path: str, plan: ThemePlan) -> None:
        temp_path: str | None = None
        try:
            source_archive = zipfile.ZipFile(path, "r")
        except (OSError, ValueError, zipfile.BadZipFile):
            raise _invalid_archive() from None
        try:
            with source_archive as source:
                try:
                    infos = preflight_zip(source, read_members=False)
                except (OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
                    raise _invalid_archive() from None
                members = _member_ledger(source, infos)
                package = _read_package_or_fail(source)
                if not _valid_mimetype(source, infos):
                    raise _invalid_archive()
                if members != plan.members or package.get("opf_path") != plan.opf_path:
                    raise _invalid_plan()
                model = package["model"]
                assert isinstance(model, dict)
                declared_html = {str(item["path"]) for item in _html_resources(model)}
                if any(resource.resource_href not in declared_html for resource in plan.resources):
                    raise _invalid_plan()
                try:
                    opf_data = read_member(source, source.getinfo(plan.opf_path))
                except (KeyError, OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
                    raise _invalid_archive() from None
                opf_tree = _parse_xml(opf_data, resource=plan.opf_path)
                opf_digest = tree_sha256(opf_tree, resource=plan.opf_path)
                protected, fixed = _opf_semantics(opf_tree, model)
                if opf_digest != plan.opf_before_tree_sha256:
                    raise _invalid_plan(plan.opf_path)
                opf_dir = posixpath.dirname(plan.opf_path)
                _validate_plan_shape(plan, opf_dir)
                try:
                    _check_collisions(source, infos, model, opf_dir)
                except ThemeError:
                    raise _invalid_plan() from None

                replacements: dict[str, bytes] = {}
                for ordinal, resource in enumerate(plan.resources):
                    try:
                        info = source.getinfo(resource.resource_href)
                        data = read_member(source, info)
                    except (KeyError, OSError, ValueError, ZipSafetyError, zipfile.BadZipFile):
                        raise _invalid_archive() from None
                    try:
                        tree, mode = parse_source_markup(data)
                    except (ValueError, etree.LxmlError):
                        raise ThemeError(
                            "theme_css",
                            "invalid_markup",
                            resource=resource.resource_href,
                        ) from None
                    if (
                        hashlib.sha256(data).hexdigest() != resource.before_sha256
                        or mode != resource.parse_mode
                        or tree_sha256(tree, resource=resource.resource_href)
                        != resource.before_tree_sha256
                    ):
                        raise _invalid_plan(resource.resource_href)
                    validate_resource_plan(
                        tree,
                        resource,
                        package_protected=resource.resource_href in protected | fixed,
                    )
                    self._admit_resource(
                        source,
                        data,
                        resource,
                        ordinal,
                        plan.bilingual,
                        resource.resource_href in protected | fixed,
                    )
                    _apply_resource(tree, resource)
                    replacements[resource.resource_href] = serialize_source_tree(
                        tree, data, resource.parse_mode
                    )
                _apply_manifest(opf_tree, plan.resources, opf_dir)
                replacements[plan.opf_path] = serialize_source_tree(opf_tree, opf_data, "xml")
                if not plan.resources:
                    return

                descriptor, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(path)}.theme-",
                    suffix=".tmp",
                    dir=os.path.dirname(os.path.abspath(path)),
                )
                os.close(descriptor)
                with MetadataZipFile(temp_path, "w") as target:
                    target.comment = source.comment
                    for info in infos:
                        data = replacements.get(info.filename)
                        if data is None:
                            data = read_member(source, info)
                        compression = zipfile.ZIP_STORED if info.filename == "mimetype" else None
                        _write_member(target, info, data, compress_type=compression)
                    for resource in plan.resources:
                        _write_css(target, resource)
            os.replace(temp_path, path)
            temp_path = None
        except ThemeError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, ZipSafetyError, etree.LxmlError):
            raise ThemeError("theme_css", "archive_write_failed") from None
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def render(
        self,
        path: str,
        scopes: Mapping[str, ResourceThemeScope],
        *,
        bilingual: bool,
    ) -> ThemePlan | None:
        plan = self.plan_archive(path, scopes, bilingual=bilingual)
        if plan is not None:
            self.apply_archive(path, plan)
        return plan
