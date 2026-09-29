"""Render a saved Scout report.json to a separate, read-only HTML view."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quantlab.scout.html_report import render_html_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_json", type=Path)
    parser.add_argument("output_html", type=Path)
    args = parser.parse_args()
    if args.report_json.resolve() == args.output_html.resolve():
        parser.error("output must differ from input")
    report = json.loads(args.report_json.read_text(encoding="utf-8"))
    html = render_html_report(report)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output_html.open("x", encoding="utf-8") as output:
            output.write(html)
    except FileExistsError:
        parser.error("output already exists; choose a new path")
    print(args.output_html.resolve())


if __name__ == "__main__":
    main()
