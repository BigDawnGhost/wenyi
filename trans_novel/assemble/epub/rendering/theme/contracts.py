from __future__ import annotations

from dataclasses import dataclass

ElementPath = tuple[int, ...]


class ThemeError(ValueError):
    """提供稳定的主题错误，不在消息中泄露用户或书籍内容。"""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        resource: str | None = None,
        node_id: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.resource = resource
        self.node_id = node_id


@dataclass(frozen=True, slots=True)
class RoleAssignment:
    node_id: int
    role: str
    level: int | None = None


@dataclass(frozen=True, slots=True)
class ThemeBundle:
    script: bytes | None
    general_css: bytes | None
    bilingual_css: bytes | None
    digest: str
    engine_version: str
    api_version: int
    policy_version: str
    provenance: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class SourcePair:
    source_path: ElementPath
    target_paths: tuple[ElementPath, ...]
    map_descendants: bool = False


@dataclass(frozen=True, slots=True)
class ResourceThemeScope:
    excluded_paths: tuple[ElementPath, ...] = ()
    source_pairs: tuple[SourcePair, ...] = ()
    preserve_resource: bool = False


@dataclass(frozen=True, slots=True)
class MarkerChange:
    path: ElementPath
    attributes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class InlineChange:
    path: ElementPath
    before: str
    after: str
    declarations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResourceThemePlan:
    resource_href: str
    before_sha256: str
    before_tree_sha256: str
    parse_mode: str
    scope: ResourceThemeScope
    markers: tuple[MarkerChange, ...]
    inline_changes: tuple[InlineChange, ...]
    css_path: str
    css_id: str
    css: bytes
    head_path: ElementPath
    link_attributes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ThemePlan:
    opf_path: str
    opf_before_tree_sha256: str
    members: tuple[tuple[str, str], ...]
    resources: tuple[ResourceThemePlan, ...]
    warnings: tuple[tuple[str, str], ...]
    role_counts: tuple[tuple[str, int], ...]
    protected_count: int
    bilingual: bool
