import uuid

import psycopg
from wenyi_core.models.document import Chapter, Segment


def test_analysis_projects_only_ordered_chapter_digests_from_postgres(
    domain_client, pg_pool, monkeypatch
):
    client, storage, _ = domain_client
    path = f"/projects/{storage.project_id}/analysis"
    other = uuid.uuid4().hex
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO projects(id, name) VALUES(%s, 'Other project')", (other,))
        conn.execute(
            "INSERT INTO chapters(project_id, seq, title) VALUES(%s, 3, 'Other')", (other,)
        )
    storage.save_chapters(
        [
            Chapter(
                index=11,
                title="Last",
                meta={"source_digest": "摘要"},
                template="Template",
                segments=[Segment(index=1, source="Body", target="译文")],
            ),
            Chapter(index=2, title="First"),
            Chapter(index=7, title="Middle", meta={"source_digest": True}),
        ]
    )
    assert client.get(path).status_code == 409
    storage.save_manifest({"chapters": [{"index": 11, "title": "Last"}]})
    storage.save_analysis({"style_guide": "自然"})

    statements = []
    execute = psycopg.Connection.execute

    def track(conn, query, *args, **kwargs):
        statements.append(str(query).lower())
        return execute(conn, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", track)
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "analysis": {"style_guide": "自然"},
        "chapter_digests": [
            {"index": 2, "title": "First", "digest": ""},
            {"index": 7, "title": "Middle", "digest": "True"},
            {"index": 11, "title": "Last", "digest": "摘要"},
        ],
    }
    assert sum("from chapters" in query for query in statements) == 1
    assert not any(
        any(part in query for part in ("segments", "template", "count(")) for query in statements
    )
