"""Create a clearly synthetic opportunity example without network or paid calls."""

import argparse
import json
from pathlib import Path

from quantlab.scout.opportunity_demo import make_opportunity_example

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--output-dir", type=Path, required=True, help="New directory; refuses overwrite"
)
args = parser.parse_args()
print(json.dumps(make_opportunity_example(args.output_dir), ensure_ascii=False, indent=2))
