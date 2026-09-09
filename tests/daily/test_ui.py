from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_all_four_local_ui_pages_render_without_exception() -> None:
    app_path = Path(__file__).resolve().parents[2] / "src" / "quantlab" / "ui" / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "QuantLab Daily v1"

    for page in ("股票排名与因子", "回测与基准", "账户与参考计划"):
        app.sidebar.radio[0].set_value(page)
        app.run(timeout=30)
        assert not app.exception
