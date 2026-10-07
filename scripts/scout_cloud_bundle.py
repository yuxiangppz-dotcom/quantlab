"""Create a credential/data-free fixed cloud deployment bundle from clean Git commits."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from quantlab.scout.cloud_setup import ENGINE_COMMIT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    git = ["git"] + ([f"--git-dir={args.git_dir}"] if args.git_dir else [])
    subprocess.run(git + ["diff", "--exit-code", "HEAD"], check=True)
    commit = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    args.output.mkdir(parents=True, exist_ok=False)
    files = {}
    with tempfile.TemporaryDirectory() as temporary:
        for label, ref in (("app", commit), ("engine", ENGINE_COMMIT)):
            destination = args.output / label
            destination.mkdir()
            archive = Path(temporary) / (label + ".tar")
            subprocess.run(git + ["archive", ref, "-o", str(archive)], check=True)
            subprocess.run(["tar", "-xf", str(archive), "-C", str(destination)], check=True)
            files[label] = {
                str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in destination.rglob("*")
                if p.is_file()
            }
    for path in (args.output / "app/deploy/scout_cloud").iterdir():
        if path.is_file():
            shutil.copyfile(path, args.output / path.name)
    (args.output / "bundle.json").write_text(
        json.dumps(
            {"app_commit": commit, "engine_commit": ENGINE_COMMIT, "files": files}, indent=2
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "bundle": str(args.output),
                "app_commit": commit,
                "engine_commit": ENGINE_COMMIT,
                "contains_credentials_or_data": False,
            }
        )
    )


if __name__ == "__main__":
    main()
