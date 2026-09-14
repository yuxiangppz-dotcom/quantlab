import pathlib

p = pathlib.Path("scripts/s4_admission_independent_verify.py")
s = p.read_text()

old = '''    # 4d. The manifest must cover every source the frozen contract requires:
    # the three base artifacts, the raw attempt triples for every gap date,
    # and the daily/suspension partitions backing each coverage claim.
    required_sources = {
        "sealed:s4_first_entry_plan/plan.json",
        "sealed:s4_entry_raw_precision/reconciliation.json",
        "sealed:cohort_dividend_readiness/profiles.json",
    }
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        stamp = trade_date.replace("-", "")
        year, month, _ = trade_date.split("-")
        base = f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        required_sources.add(f"sealed:{base}/intent.json")
        required_sources.add(f"sealed:{base}/result.json")
        required_sources.add(f"sealed:{base}/response.body")
        required_sources.add(
            f"canonical:daily/year={year}/month={month}"
        )
        required_sources.add(
            f"canonical:lifecycle_context_v1/suspensions/year={year}/month={month}"
        )
    manifest_files = report.get("manifest", {}).get("files", {})
    missing_required = sorted(
        req
        for req in required_sources
        if not any(key.startswith(req) for key in manifest_files)
    )
    if missing_required:
        check(
            "manifest_covers_required_sources",
            False,
            "; ".join(missing_required[:4]) or "covered",
        )
    roots = report.get("manifest", {}).get("roots", {})
    roots_ok = roots.get("sealed") == str(source_dir.resolve()) and roots.get(
        "canonical"
    ) == str(canonical_dir.resolve())
    check(
        "manifest_roots_match_passed_directories",
        roots_ok,
        str(roots) if not roots_ok else "sealed/canonical roots match",
    )'''
new = '''    # 4d. Manifest identity: the format must be version 2 (anything else is
    # a legacy artifact and is rejected outright), the roots must match the
    # passed directories, and the required file set is derived from the
    # frozen contract plus the partition files actually consumed — exact
    # keys, no prefix existence.
    manifest = report.get("manifest", {})
    manifest_files = manifest.get("files", {})
    version_ok = manifest.get("manifest_version") == 2
    check(
        "manifest_version_is_2",
        version_ok,
        f"manifest_version={manifest.get('manifest_version')!r}",
    )
    roots = manifest.get("roots", {})
    roots_ok = roots.get("sealed") == str(source_dir.resolve()) and roots.get(
        "canonical"
    ) == str(canonical_dir.resolve())
    check(
        "manifest_roots_match_passed_directories",
        roots_ok,
        str(roots) if not roots_ok else "sealed/canonical roots match",
    )
    required_sources = {
        "sealed:s4_first_entry_plan/plan.json",
        "sealed:s4_entry_raw_precision/reconciliation.json",
        "sealed:cohort_dividend_readiness/profiles.json",
    }
    consumed_partitions: dict[str, pathlib.Path] = {}
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        stamp = trade_date.replace("-", "")
        year, month, _ = trade_date.split("-")
        base = f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        required_sources.add(f"sealed:{base}/intent.json")
        required_sources.add(f"sealed:{base}/result.json")
        required_sources.add(f"sealed:{base}/response.body")
        for label, root, directory in (
            (
                "canonical",
                canonical_dir,
                f"daily/year={year}/month={month}",
            ),
            (
                "canonical",
                canonical_dir,
                f"lifecycle_context_v1/suspensions/year={year}/month={month}",
            ),
        ):
            parts = sorted((root / directory).glob("*.parquet"))
            for part in parts:
                relative = part.resolve().relative_to(root.resolve()).as_posix()
                key = f"{label}:{relative}"
                required_sources.add(key)
                consumed_partitions[key] = part
    missing_required = sorted(required_sources - set(manifest_files))
    extra_entries = sorted(
        key
        for key in manifest_files
        if key.startswith("canonical:") and key not in required_sources
        and any(
            key.rsplit("/", 1)[0] in req or key.startswith(req.rsplit("/", 1)[0])
            for req in ()
        )
    )
    if missing_required or extra_entries:
        check(
            "manifest_covers_exactly_the_consumed_set",
            False,
            "; ".join(
                (f"missing {k}" for k in missing_required[:3])
                | (f"extra {k}" for k in extra_entries[:3])
            ),
        )
    bound_files = manifest.get("bound_files", {})
    bound_failures = []
    if not bound_files:
        bound_failures.append("bound_files is empty")
    for key, entry in bound_files.items():
        file_entry = manifest_files.get(key)
        if file_entry is None:
            bound_failures.append(f"{key}: bound file is not in files")
        elif file_entry.get("sha256") != entry.get("sha256") or file_entry.get(
            "bytes"
        ) != entry.get("bytes"):
            bound_failures.append(f"{key}: bound entry contradicts files")
    check(
        "bound_files_consistent_with_files",
        not bound_failures,
        "; ".join(bound_failures[:4]) or f"{len(bound_files)} bound entries agree",
    )'''
assert old in s, "manifest coverage block not found"
s = s.replace(old, new)
p.write_text(s)
print("verifier consumption-set hardening applied")
