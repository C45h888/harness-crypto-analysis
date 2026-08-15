from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from check_write_boundary import check_write_boundary, snapshot_baseline


def test_clean_repo_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Simulate a clean repo by passing an empty baseline that records
    # "no changes", and a synthesized git-status output that has nothing.
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text("")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert report.ok, f"unexpected violations: {report.violations}"


def test_untouched_allowed_path_passes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M docs/nooa-kb/from-monorepo/packages-nooa/exports.md\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert report.ok, f"allowed path flagged: {report.violations}"


def test_violation_in_market_service_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M market_service/nooa_harness/agents.py\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert not report.ok
    assert any("market_service" in v for v in report.violations)


def test_violation_in_requirements_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M requirements.txt\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert not report.ok
    assert any("requirements.txt" in v for v in report.violations)


def test_violation_in_build_config_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("")
    fake_status = tmp_path / "status.txt"
    fake_status.write_text(" M docker-compose.yml\n")
    report = check_write_boundary(str(baseline), git_status_path=str(fake_status))
    assert not report.ok
    assert any("docker-compose.yml" in v for v in report.violations)