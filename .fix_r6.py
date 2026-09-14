import pathlib

p = pathlib.Path("scripts/s4_admission_independent_verify.py")
s = p.read_text()

# ---- A: frozen gap identities + exact required set + report pair match ----
old = '''    required_sources = {
        "sealed:s4_first_entry_plan/plan.json",
        "sealed:s4_entry_raw_precision/reconciliation.json",
        "sealed:cohort_dividend_readiness/profiles.json",
    }
    consumed_partition_keys: set[str] = set()
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        stamp = trade_date.replace("-", "")
        year, month, _ = trade_date.split("-")
        base = f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        required_sources.add(f"sealed:{base}/intent.json")
        required_sources.add(f"sealed:{base}/result.json")
        required_sources.add(f"sealed:{base}/response.body")
        for directory in (
            f"daily/year={year}/month={month}",
            f"lifecycle_context_v1/suspensions/year={year}/month={month}",
        ):
            parts = sorted((canonical_dir / directory).glob("*.parquet"))
            for part in parts:
                relative = part.resolve().relative_to(canonical_dir.resolve()).as_posix()
                consumed_partition_keys.add(f"canonical:{relative}")
    required_sources |= consumed_partition_keys
    missing_required = sorted(required_sources - set(manifest_files))
    extra_entries = sorted(
        key
        for key in manifest_files
        if key.startswith("canonical:") and key not in required_sources
    )
    if missing_required or extra_entries:
        check(
            "manifest_covers_exactly_the_consumed_set",
            False,
            "; ".join(
                [f"missing {k}" for k in missing_required[:3]]
                + [f"extra {k}" for k in extra_entries[:3]]
            ),
        )'''
new = '''    required_sources = {
        "sealed:s4_first_entry_plan/plan.json",
        "sealed:s4_entry_raw_precision/reconciliation.json",
        "sealed:cohort_dividend_readiness/profiles.json",
    }
    # A. The six gap identities are frozen in this verifier, independent of
    # the report. Duplicates, omissions, substitutions and extra dates in
    # the report are all rejected before anything else is derived.
    report_pairs = [
        (gap.get("instrument_id"), gap.get("trade_date"))
        for gap in report.get("prior20_gaps", [])
    ]
    frozen_pairs = list(FROZEN_PRIOR20_GAPS)
    pairs_match = (
        len(report_pairs) == len(frozen_pairs)
        and set(report_pairs) == set(frozen_pairs)
        and len(set(report_pairs)) == len(frozen_pairs)
    )
    check(
        "prior20_gap_identities_match_frozen_contract",
        pairs_match,
        f"report pairs {sorted(report_pairs)}" if not pairs_match else "6/6 frozen identities",
    )
    consumed_partition_keys: set[str] = set()
    for code, trade_date in FROZEN_PRIOR20_GAPS:
        stamp = trade_date.replace("-", "")
        year, month, _ = trade_date.split("-")
        base = f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        required_sources.add(f"sealed:{base}/intent.json")
        required_sources.add(f"sealed:{base}/result.json")
        required_sources.add(f"sealed:{base}/response.body")
        for directory in (
            f"daily/year={year}/month={month}",
            f"lifecycle_context_v1/suspensions/year={year}/month={month}",
        ):
            parts = sorted((canonical_dir / directory).glob("*.parquet"))
            for part in parts:
                relative = part.resolve().relative_to(canonical_dir.resolve()).as_posix()
                consumed_partition_keys.add(f"canonical:{relative}")
    required_sources |= consumed_partition_keys
    missing_required = sorted(required_sources - set(manifest_files))
    extra_entries = sorted(
        key
        for key in manifest_files
        if key.startswith("canonical:") and key not in required_sources
    )
    if missing_required or extra_entries:
        check(
            "manifest_covers_exactly_the_consumed_set",
            False,
            "; ".join(
                [f"missing {k}" for k in missing_required[:3]]
                + [f"extra {k}" for k in extra_entries[:3]]
            ),
        )'''
assert old in s, "required-source block not found"
s = s.replace(old, new)

# ---- constant: frozen gap identity set ----
old_frozen = '''FROZEN = {'''
new_frozen = '''# The six empty prior20 code-date pairs, frozen independently of any report.
FROZEN_PRIOR20_GAPS = (
    ("000301.SZ", "2021-12-22"),
    ("000777.SZ", "2021-12-07"),
    ("000777.SZ", "2021-12-08"),
    ("000777.SZ", "2021-12-09"),
    ("000777.SZ", "2021-12-10"),
    ("000777.SZ", "2021-12-13"),
)

FROZEN = {'''
assert old_frozen in s
s = s.replace(old_frozen, new_frozen, 1)

# ---- B: profiles fingerprint + fingerprint_checks completeness + bytes ----
old_fp = '''    check(
        "sealed_fingerprints_recomputed",
        canonical_fingerprint(plan) == plan["fingerprint"] == frozen["plan"]
        and canonical_fingerprint(recon) == recon["fingerprint"] == frozen["reconciliation"],
        "recomputed over the file bodies and equal to the frozen identities",
    )'''
new_fp = '''    profiles_path = source_dir / "cohort_dividend_readiness/profiles.json"
    profiles = json.loads(profiles_path.read_text())
    check(
        "sealed_fingerprints_recomputed",
        canonical_fingerprint(plan) == plan["fingerprint"] == frozen["plan"]
        and canonical_fingerprint(recon) == recon["fingerprint"] == frozen["reconciliation"]
        and canonical_fingerprint(profiles)
        == profiles["fingerprint"]
        == frozen["profiles"],
        "recomputed over the file bodies and equal to the frozen identities",
    )'''
assert old_fp in s
s = s.replace(old_fp, new_fp)

# ---- bytes + fingerprint_checks completeness in the manifest loop ----
old_loop = '''    for key, entry in manifest_files.items():
        if ":" not in key:
            manifest_ok = False
            manifest_notes.append(f"{key}: key is not root:relative")
            continue
        root_label, relative = key.split(":", 1)
        declared_root = declared_roots.get(root_label)
        if declared_root is None:
            manifest_ok = False
            manifest_notes.append(f"{key}: unknown root label")
            continue
        declared_path = pathlib.Path(entry.get("path", ""))
        expected_path = pathlib.Path(declared_root) / relative
        # The key must name exactly this file under exactly this root: a
        # relabelled or redirected path is rejected even if the hash matches.
        if declared_path != expected_path:
            manifest_ok = False
            manifest_notes.append(
                f"{key}: path {entry.get('path')!r} is not {str(expected_path)!r}"
            )
            continue
        if not path_is_under(declared_path, pathlib.Path(declared_root)):
            manifest_ok = False
            manifest_notes.append(f"{key}: path escapes its declared root")
            continue
        if not declared_path.is_file() or sha_bytes(
            declared_path.read_bytes()
        ) != entry.get("sha256"):
            manifest_ok = False
            manifest_notes.append(f"{key}: missing or changed")'''
new_loop = '''    for key, entry in manifest_files.items():
        if ":" not in key:
            manifest_ok = False
            manifest_notes.append(f"{key}: key is not root:relative")
            continue
        root_label, relative = key.split(":", 1)
        declared_root = declared_roots.get(root_label)
        if declared_root is None:
            manifest_ok = False
            manifest_notes.append(f"{key}: unknown root label")
            continue
        declared_path = pathlib.Path(entry.get("path", ""))
        expected_path = pathlib.Path(declared_root) / relative
        # The key must name exactly this file under exactly this root: a
        # relabelled or redirected path is rejected even if the hash matches.
        if declared_path != expected_path:
            manifest_ok = False
            manifest_notes.append(
                f"{key}: path {entry.get('path')!r} is not {str(expected_path)!r}"
            )
            continue
        if not path_is_under(declared_path, pathlib.Path(declared_root)):
            manifest_ok = False
            manifest_notes.append(f"{key}: path escapes its declared root")
            continue
        actual_size = declared_path.stat().st_size if declared_path.is_file() else None
        if actual_size is None:
            manifest_ok = False
            manifest_notes.append(f"{key}: missing")
            continue
        if entry.get("bytes") is not None and entry.get("bytes") != actual_size:
            manifest_ok = False
            manifest_notes.append(f"{key}: byte count {entry.get('bytes')} != {actual_size}")
        if sha_bytes(declared_path.read_bytes()) != entry.get("sha256"):
            manifest_ok = False
            manifest_notes.append(f"{key}: missing or changed")'''
assert old_loop in s
s = s.replace(old_loop, new_loop)

# ---- fingerprint_checks completeness check (after version check) ----
old_checks_anchor = '''    manifest_files = manifest.get("files", {})
    roots = manifest.get("roots", {})'''
new_checks = '''    # fingerprint_checks: exactly the three base keys, each recording the
    # recomputed embedded fingerprint equal to the frozen identity.
    fingerprint_checks = manifest.get("fingerprint_checks", {})
    expected_check_keys = {
        f"sealed:{relative}"
        for relative in (
            "s4_first_entry_plan/plan.json",
            "s4_entry_raw_precision/reconciliation.json",
            "cohort_dividend_readiness/profiles.json",
        )
    }
    checks_failures = []
    if set(fingerprint_checks) != expected_check_keys:
        checks_failures.append(
            f"keys {sorted(fingerprint_checks)} != {sorted(expected_check_keys)}"
        )
    recomputed_sources = {
        "sealed:s4_first_entry_plan/plan.json": plan,
        "sealed:s4_entry_raw_precision/reconciliation.json": recon,
        "sealed:cohort_dividend_readiness/profiles.json": profiles,
    }
    for key in sorted(expected_check_keys):
        entry = fingerprint_checks.get(key)
        recomputed = canonical_fingerprint(recomputed_sources[key])
        if entry is None:
            checks_failures.append(f"{key}: fingerprint check missing")
            continue
        if entry.get("embedded_fingerprint") != recomputed:
            checks_failures.append(f"{key}: recorded fingerprint != recomputed")
        short_name = key.rsplit("/", 1)[-1].split(".")[0]
        identity_key = {"plan": "plan", "reconciliation": "reconciliation", "profiles": "profiles"}[short_name]
        if entry.get("frozen_identity") != frozen.get(identity_key):
            checks_failures.append(f"{key}: frozen identity mismatch")
        if entry.get("match") is not True:
            checks_failures.append(f"{key}: match flag is not true")
    check(
        "fingerprint_checks_complete_and_factual",
        not checks_failures,
        "; ".join(checks_failures[:4]) or "three base provenance records verified",
    )
    manifest_files = manifest.get("files", {})
    roots = manifest.get("roots", {})'''
assert old_checks_anchor in s
s = s.replace(old_checks_anchor, new_checks)

# ---- bound_files exact membership ----
old_bound = '''    bound_files = manifest.get("bound_files", {})
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
            bound_failures.append(f"{key}: bound entry contradicts files")'''
new_bound = '''    bound_files = manifest.get("bound_files", {})
    bound_failures = []
    # Exact membership per the producer binding contract: the attempt
    # triples for all six frozen gap dates plus every consumed partition
    # file. Base artifacts are bound through the main binding, not here.
    expected_bound_keys = sorted(
        key
        for key in required_sources
        if key.startswith("sealed:s4_entry_raw_precision/attempts/")
        or key.startswith("canonical:")
    )
    if sorted(bound_files) != expected_bound_keys:
        missing = sorted(set(expected_bound_keys) - set(bound_files))
        extra = sorted(set(bound_files) - set(expected_bound_keys))
        bound_failures.append(
            f"membership mismatch: missing {missing[:3]} extra {extra[:3]}"
        )
    for key, entry in bound_files.items():
        file_entry = manifest_files.get(key)
        if file_entry is None:
            bound_failures.append(f"{key}: bound file is not in files")
        elif file_entry.get("sha256") != entry.get("sha256") or file_entry.get(
            "bytes"
        ) != entry.get("bytes"):
            bound_failures.append(f"{key}: bound entry contradicts files")'''
assert old_bound in s
s = s.replace(old_bound, new_bound)

p.write_text(s)
print("verifier frozen-identity and fact checks applied")
