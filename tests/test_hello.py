import quantlab
from quantlab.__main__ import _parser


def test_version() -> None:
    assert quantlab.__version__ == "1.0.0"


def test_product_cli_exposes_real_commands() -> None:
    parser = _parser()
    for command in (
        "doctor",
        "update",
        "daily",
        "research",
        "research-status",
        "portfolio",
        "ui",
        "accept",
    ):
        arguments = [command]
        if command == "portfolio":
            arguments.append("demo")
        assert parser.parse_args(arguments).command == command
