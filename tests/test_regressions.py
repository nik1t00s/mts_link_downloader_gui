from pathlib import Path
import pytest
from app.downloader import validate_record_url,validate_output_dir,DownloaderError

@pytest.mark.parametrize("url",["https://webinar.ru/record/1","https://events.webinar.ru:443/record/1","https://mts-link.ru/record/1"])
def test_valid_hosts(url): validate_record_url(url)

@pytest.mark.parametrize("url",["https://evilwebinar.ru/record/1","https://webinar.ru.evil.test/record/1","file:///webinar.ru","https://evil-mts-link.ru/record/1"])
def test_similar_names_are_not_mts(url):
    with pytest.raises(DownloaderError): validate_record_url(url)

def test_write_probe_preserves_existing_user_file(tmp_path):
    path=tmp_path/".mts_link_downloader_write_test"
    path.write_text("user content",encoding="utf-8")
    assert validate_output_dir(tmp_path)==tmp_path
    assert path.read_text(encoding="utf-8")=="user content"
