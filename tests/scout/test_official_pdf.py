"""Bounded official PDF text is separately attributed and never backdated."""

from datetime import datetime
from unittest.mock import Mock, patch

from quantlab.scout.models import SHANGHAI, Evidence
from quantlab.scout.pipeline import DEFAULT_CONFIG, evidence_packet
from quantlab.scout.report import announcement_index_lines, announcement_timing_note
from quantlab.scout.sources import collect_cninfo_pdf_bodies, read_cninfo_pdf

NOW = datetime(2026, 1, 10, 12, tzinfo=SHANGHAI)
URL = "https://static.cninfo.com.cn/finalpage/2026-01-10/1225586847.PDF"


def notice():
    return Evidence(
        "cninfo:official_index",
        "股票交易异常波动公告",
        "仅公告索引",
        URL,
        None,
        NOW.isoformat(),
        kind="official_announcement_index_unverified",
        instrument_ids=("600241.SH",),
        event_dates=("2026-01-10",),
    )


def test_official_pdf_url_is_fixed_before_network():
    with patch("urllib.request.build_opener") as opener:
        try:
            read_cninfo_pdf("https://static.cninfo.com.cn.evil.test/finalpage/2026-01-10/1.PDF")
        except ValueError:
            pass
        else:
            raise AssertionError("foreign PDF host accepted")
    opener.assert_not_called()


def test_pdf_text_is_bounded_attributed_and_unverified():
    config = DEFAULT_CONFIG | {"cninfo_announcements": True, "cninfo_pdf_bodies": True}
    page = Mock()
    page.extract_text.return_value = "归属于上市公司股东的净利润-1,245.99万元，转为亏损。" * 5
    reader = Mock(is_encrypted=False, pages=[page])
    with (
        patch("quantlab.scout.sources.read_cninfo_pdf", return_value=b"%PDF-test"),
        patch("pypdf.PdfReader", return_value=reader),
    ):
        found, coverage = collect_cninfo_pdf_bodies(config, NOW, True, [notice()])
    assert coverage.status == "targeted_only"
    assert len(found) == 1
    assert found[0].kind == "official_pdf_text_unverified"
    assert found[0].published_at is None
    assert found[0].url == URL
    assert "净利润-1,245.99万元" in found[0].body
    packet = evidence_packet(found, {"600241.SH"}, 4000, 1400)
    assert len(packet) == 1 and packet[0]["evidence_id"] == found[0].evidence_id
    assert "净利润-1,245.99万元" in packet[0]["body"]
    report = {
        "market": {"session": "2026-01-09"},
        "evidence": [notice().to_dict(), found[0].to_dict()],
    }
    assert "其中1条PDF正文" in announcement_timing_note(report)
    assert (
        "机器提取正文、未人工核实" in announcement_index_lines(report["evidence"], "600241.SH")[0]
    )


def test_pdf_disabled_does_not_download():
    with patch("quantlab.scout.sources.read_cninfo_pdf") as fetch:
        found, coverage = collect_cninfo_pdf_bodies(DEFAULT_CONFIG, NOW, True, [notice()])
    assert found == [] and coverage.status == "not_configured"
    fetch.assert_not_called()
