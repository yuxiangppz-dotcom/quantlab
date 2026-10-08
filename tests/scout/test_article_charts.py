import hashlib
import json

import pytest

from quantlab.scout.article_charts import VERSION, load_charts, visual_errors


def capture(tmp_path):
    # Unit contract specimen, not evidence of a real stock or a live vision call.
    image = tmp_path / "capture.jpg"
    image.write_bytes(b"\xff\xd8\xffsynthetic-test")
    row = {
        "ts_code": "600001.SH",
        "captured_at": "2026-10-08T18:00:00+08:00",
        "data_asof": "2026-10-08T15:00:00+08:00",
        "source_url": "https://example.com/stock",
        "visible_dates": ["2026-09-29", "2026-09-30", "2026-10-08"],
        "image_file": image.name,
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "stock_visible": True,
        "dates_visible": True,
        "price_axis_visible": True,
        "price_line_visible": True,
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": VERSION, "charts": [row]}))
    return path, row


def test_chart_cutoff_and_sha_not_just_url(tmp_path):
    manifest, row = capture(tmp_path)
    sessions = row["visible_dates"]
    packet = load_charts(
        manifest, [row["ts_code"]], sessions, "2026-10-08", "2026-10-08T19:00:00+08:00"
    )[row["ts_code"]]
    assert packet["chart_status"] == "complete"
    assert packet["images"][0]["data_url"].startswith("data:image/jpeg;base64,")
    with pytest.raises(ValueError, match="after"):
        load_charts(manifest, [row["ts_code"]], sessions, "2026-10-08", "2026-10-08T17:00:00+08:00")
    (tmp_path / row["image_file"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        load_charts(manifest, [row["ts_code"]], sessions, "2026-10-08", "2026-10-08T19:00:00+08:00")


def test_partial_day_cannot_confirm_window(tmp_path):
    manifest, row = capture(tmp_path)
    row["visible_dates"] = ["2026-10-08"]
    manifest.write_text(json.dumps({"version": VERSION, "charts": [row]}))
    packet = load_charts(
        manifest, [row["ts_code"]], row["visible_dates"], "2026-10-08", "2026-10-08T19:00:00+08:00"
    )[row["ts_code"]]
    assert packet["chart_status"] == "partial"
    assert any(
        item["code"] == "partial_window_cannot_be_good"
        for item in visual_errors(
            {
                "intraday_quality": "good",
                "image_ids": packet["image_ids"],
                "per_day_findings": [{"date": "2026-10-08"}],
            },
            packet,
        )
    )


def test_target_chart_does_not_rewrite_prior_signal(tmp_path):
    manifest, row = capture(tmp_path)
    row["visible_dates"].append("2026-10-09")
    manifest.write_text(json.dumps({"version": VERSION, "charts": [row]}))
    with pytest.raises(ValueError, match="Target-day"):
        load_charts(
            manifest,
            [row["ts_code"]],
            row["visible_dates"],
            "2026-10-08",
            "2026-10-09T19:00:00+08:00",
        )
