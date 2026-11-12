"""Serialize glossary entries as a self-contained Anki deck package."""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from .store import GlossaryTerm

# The legacy schema-11 .apkg format remains importable by current Anki clients.
# This is an export artifact, never a Wenyi state database. Keep the format local
# to this module; no scheduler, media handling or general-purpose Anki API is needed.
# Reference: ankitects/anki, rslib/src/storage/schema11.sql.
_SCHEMA = """
CREATE TABLE col (
    id integer PRIMARY KEY, crt integer NOT NULL, mod integer NOT NULL,
    scm integer NOT NULL, ver integer NOT NULL, dty integer NOT NULL,
    usn integer NOT NULL, ls integer NOT NULL, conf text NOT NULL,
    models text NOT NULL, decks text NOT NULL, dconf text NOT NULL, tags text NOT NULL
);
CREATE TABLE notes (
    id integer PRIMARY KEY, guid text NOT NULL, mid integer NOT NULL,
    mod integer NOT NULL, usn integer NOT NULL, tags text NOT NULL,
    flds text NOT NULL, sfld integer NOT NULL, csum integer NOT NULL,
    flags integer NOT NULL, data text NOT NULL
);
CREATE TABLE cards (
    id integer PRIMARY KEY, nid integer NOT NULL, did integer NOT NULL,
    ord integer NOT NULL, mod integer NOT NULL, usn integer NOT NULL,
    type integer NOT NULL, queue integer NOT NULL, due integer NOT NULL,
    ivl integer NOT NULL, factor integer NOT NULL, reps integer NOT NULL,
    lapses integer NOT NULL, left integer NOT NULL, odue integer NOT NULL,
    odid integer NOT NULL, flags integer NOT NULL, data text NOT NULL
);
CREATE TABLE revlog (
    id integer PRIMARY KEY, cid integer NOT NULL, usn integer NOT NULL,
    ease integer NOT NULL, ivl integer NOT NULL, lastIvl integer NOT NULL,
    factor integer NOT NULL, time integer NOT NULL, type integer NOT NULL
);
CREATE TABLE graves (usn integer NOT NULL, oid integer NOT NULL, type integer NOT NULL);
CREATE INDEX ix_notes_usn ON notes (usn);
CREATE INDEX ix_cards_usn ON cards (usn);
CREATE INDEX ix_revlog_usn ON revlog (usn);
CREATE INDEX ix_cards_nid ON cards (nid);
CREATE INDEX ix_cards_sched ON cards (did, queue, due);
CREATE INDEX ix_revlog_cid ON revlog (cid);
CREATE INDEX ix_notes_csum ON notes (csum);
"""

_FIELDS = (
    "Source",
    "Target",
    "Reading",
    "Note",
    "Aliases",
    "Type",
    "Book",
    "SourceLanguage",
    "TargetLanguage",
    "Status",
)

# A valid schema-11 default preset for newly imported decks. Existing cards carry
# no review history in this package, and their scheduling is owned by Anki.
_DECK_CONFIG = {
    "id": 1,
    "name": "Default",
    "mod": 0,
    "usn": 0,
    "maxTaken": 60,
    "autoplay": True,
    "replayq": True,
    "timer": 0,
    "new": {
        "delays": [1, 10],
        "ints": [1, 4, 7],
        "initialFactor": 2500,
        "order": 1,
        "perDay": 20,
        "bury": True,
    },
    "rev": {
        "perDay": 200,
        "ease4": 1.3,
        "fuzz": 0.05,
        "ivlFct": 1,
        "maxIvl": 36500,
        "bury": True,
    },
    "lapse": {"delays": [10], "mult": 0, "minInt": 1, "leechFails": 8, "leechAction": 0},
}


def _identity(*parts: str) -> str:
    return json.dumps(("wenyi-glossary-v1", *parts), ensure_ascii=False)


def _id(*parts: str) -> int:
    # Keep IDs positive and exactly representable in both SQLite and JSON consumers.
    return int.from_bytes(hashlib.sha256(_identity(*parts).encode()).digest()[:6], "big") + 1


def _category(value: str) -> str:
    """Escape Anki hierarchy/tag separators without conflating custom categories."""
    return re.sub(r'[%:"\s\x00-\x1f\x7f]', lambda match: quote(match[0], safe=""), value)


def _field(value: str) -> str:
    """Render user text, not HTML, media directives or Anki field separators."""
    return (
        html.escape(value)
        .replace("[", "&#91;")
        .replace("\x1f", "&#31;")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def _model(model_id: int, deck_id: int, modified: int) -> dict:
    return {
        "id": str(model_id),
        "name": "Wenyi glossary v1",
        "type": 0,
        "mod": modified,
        "usn": -1,
        "sortf": 0,
        "did": deck_id,
        "flds": [
            {
                "name": name,
                "ord": index,
                "font": "Arial",
                "size": 20,
                "rtl": False,
                "sticky": False,
                "media": [],
            }
            for index, name in enumerate(_FIELDS)
        ],
        "tmpls": [
            {
                "name": "Recognize",
                "ord": 0,
                "did": None,
                "qfmt": '<div class="source">{{Source}}</div>',
                "afmt": (
                    '{{FrontSide}}<hr id="answer">'
                    '{{#Reading}}<div class="reading">{{Reading}}</div>{{/Reading}}'
                    '<div class="target">{{Target}}</div>'
                    '{{#Note}}<div class="note">{{Note}}</div>{{/Note}}'
                    '{{#Aliases}}<div class="meta">{{Aliases}}</div>{{/Aliases}}'
                    '<div class="meta">{{Book}} · {{Type}}'
                    " · {{SourceLanguage}} → {{TargetLanguage}}</div>"
                ),
                "bqfmt": "",
                "bafmt": "",
                "bfont": "",
                "bsize": 0,
            }
        ],
        "css": (
            ".card { font-family: sans-serif; font-size: 22px; text-align: center; "
            "overflow-wrap: anywhere; }"
            ".source { font-size: 1.4em; }"
            ".reading, .meta { font-size: 0.8em; opacity: 0.7; margin-top: 1em; }"
            ".target, .note { margin-top: 1em; }"
        ),
        "req": [[0, "all", [0]]],
        "latexPre": "",
        "latexPost": "",
        "latexsvg": False,
        "tags": [],
        "vers": [],
    }


def _deck(deck_id: int, name: str, modified: int) -> dict:
    return {
        "id": deck_id,
        "name": name,
        "mod": modified,
        "usn": -1,
        "desc": "",
        "dyn": 0,
        "conf": 1,
        "collapsed": False,
        "extendNew": 0,
        "extendRev": 0,
        "newToday": [0, 0],
        "revToday": [0, 0],
        "lrnToday": [0, 0],
        "timeToday": [0, 0],
    }


def export_anki_glossary(
    terms: Iterable[GlossaryTerm],
    *,
    project_id: str,
    title: str,
    source_lang: str,
    target_lang: str,
) -> bytes:
    """Export every entry as one recognition card, grouped by its stored type.

    Note identity depends only on project and source, not the current translation,
    category, title or export order. Keep the model ID and field order stable.
    """
    terms = sorted(terms, key=lambda term: term.source)
    timestamp = time.time()
    modified = int(timestamp)
    first_id = int(timestamp * 1000)
    model_id = _id("model")
    root_id = _id("deck", project_id)
    # A project suffix keeps unrelated books with the same title in separate decks.
    book_label = " ".join(title.split()).replace("::", " — ").replace('"', "'") or "Glossary"
    root_name = f"Wenyi::{book_label} [{_category(project_id)}]"
    decks = {
        1: _deck(1, "Default", modified),
        root_id: _deck(root_id, root_name, modified),
    }
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="wenyi-anki-") as directory:
        database = Path(directory) / "collection.anki2"
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript(_SCHEMA)
            for position, term in enumerate(terms, start=1):
                deck_id = _id("deck", project_id, term.type)
                if deck_id not in decks:
                    decks[deck_id] = _deck(
                        deck_id, f"{root_name}::{_category(term.type)}", modified
                    )
                fields = [
                    _field(value)
                    for value in (
                        term.source,
                        term.target,
                        term.reading,
                        term.note,
                        "\n".join(term.aliases),
                        term.type,
                        title,
                        source_lang,
                        target_lang,
                        term.status,
                    )
                ]
                tags = [
                    "wenyi",
                    f"wenyi::type::{_category(term.type)}",
                    f"wenyi::project::{_category(project_id)}",
                    f"wenyi::source::{_category(source_lang)}",
                    f"wenyi::target::{_category(target_lang)}",
                ]
                note_id = first_id + position * 2
                guid = hashlib.sha256(
                    _identity("note", project_id, term.source).encode()
                ).hexdigest()[:20]
                # The source is escaped as literal text. Anki strips our <br>
                # markers for sorting/checksums but retains the original characters.
                sort_field = term.source.replace("\r", "").replace("\n", "")
                checksum = int(hashlib.sha1(sort_field.encode()).hexdigest()[:8], 16)
                connection.execute(
                    "INSERT INTO notes VALUES (?, ?, ?, ?, -1, ?, ?, ?, ?, 0, '')",
                    (
                        note_id,
                        guid,
                        model_id,
                        modified,
                        " " + " ".join(tags) + " ",
                        "\x1f".join(fields),
                        sort_field,
                        checksum,
                    ),
                )
                # One unscheduled card per note. Anki preserves existing scheduling
                # when re-importing the same note GUID and template ordinal.
                connection.execute(
                    "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, 0, 0, ?, "
                    "0, 0, 0, 0, 0, 0, 0, 0, '')",
                    (note_id + 1, note_id, deck_id, modified, position),
                )
            conf = {
                "activeDecks": [root_id],
                "curDeck": root_id,
                "curModel": str(model_id),
                "nextPos": len(terms) + 1,
            }
            connection.execute(
                "INSERT INTO col VALUES (1, ?, ?, ?, 11, 0, 0, 0, ?, ?, ?, ?, '{}')",
                (
                    modified,
                    first_id,
                    first_id,
                    json.dumps(conf),
                    json.dumps({model_id: _model(model_id, root_id, modified)}),
                    json.dumps(decks),
                    json.dumps({1: _DECK_CONFIG}),
                ),
            )
            connection.commit()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(database, "collection.anki2")
            archive.writestr("media", "{}")
    return buffer.getvalue()
