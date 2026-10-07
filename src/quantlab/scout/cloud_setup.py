"""Seal Git-only application/engine archives inside an immutable container image."""

import argparse
import hashlib
import json
from importlib.metadata import distributions
from pathlib import Path

from quantlab.scout.daily_runtime import read
from quantlab.scout.models import fingerprint

ENGINE_COMMIT = "9c1f91422fe6b22b06a8c28f064168d360b82176"


def seal(app, engine, bundle, data_root):
    bundle = read(bundle)
    if bundle["engine_commit"] != ENGINE_COMMIT:
        raise ValueError("Only the explicitly accepted Scout engine may be deployed")
    for label, root in (("app", app), ("engine", engine)):
        for relative, expected in bundle["files"][label].items():
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
                raise ValueError("Bundle source integrity failed")
    if (app / "config/scout_daily.fixed.json").read_bytes() != (
        engine / "config/scout_daily.fixed.json"
    ).read_bytes():
        raise ValueError("Cloud delivery must not change prediction configuration")
    dependencies = {d.metadata["Name"]: d.version for d in distributions()}

    def manifest(root, settings, commit):
        (root / "daily-settings.json").write_text(json.dumps(settings, indent=2))
        files = dict(bundle["files"]["engine" if root == engine else "app"])
        files["daily-settings.json"] = hashlib.sha256(
            (root / "daily-settings.json").read_bytes()
        ).hexdigest()
        value = {
            "commit": commit,
            "files": files,
            "settings_sha256": fingerprint(settings),
            "config_sha256": files["config/scout_daily.fixed.json"],
            "dependencies": dependencies,
        }
        (root / "daily-manifest.json").write_text(json.dumps(value, indent=2))

    engine_settings = {
        "release_root": str(engine),
        "canonical_dir": str(data_root / "market"),
        "output_root": str(data_root / "runs"),
        "state_root": str(data_root / "runtime"),
    }
    manifest(engine, engine_settings, bundle["engine_commit"])
    app_settings = engine_settings | {
        "release_root": str(app),
        "prediction_release_root": str(engine),
        "prediction_manifest_sha256": hashlib.sha256(
            (engine / "daily-manifest.json").read_bytes()
        ).hexdigest(),
    }
    manifest(app, app_settings, bundle["app_commit"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("/app"))
    parser.add_argument("--engine", type=Path, default=Path("/engine"))
    parser.add_argument("--bundle", type=Path, default=Path("/bundle.json"))
    parser.add_argument("--data-root", type=Path, default=Path("/data/scout"))
    args = parser.parse_args()
    seal(args.app, args.engine, args.bundle, args.data_root)


if __name__ == "__main__":
    main()
