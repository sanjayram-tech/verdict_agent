"""JIRA_PROJECT supplies the project key when --project is omitted."""
import pytest

from tyn import cli, config, importer


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("JIRA_PROJECT", raising=False)


def test_default_project_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT", " kan ")
    assert config.default_project() == "KAN"


def test_default_project_unset():
    assert config.default_project() is None


def test_importer_requires_project_without_env():
    with pytest.raises(SystemExit) as exc:
        importer.main(["verify", "--dataset", "inputs/dataset.json"])
    assert exc.value.code == 2


def test_importer_uses_env_project(monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT", "KAN")
    seen = {}
    monkeypatch.setattr(importer, "load_dataset", lambda path: {})
    monkeypatch.setattr(importer, "run_verify", lambda data, project: seen.setdefault("project", project) and 0)
    importer.main(["verify", "--dataset", "x.json"])
    assert seen["project"] == "KAN"


def test_importer_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT", "KAN")
    seen = {}
    monkeypatch.setattr(importer, "load_dataset", lambda path: {})
    monkeypatch.setattr(importer, "run_verify", lambda data, project: seen.setdefault("project", project) and 0)
    importer.main(["verify", "--dataset", "x.json", "--project", "TYN"])
    assert seen["project"] == "TYN"


def test_cli_missing_project_mentions_env(capsys):
    rc = cli.main(["assess", "--label", "L", "--release", "R2.4"])
    assert rc != 0
    assert "JIRA_PROJECT" in capsys.readouterr().err
