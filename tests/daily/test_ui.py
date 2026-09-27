from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_all_seven_local_ui_pages_render_without_exception() -> None:
    app_path = Path(__file__).resolve().parents[2] / "src" / "quantlab" / "ui" / "app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "QuantLab 个人量化工作台"

    for page in (
        "数据状态与日报",
        "股票排名与因子",
        "前瞻观察",
        "回测与基准",
        "账户与参考计划",
        "资金流水与估值",
    ):
        if page in {"账户与参考计划", "资金流水与估值"}:
            app.sidebar.radio[0].set_value(page)
        else:
            app.sidebar.radio[0].set_value("历史研究").run(timeout=30)
            app.sidebar.selectbox[0].set_value(page)
        app.run(timeout=30)
        assert not app.exception
