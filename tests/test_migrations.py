import sqlite3

from autoresearch.storage import ResearchRepository


def test_empty_database_migrates_idempotently(tmp_path):
    path = tmp_path / "research.sqlite3"
    first = ResearchRepository(path, tmp_path / "artifacts")
    second = ResearchRepository(path, tmp_path / "artifacts")
    assert first.integrity_check() == "ok"
    assert second.row("SELECT MAX(version) AS version FROM schema_migrations")["version"] == 1
    tables = {row["name"] for row in second.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "pipeline_runs", "papers", "paper_reviews", "evidence_claims", "research_gaps", "provider_usage", "artifacts"} <= tables


def test_existing_empty_file_migrates_without_data_loss(tmp_path):
    path = tmp_path / "existing.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE preserved(id TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO preserved VALUES('one','keep')")
    connection.commit(); connection.close()
    repository = ResearchRepository(path, tmp_path / "artifacts")
    assert repository.row("SELECT value FROM preserved WHERE id='one'")["value"] == "keep"
