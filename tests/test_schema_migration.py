"""Additive schema migration from v16 to v17 (monitor contract)."""

from __future__ import annotations

import sqlite3

import autoresearch.storage.repository as repository_module
from autoresearch.storage.repository import ResearchRepository


def test_v16_database_migrates_additively_to_v17(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version in range(1, 17):
        connection.executescript(getattr(repository_module, f"_SCHEMA_V{version}"))
        connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, "2026-01-01"))
    connection.commit()
    connection.close()

    # Instantiating the repository runs the migration exactly as an upgraded
    # user's existing project database would.
    ResearchRepository(db, tmp_path / "artifacts")

    connection = sqlite3.connect(db)
    versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
    columns = {
        row[1]: row[4]
        for row in connection.execute("PRAGMA table_info(experiment_specs)")
    }
    connection.close()

    assert versions == list(range(1, 18))
    assert "monitor_contract_json" in columns
    assert "required_artifacts_json" in columns
    assert str(columns["monitor_contract_json"] or "").strip("'") == "{}"
    assert columns["required_artifacts_json"] is None


def test_partial_v17_migration_is_idempotent(tmp_path):
    db = tmp_path / "partial.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version in range(1, 17):
        connection.executescript(getattr(repository_module, f"_SCHEMA_V{version}"))
        connection.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)", (version, "2026-01-01"))
    # Simulate a crash after the first ALTER: the version row is absent.
    connection.execute("ALTER TABLE experiment_specs ADD COLUMN monitor_contract_json TEXT NOT NULL DEFAULT '{}'")
    connection.commit()
    connection.close()

    ResearchRepository(db, tmp_path / "artifacts")
    connection = sqlite3.connect(db)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(experiment_specs)")}
    version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    connection.close()
    assert "monitor_contract_json" in columns
    assert "required_artifacts_json" in columns
    assert version == 17
