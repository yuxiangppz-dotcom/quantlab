import quantlab
from quantlab.__main__ import main


def test_version() -> None:
    assert quantlab.__version__ == "0.1.0"


def test_main_prints_hello(capsys) -> None:
    main()
    assert "Hello from quantlab!" in capsys.readouterr().out
