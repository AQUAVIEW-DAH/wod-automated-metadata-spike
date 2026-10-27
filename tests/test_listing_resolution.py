"""Getting from what a person cites (a landing page, a number, a tarball) to the files."""
import io
import tarfile
import zipfile

import httpx
import pytest

import importlib

from wod_etl.discovery import list_remote_directory, resolve_accession_url

# The package re-exports a function under the module's own name, so import the module itself.
lrd_module = importlib.import_module("wod_etl.discovery.list_remote_directory")
from wod_etl.http_util import get_with_retries

LANDING_HTML = """<html><script type="application/ld+json">
{"url" : "ftp://ftp-oceans.ncei.noaa.gov/nodc/archive/arc0238/0303535/"}</script>
<a href="https://www.ncei.noaa.gov/archive/accession/download/303535">HTTPS</a>
<a href="ftp://ftp-oceans.ncei.noaa.gov/nodc/archive/arc0238/0303535/">FTP</a></html>"""

ROOT = "https://www.ncei.noaa.gov/data/oceans/archive/arc0238/0303535/"
INDEX = {
    ROOT: '<a href="/data/oceans/archive/arc0238/">Parent</a>'
          '<a href="0303535.1.1.xml">x</a><a href="1.1/">1.1/</a><a href="2.2/">2.2/</a>',
    ROOT + "2.2/": '<a href="NCEI-Readme.txt">r</a><a href="data/">data/</a>',
    ROOT + "2.2/data/": '<a href="tsg.csv">tsg.csv</a>',
    ROOT + "1.1/": '<a href="old.csv">old.csv</a>',
}


class _Resp:
    def __init__(self, text):
        self.text = text


@pytest.fixture
def fake_ncei(monkeypatch):
    calls = []

    def fake_get(url, **_kw):
        calls.append(url)
        if "landing-page" in url:
            return _Resp(LANDING_HTML)
        if url in INDEX:
            return _Resp(INDEX[url])
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url),
                                    response=httpx.Response(404))

    monkeypatch.setattr(lrd_module, "get_with_retries", fake_get)
    return calls


class TestRetries:
    def test_a_503_is_retried_then_succeeds(self):
        n = {"calls": 0}

        def handler(request):
            n["calls"] += 1
            return httpx.Response(503 if n["calls"] < 3 else 200, text="ok")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            resp = get_with_retries("https://x.test/", retries=3, client=client)
        assert resp.text == "ok" and n["calls"] == 3

    def test_a_404_is_not_retried(self):
        n = {"calls": 0}

        def handler(request):
            n["calls"] += 1
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                get_with_retries("https://x.test/", retries=3, client=client)
        assert n["calls"] == 1

    def test_persistent_503_raises_after_every_attempt(self):
        n = {"calls": 0}

        def handler(request):
            n["calls"] += 1
            return httpx.Response(503)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                get_with_retries("https://x.test/", retries=3, client=client)
        assert n["calls"] == 3


class TestLandingPageResolution:
    @pytest.mark.parametrize("ref", [
        "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.nodc:303535",
        "https://www.ncei.noaa.gov/archive/accession/0303535",
        "0303535", "303535"])
    def test_every_citation_form_resolves_to_the_data_directory(self, fake_ncei, ref):
        r = resolve_accession_url(ref)
        assert r["url"] == ROOT and r["arc"] == "arc0238"

    def test_a_landing_page_is_listed_through_its_latest_version(self, fake_ncei):
        res = list_remote_directory("0303535", recursive=True)
        names = {i["name"] for i in res["items"]}
        assert "tsg.csv" in names and "old.csv" not in names
        assert res["resolution"]["arc"] == "arc0238"
        assert res["version"]["chosen"] == "2.2"
        assert res["version"]["available"] == ["1.1", "2.2"]
        assert res["source"] == ROOT + "2.2/"

    def test_an_unreadable_landing_page_still_names_the_page_to_open(self, monkeypatch):
        def down(url, **_kw):
            raise httpx.ConnectError("503 forever")
        monkeypatch.setattr(lrd_module, "get_with_retries", down)
        res = list_remote_directory("0303535")
        assert "could not find the data directory for accession 303535" in res["error"]
        assert "landing-page" in res["error"] and "geoportal" in res["error"]
        assert "archive/accession/download/303535" in res["hint"]

    def test_the_metadata_catalogue_is_asked_first_and_answers_when_the_page_does_not(
            self, fake_ncei, monkeypatch):
        """The landing page can time out; the catalogue's ISO record names the same bundle."""
        real = lrd_module.get_with_retries

        def landing_down(url, **kw):
            if "landing-page" in url:
                raise httpx.ReadTimeout("The read operation timed out")
            if "geoportal" in url:
                return _Resp("<gmd:URL>https://www.ncei.noaa.gov/data/oceans/archive/arc0238/"
                             "0303535/</gmd:URL>")
            return real(url, **kw)
        monkeypatch.setattr(lrd_module, "get_with_retries", landing_down)
        r = resolve_accession_url(
            "https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.nodc:303535")
        assert r["url"] == ROOT and "geoportal" in r["resolved_from"]
        assert not any("landing-page" in c for c in fake_ncei)

    def test_process_accession_keys_are_relative_to_the_resolved_version(self, fake_ncei,
                                                                        tmp_path):
        from wod_etl.orchestration import process_accession

        res = process_accession("0303535", output_dir=str(tmp_path / "o"),
                                raw_dir=str(tmp_path / "r"), dry_run=True, resume=False)
        keys = {w["key"] for w in res["would_process"]} | {w["key"] for w in res["would_skip"]}
        assert "data/tsg.csv" in keys and "NCEI-Readme.txt" in keys


class TestLocalArchives:
    @pytest.fixture
    def raw(self, tmp_path, monkeypatch):
        d = tmp_path / "raw"
        d.mkdir()
        monkeypatch.setattr(lrd_module, "RAW_DIR", d)
        return d

    def test_a_tarball_is_extracted_under_raw_and_listed(self, tmp_path, raw):
        tgz = tmp_path / "0303928.tar.gz"
        with tarfile.open(tgz, "w:gz") as t:
            data = b"Depth (m),Temp (deg C)\n0,20\n"
            info = tarfile.TarInfo("0303928/data/0-data/cast.csv")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        res = list_remote_directory(str(tgz), recursive=True)
        assert res["extracted_from"] == str(tgz)
        assert any(i["name"] == "cast.csv" for i in res["items"])
        assert (raw / "0303928" / "0303928" / "data" / "0-data" / "cast.csv").exists()

    def test_a_zip_member_escaping_the_target_is_refused(self, tmp_path, raw):
        z = tmp_path / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("ok.csv", "a,b\n1,2\n")
            zf.writestr("../escape.csv", "x")
        res = list_remote_directory(str(z), recursive=True)
        assert res["refused_members"] == ["../escape.csv"]
        assert not (raw / "escape.csv").exists() and not (tmp_path / "escape.csv").exists()


class TestAccessionFor:
    @pytest.mark.parametrize("text,expected", [
        ("0303928", "0303928"), ("303928", "0303928"),
        ("https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso"
         "?id=gov.noaa.nodc:303535", "0303535"),
        ("https://www.ncei.noaa.gov/archive/accession/0303535", "0303535"),
        ("https://www.ncei.noaa.gov/data/oceans/archive/arc0238/0303535/2.2/", "0303535"),
        ("data/raw/0303928", "0303928"),
        ("data/staging/0303928/CTD_20261012", "0303928"),
        ("downloads/0303928.tar.gz", "0303928"),
        ("data/raw/0303928/data/0-data/U2015-015_CTD_Temp.csv", "0303928"),
        ("data/raw/t1", None), ("", None),
    ])
    def test_every_form_gives_the_zero_padded_id(self, text, expected):
        from wod_etl.discovery import accession_for

        assert accession_for(text) == expected
