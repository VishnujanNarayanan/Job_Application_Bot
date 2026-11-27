"""#154: the autofill's personal answer files are backed up to S3, never pulled by the runner."""

from __future__ import annotations

from pathlib import Path

from src.cli import assets


class FakeS3:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, dict]] = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):  # noqa: N803 - boto3's name
        self.uploads.append((Path(path).name, key, ExtraArgs or {}))


def test_backups_pushed_when_present(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(assets, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "ats_answers.yaml").write_text("answers: {}\n")
    s3 = FakeS3()
    assert assets.push_backups(s3) == 1  # the learned file doesn't exist yet: skipped
    assert s3.uploads == [("ats_answers.yaml", "assets/backup/ats_answers.yaml",
                           {"ServerSideEncryption": "AES256"})]


def test_runner_pull_never_includes_backups() -> None:
    keys = [k for _, k in assets._asset_paths()]
    assert not any("backup" in k or "ats_" in k for k in keys)


def test_backup_files_are_gitignored() -> None:
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text().splitlines()
    for p in assets._BACKUPS:
        assert str(p) in ignore
