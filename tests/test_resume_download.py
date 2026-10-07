"""#42 — resumes open and save under a readable name, not their cache key."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src import resume_download as rd


@pytest.fixture(autouse=True)
def _fresh_name_cache():
    rd._candidate_name.cache_clear()
    yield
    rd._candidate_name.cache_clear()


def test_pdf_opens_in_the_browser_under_the_candidates_name():
    with patch.object(rd, "_candidate_name", return_value="Vishnujan Narayanan"):
        assert rd.content_disposition("pdf") == 'inline; filename="Vishnujan_Narayanan_Resume.pdf"'


def test_docx_downloads_under_the_same_name():
    with patch.object(rd, "_candidate_name", return_value="Vishnujan Narayanan"):
        assert rd.content_disposition("docx") == 'attachment; filename="Vishnujan_Narayanan_Resume.docx"'


@pytest.mark.parametrize("name,stem", [
    ('Ann "Quote" O\'Neil', "Ann_Quote_O_Neil_Resume"),   # nothing that breaks the header
    ("Zoë / Doe", "Zo_Doe_Resume"),
    ("", "Resume"),                                       # no profile: still a sane name
])
def test_names_are_made_header_and_filesystem_safe(name, stem):
    with patch.object(rd, "_candidate_name", return_value=name):
        assert rd.download_stem() == stem


def test_the_template_is_configurable(monkeypatch):
    from src.config import settings

    monkeypatch.setitem(settings.storage._data, "resume_download_name", "CV_{name}")
    with patch.object(rd, "_candidate_name", return_value="A B"):
        assert rd.download_stem() == "CV_A_B"


def test_presigned_links_carry_the_type_and_name():
    from src.aws import s3

    client = MagicMock()
    client.generate_presigned_url.return_value = "https://signed"
    with patch.object(s3, "_s3", return_value=client), \
         patch.object(s3, "_bucket", return_value="b"), \
         patch.object(rd, "_candidate_name", return_value="A B"):
        assert s3.cache_presigned_url("key1", "pdf", 600) == "https://signed"

    params = client.generate_presigned_url.call_args.kwargs["Params"]
    assert params["Key"] == "pdf_cache/key1.pdf"
    assert params["ResponseContentType"] == "application/pdf"
    assert params["ResponseContentDisposition"] == 'inline; filename="A_B_Resume.pdf"'


def test_uploads_are_stored_with_a_real_content_type(tmp_path):
    from src.aws import s3

    f = tmp_path / "r.docx"
    f.write_bytes(b"x")
    client = MagicMock()
    with patch.object(s3, "_s3", return_value=client), patch.object(s3, "_bucket", return_value="b"):
        s3.cache_put(f, "key1", "docx")

    assert client.upload_file.call_args.kwargs["ExtraArgs"] == {
        "ContentType": rd.CONTENT_TYPES["docx"]
    }
