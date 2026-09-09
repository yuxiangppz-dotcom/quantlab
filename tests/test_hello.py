import quantlab
from quantlab.__main__ import _parser


def test_version() -> None:
    assert quantlab.__version__ == "0.1.0"


def test_product_cli_exposes_real_commands() -> None:
    parser = _parser()
    for command in ("doctor", "update", "daily", "research", "ui"):
        assert parser.parse_args([command]).command == command
