from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from autoresearch.api.app import create_app
from autoresearch.application.config import Settings


def test_api_contract_capabilities_and_idempotent_mutation(tmp_path: Path):
    config = Path(__file__).parents[1] / "docs" / "live_config.example.toml"
    settings = Settings(
        data_root=tmp_path,
        database_path=tmp_path / "api.sqlite3",
        artifact_root=tmp_path / "artifacts",
        download_root=tmp_path / "downloads",
        provider_config=config,
        search_sources=("crossref",),
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/health").json()["status"] == "ready"
        capabilities = client.get("/v1/capabilities").json()["pipelines"]
        assert {item["key"] for item in capabilities if item["enabled"]} == {"study_search", "deep_literature_review"}
        assert all(item["reason"] for item in capabilities if not item["enabled"])

        project = client.post("/v1/projects", json={"title": "API contract"}).json()
        project_id = project["id"]
        headers = {"Idempotency-Key": "discover-contract-key"}
        first = client.post(
            f"/v1/projects/{project_id}/discover",
            headers=headers,
            json={"seed_paper": "10.48550/arXiv.2005.11401", "limit": 20},
        )
        second = client.post(
            f"/v1/projects/{project_id}/discover",
            headers=headers,
            json={"seed_paper": "10.48550/arXiv.2005.11401", "limit": 20},
        )
        assert first.status_code == 202
        assert first.json()["run_id"] == second.json()["run_id"]
        run_id = first.json()["run_id"]
        assert client.get(f"/v1/runs/{run_id}").status_code == 200
        assert client.post(f"/v1/projects/{project_id}/invent", json={}).status_code == 409


def test_api_authentication_and_secret_free_provider_status(tmp_path: Path, monkeypatch):
    config = Path(__file__).parents[1] / "docs" / "live_config.example.toml"
    settings = Settings(tmp_path, tmp_path / "api.sqlite3", tmp_path / "artifacts", tmp_path / "downloads", config, ("crossref",))
    monkeypatch.setenv("AUTORESEARCH_API_AUTH_TOKEN_ENV", "TEST_AUTORESEARCH_TOKEN")
    monkeypatch.setenv("TEST_AUTORESEARCH_TOKEN", "secret-token")
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/health").status_code == 401
        response = client.get("/v1/providers/status", headers={"Authorization": "Bearer secret-token"})
        assert response.status_code == 200
        body = response.json()
        assert all("api_key" not in str(item).lower() for item in body["providers"])
        assert all("secret-token" not in str(item) for item in body["providers"])
