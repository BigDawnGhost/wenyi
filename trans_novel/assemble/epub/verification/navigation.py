"""Navigation DOM proof helpers."""

from __future__ import annotations

from lxml import etree

from trans_novel.assemble.epub.verification import archive_model, dom


def nav_label_locations(
    root: etree._Element, *, is_ncx: bool
) -> list[tuple[etree._Element, tuple[int, ...]]]:
    """Locate exactly the labels enumerated by the production TOC parser."""
    locations: list[tuple[etree._Element, tuple[int, ...]]] = []

    def direct(parent: etree._Element, name: str) -> etree._Element | None:
        return next(
            (
                child
                for child in dom.element_children_lxml(parent)
                if child.tag.rsplit("}", 1)[-1].lower() == name.lower()
            ),
            None,
        )

    if is_ncx:
        nav_map = next(
            (
                node
                for node in root.iter()
                if archive_model.local_name(node.tag).lower() == "navmap"
            ),
            None,
        )
        if nav_map is None:
            return locations

        def walk_ncx(parent: etree._Element) -> None:
            for point in dom.element_children_lxml(parent):
                if archive_model.local_name(point.tag).lower() != "navpoint":
                    continue
                label_parent = direct(point, "navLabel")
                label = (
                    next(
                        (
                            child
                            for child in label_parent.iter()
                            if isinstance(child.tag, str)
                            and archive_model.local_name(child.tag) == "text"
                        ),
                        None,
                    )
                    if label_parent is not None
                    else None
                )
                if label is not None:
                    path = dom.element_path_lxml(root, label)
                    if path is not None:
                        locations.append((label, path))
                walk_ncx(point)

        walk_ncx(nav_map)
        return locations

    navs = [
        node
        for node in root.iter()
        if isinstance(node.tag, str)
        and archive_model.local_name(node.tag).lower() == "nav"
        and "toc"
        in (
            str(node.get("epub:type", node.get("type", "")))
            + " "
            + str(node.get("{http://www.idpf.org/2007/ops}type", ""))
        ).split()
    ]
    if not navs:
        all_navs = [
            node
            for node in root.iter()
            if isinstance(node.tag, str) and archive_model.local_name(node.tag).lower() == "nav"
        ]
        navs = all_navs[:1]

    def walk_nav(ordered_list: etree._Element) -> None:
        for li in dom.element_children_lxml(ordered_list):
            if archive_model.local_name(li.tag).lower() != "li":
                continue
            label = next(
                (
                    child
                    for child in dom.element_children_lxml(li)
                    if archive_model.local_name(child.tag).lower() in {"a", "span"}
                ),
                None,
            )
            if label is not None:
                path = dom.element_path_lxml(root, label)
                if path is not None:
                    locations.append((label, path))
            nested = direct(li, "ol")
            if nested is not None:
                walk_nav(nested)

    for nav in navs:
        ordered = direct(nav, "ol")
        if ordered is None:
            ordered = next(
                (node for node in nav.iter() if archive_model.local_name(node.tag).lower() == "ol"),
                None,
            )
        if ordered is not None:
            walk_nav(ordered)
    return locations


def _allow_cleared_descendants(parent, parent_path, slot_map):
    element_index = 0
    for child in parent:
        if not isinstance(child.tag, str):
            continue
        child_path = (*parent_path, element_index)
        element_index += 1
        for field, value in (("text", child.text), ("tail", child.tail)):
            slot_map[(child_path, field)] = {
                "kind": "toc",
                "expected": None,
                "source": value,
                "count": False,
            }
        _allow_cleared_descendants(child, child_path, slot_map)


def navigation_slots(
    root_source,
    root_output,
    resource,
    toc_entries,
    slot_map,
    toc_label_paths,
    language_paths,
    source_lang,
    failures,
):
    from trans_novel.assemble.epub.metadata import translated_toc_title

    is_ncx = any(
        archive_model.local_name(node.tag).lower() == "navmap" for node in root_source.iter()
    )
    locations = nav_label_locations(root_source, is_ncx=is_ncx)
    entries = sorted(
        (
            entry
            for entry in toc_entries
            if entry.get("toc_path") == resource and isinstance(entry.get("node_index"), int)
        ),
        key=lambda entry: int(entry["node_index"]),
    )
    if entries and len(locations) != len(entries):
        failures.append(archive_model.item("nav", "label_count_mismatch", resource, "toc"))
    for entry in entries:
        index = int(entry["node_index"])
        if index < 0 or index >= len(locations):
            failures.append(archive_model.item("nav", "label_locator_missing", resource, "toc"))
            continue
        label, path = locations[index]
        toc_label_paths.add(path)
        slot_map[(path, "text")] = {
            "kind": "toc",
            "expected": translated_toc_title(entry),
            "source": label.text,
            "count": True,
        }
        _allow_cleared_descendants(label, path, slot_map)
        if entry.get("preserve_source") is True and source_lang and not is_ncx:
            language_paths.add(path)
            output_label = dom.resolve_path_lxml(root_output, path)
            languages = (
                {
                    key: value
                    for key, value in output_label.attrib.items()
                    if key.rsplit("}", 1)[-1].split(":", 1)[-1] == "lang"
                }
                if output_label is not None
                else {}
            )
            if not languages or any(value != source_lang for value in languages.values()):
                failures.append(
                    archive_model.item("nav", "label_language_mismatch", resource, "source")
                )
