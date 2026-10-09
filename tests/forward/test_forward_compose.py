"""§9 docker-compose `collector` service and .env.example documentation."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_collector_service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = compose["services"]["collector"]
    bot = compose["services"]["bot-perp"]
    assert svc["build"] == bot["build"] and svc["env_file"] == bot["env_file"]
    assert svc["command"] == ["collect"]
    assert svc["restart"] == "unless-stopped"
    assert "./var:/app/var" in svc["volumes"]
    assert "${LUNA_FORWARD_BACKUP_HOST_DIR:-./var/.forward_backup_unset}:/backup" in svc["volumes"]
    assert svc["environment"]["LUNA_FORWARD_BACKUP_DIR"] == "${LUNA_FORWARD_BACKUP_HOST_DIR:+/backup}"
    hc = svc["healthcheck"]
    assert hc["test"] == ["CMD", "uv", "run", "luna", "collector-health"]
    assert (hc["interval"], hc["timeout"], hc["retries"], hc["start_period"]) == ("60s", "20s", 3, "5m")


def test_env_example_documents_backup_host_dir():
    assert "LUNA_FORWARD_BACKUP_HOST_DIR=" in (ROOT / ".env.example").read_text(encoding="utf-8")
