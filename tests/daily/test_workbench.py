from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from quantlab.personal.tracking import FILL_COLUMNS
from quantlab.ui import workbench, workbench_pages


def _report(day="2026-09-10", generated="2026-09-10T16:10:00+08:00"):
    return {
        "effective_as_of": day,
        "generated_at": generated,
        "data_status": {"status": "complete"},
    }


@pytest.mark.parametrize(
    "report,now,eligible",
    [
        (None, "2026-09-10T17:00:00+08:00", False),
        (_report(), "2026-09-10T17:00:00+08:00", True),
        (_report(), "2026-09-11T00:00:00+08:00", False),
        (_report(generated="2026-09-10T15:59:59+08:00"), "2026-09-10T17:00:00+08:00", False),
        (_report(generated="invalid"), "2026-09-10T17:00:00+08:00", False),
    ],
)
def test_registration_button_uses_real_temporal_admission(report, now, eligible):
    assert workbench.shadow_admission(report, datetime.fromisoformat(now))["eligible"] is eligible


def test_registration_rechecks_readiness_before_any_write(monkeypatch):
    monkeypatch.setattr(workbench, "load_validated_latest_snapshot", lambda: None)
    monkeypatch.setattr(
        workbench, "generate_forward_shadow", lambda **kw: pytest.fail("must not write")
    )
    with pytest.raises(ValueError, match="日报"):
        workbench.register_current_shadow()


def test_corrupt_evidence_is_unknown_in_workbench_not_zero(monkeypatch):
    monkeypatch.setattr(workbench, "inspect_data_status", lambda: {"status": "blocked_no_calendar"})
    monkeypatch.setattr(workbench, "load_validated_latest_snapshot", lambda: None)
    monkeypatch.setattr(workbench, "list_accounts", lambda: [])

    def corrupt(*args):
        raise ValueError("fingerprint mismatch")

    monkeypatch.setattr(workbench, "summarize_forward_shadow", corrupt)
    result = workbench.read_workbench_status()
    assert result["shadow"] is None
    assert "fingerprint mismatch" in result["errors"]["shadow"]
    assert result["accounts"] == []


def test_template_contains_no_pretend_account_or_fill_rows():
    text = workbench.csv_template(FILL_COLUMNS).decode("utf-8-sig")
    assert text.splitlines() == [",".join(FILL_COLUMNS)]


def test_data_error_redacts_configured_credential(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-secret-for-redaction-test")
    result = workbench.public_error(ValueError("failed: synthetic-secret-for-redaction-test"))
    assert "synthetic-secret" not in result
    assert "隐藏" in result


@pytest.mark.parametrize("changed", ["account", "source", "state"])
def test_changed_account_source_or_state_invalidates_import_preview(changed):
    preview = {"account_id": "mine"}
    account = {"account_id": "mine", "account_fingerprint": "first-state"}
    source = "file-a"
    assert workbench.preview_matches(preview, source, account, "file-a", "first-state")
    if changed == "account":
        account["account_id"] = "another"
    elif changed == "source":
        source = "file-b"
    else:
        account["account_fingerprint"] = "later-state"
    assert not workbench.preview_matches(preview, source, account, "file-a", "first-state")


def _shadow_screen():
    from quantlab.ui.workbench_pages import render_shadow

    render_shadow()


def test_shadow_ui_action_is_explicit_and_reports_reuse(monkeypatch):
    calls = []
    monkeypatch.setattr(
        workbench_pages,
        "read_workbench_status",
        lambda: {
            "errors": {},
            "shadow_admission": {"eligible": True, "reason": "ready"},
            "shadow": [],
        },
    )
    monkeypatch.setattr(
        workbench_pages,
        "register_current_shadow",
        lambda: calls.append("register") or [SimpleNamespace(reused=True)],
    )
    app = AppTest.from_function(_shadow_screen).run()
    assert calls == []
    app.button[0].click().run()
    assert calls == ["register"]
    assert not app.exception
    assert "复用首次记录" in app.success[0].value


def _data_screen():
    from quantlab.ui.workbench_pages import render_data_update

    render_data_update()


def test_data_update_requires_checkbox_and_page_load_has_no_provider_call(monkeypatch):
    import quantlab.data

    monkeypatch.setattr(
        quantlab.data, "TushareProvider", lambda: pytest.fail("unexpected provider")
    )
    app = AppTest.from_function(_data_screen).run()
    assert not app.exception
    assert app.button[0].disabled


def _cash_screen():
    from quantlab.ui.workbench_pages import render_cash_and_valuation

    render_cash_and_valuation()


def test_cash_flow_ui_preview_then_commit_uses_real_temporary_journal(tmp_path, monkeypatch):
    import streamlit as st

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "personal"))
    from test_cash_flow_tracking import _flow, _seed

    import quantlab.personal as personal

    account_root, storage = _seed(tmp_path)
    monkeypatch.setattr(workbench_pages, "list_accounts", lambda: ["mine"])
    for name in ("load_effective_account", "load_tracking_summary"):
        original = getattr(personal, name)
        monkeypatch.setattr(
            workbench_pages,
            name,
            lambda account_id, fn=original: fn(
                account_id, account_root=account_root, storage=storage
            ),
        )
    for name in ("preview_manual_cash_flows", "import_manual_cash_flows"):
        original = getattr(personal, name)
        monkeypatch.setattr(
            workbench_pages,
            name,
            lambda account_id, raw, fn=original: fn(
                account_id, raw, account_root=account_root, storage=storage
            ),
        )
    monkeypatch.setattr(workbench_pages, "load_latest_valuation_checkpoint", lambda *args: None)
    raw = _flow("D1", "2026-09-07T10:00:00+08:00", "DEPOSIT", "500.00")
    monkeypatch.setattr(
        st, "file_uploader", lambda *args, **kwargs: SimpleNamespace(getvalue=lambda: raw)
    )
    app = AppTest.from_function(_cash_screen).run()
    assert not app.exception
    next(button for button in app.button if button.label == "预览并校验出入金").click().run()
    assert not app.exception
    assert not list(account_root.glob("mine/tracking/*/journal.json"))
    next(button for button in app.button if button.label == "确认导入已预览出入金").click().run()
    assert not app.exception
    assert len(list(account_root.glob("mine/tracking/*/journal.json"))) == 1
    result = personal.load_effective_account("mine", account_root=account_root, storage=storage)
    assert result["cash_fen"] == 250000


def test_fill_ui_preview_then_commit_uses_real_temporary_journal(tmp_path, monkeypatch):
    import streamlit as st

    import quantlab.personal as personal
    from quantlab.daily.service import PROJECT_ROOT

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "personal"))
    from test_tracking import _fills, _seed

    account_root, storage = _seed(tmp_path)
    monkeypatch.setattr(personal, "list_accounts", lambda: ["mine"])
    for name in ("load_effective_account", "load_tracking_summary"):
        original = getattr(personal, name)
        monkeypatch.setattr(
            personal,
            name,
            lambda account_id, fn=original: fn(
                account_id, account_root=account_root, storage=storage
            ),
        )
    for name in ("preview_manual_fills", "import_manual_fills"):
        original = getattr(personal, name)
        monkeypatch.setattr(
            personal,
            name,
            lambda account_id, raw, fn=original: fn(
                account_id, raw, account_root=account_root, storage=storage
            ),
        )
    monkeypatch.setattr(personal, "load_latest_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(personal, "build_plan_fill_comparison", lambda *args: None)
    monkeypatch.setattr(
        personal, "build_tracking_valuation", lambda *args: {"status": "synthetic_unpriced"}
    )
    raw = _fills()
    monkeypatch.setattr(
        st,
        "file_uploader",
        lambda *args, **kwargs: (
            SimpleNamespace(getvalue=lambda: raw)
            if kwargs.get("key") == "manual_fill_upload"
            else None
        ),
    )
    app = AppTest.from_file(PROJECT_ROOT / "src/quantlab/ui/app.py", default_timeout=30).run()
    app.sidebar.radio[0].set_value("账户与参考计划").run()
    assert not app.exception
    next(button for button in app.button if button.label == "预览并校验成交").click().run()
    assert not app.exception
    assert not list(account_root.glob("mine/tracking/*/journal.json"))
    next(button for button in app.button if button.label == "确认导入已预览成交").click().run()
    assert not app.exception
    assert len(list(account_root.glob("mine/tracking/*/journal.json"))) == 1
    assert personal.load_effective_account("mine")["cash_fen"] == 219000


def _history_screen():
    from quantlab.ui.workbench_pages import render_historical_account

    render_historical_account("mine", {"account_fingerprint": "synthetic-ui-basis"})


def test_historical_ui_reads_real_temporary_ledger_without_writing(tmp_path, monkeypatch):
    from datetime import date, time

    from quantlab.personal import import_manual_fills, replay_account_at

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "personal"))
    from test_cash_flow_tracking import _seed
    from test_manual_fill_time import _fill

    root, storage = _seed(tmp_path)
    import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    journal = next(root.glob("mine/tracking/*/journal.json"))
    before = journal.read_bytes()
    monkeypatch.setattr(
        workbench_pages,
        "replay_account_at",
        lambda account_id, as_of, **kw: replay_account_at(
            account_id, as_of, account_root=root, storage=storage, **kw
        ),
    )
    app = AppTest.from_function(_history_screen).run()
    app.date_input[0].set_value(date(2026, 9, 8))
    app.time_input[0].set_value(time(10, 0))
    next(button for button in app.button if button.label == "回放指定历史时点").click().run()
    assert not app.exception
    assert app.metric[0].value == "¥995.00"
    assert "1 笔是在截止时间之后补录" in app.caption[0].value
    assert journal.read_bytes() == before


def _performance_screen():
    from quantlab.ui.workbench_pages import render_performance_inputs

    render_performance_inputs("mine", {"account_fingerprint": "synthetic-ui-basis"})


def test_performance_ui_explains_real_temporary_evidence_gaps(tmp_path, monkeypatch):
    from quantlab.personal import inspect_performance_inputs
    from quantlab.personal import performance_inputs as module

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "personal"))
    from test_performance_inputs import FixedDatetime, _valued

    root, storage, checkpoint, _ = _valued(tmp_path)
    before = checkpoint.read_bytes()
    monkeypatch.setattr(module, "datetime", FixedDatetime)
    monkeypatch.setattr(
        workbench_pages,
        "inspect_performance_inputs",
        lambda account_id: inspect_performance_inputs(
            account_id, account_root=root, storage=storage
        ),
    )
    app = AppTest.from_function(_performance_screen).run()
    next(button for button in app.button if button.label == "检查收益计算条件").click().run()
    assert not app.exception
    assert "不能生成可信的账户收益率" in app.warning[0].value
    assert "分红、送转" in app.dataframe[0].value.to_string()
    assert checkpoint.read_bytes() == before
