import sqlite3

from autoresearch.storage import ResearchRepository


def test_empty_database_migrates_idempotently(tmp_path):
    path = tmp_path / "research.sqlite3"
    first = ResearchRepository(path, tmp_path / "artifacts")
    second = ResearchRepository(path, tmp_path / "artifacts")
    assert first.integrity_check() == "ok"
    assert second.row("SELECT MAX(version) AS version FROM schema_migrations")["version"] == 5
    assert second.row("SELECT name FROM sqlite_master WHERE type='table' AND name='api_events'") is not None
    tables = {row["name"] for row in second.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "pipeline_runs", "pipeline_checkpoints", "papers", "paper_reviews", "evidence_claims", "research_gaps", "provider_usage", "artifacts", "novelty_candidates", "experiment_specs", "api_project_idempotency"} <= tables


def test_existing_empty_file_migrates_without_data_loss(tmp_path):
    path = tmp_path / "existing.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE preserved(id TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO preserved VALUES('one','keep')")
    connection.commit(); connection.close()
    repository = ResearchRepository(path, tmp_path / "artifacts")
    assert repository.row("SELECT value FROM preserved WHERE id='one'")["value"] == "keep"


def test_upgrade_from_v1_and_v2_and_v3(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(1, '2026-01-01T00:00:00Z')")
    from autoresearch.storage.repository import _SCHEMA_V1
    conn.executescript(_SCHEMA_V1)
    conn.commit()
    conn.close()

    repo = ResearchRepository(path, tmp_path / "artifacts")
    assert repo.integrity_check() == "ok"
    assert repo.row("SELECT MAX(version) AS version FROM schema_migrations")["version"] == 5
    tables = {row["name"] for row in repo.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "novelty_candidates" in tables
    assert "api_project_idempotency" in tables
