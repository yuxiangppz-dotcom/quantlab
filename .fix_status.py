import pathlib

# ---- 1. evidence_catalog: classify legacy summaries in non-strict mode ----
p = pathlib.Path("src/quantlab/research/evidence_catalog.py")
s = p.read_text()

old_issue = '''@dataclass(frozen=True)
class EvidenceCatalogIssue:
    relative_path: str
    message: str'''
new_issue = '''@dataclass(frozen=True)
class EvidenceCatalogIssue:
    relative_path: str
    message: str
    classification: str = "malformed_evidence"'''
assert old_issue in s
s = s.replace(old_issue, new_issue, 1)

old_scan = '''    for path in sorted(root.rglob("summary.json")):
        relative = path.relative_to(root).as_posix()
        try:
            entry = _parse_entry(root, path)
        except (OSError, DataValidationError) as exc:
            if strict:
                if isinstance(exc, DataValidationError):
                    raise
                raise DataValidationError(
                    f"{relative}: cannot read evidence artifact"
                ) from exc
            message = str(exc)'''
new_scan = '''    for path in sorted(root.rglob("summary.json")):
        relative = path.relative_to(root).as_posix()
        try:
            entry = _parse_entry(root, path)
        except (OSError, DataValidationError) as exc:
            if strict:
                if isinstance(exc, DataValidationError):
                    raise
                raise DataValidationError(
                    f"{relative}: cannot read evidence artifact"
                ) from exc
            message = str(exc)
            classification = "malformed_evidence"
            try:
                payload = json.loads(path.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                legacy_schema = payload.get("experiment_schema")
                if isinstance(legacy_schema, str) and legacy_schema.strip():
                    classification = "legacy_experiment_summary"
                    message = (
                        f"{message}; legacy experiment summary "
                        f"(experiment_schema={legacy_schema.strip()!r}) is not "
                        "an evidence-catalog artifact and gains no eligibility"
                    )'''
assert old_scan in s, "scan block not found"
s = s.replace(old_scan, new_scan, 1)

old_issue_append = '''            issues.append(
                EvidenceCatalogIssue(relative_path=relative, message=message)
            )'''
new_issue_append = '''            issues.append(
                EvidenceCatalogIssue(
                    relative_path=relative,
                    message=message,
                    classification=classification,
                )
            )'''
assert old_issue_append in s, "issue append not found"
s = s.replace(old_issue_append, new_issue_append, 1)
p.write_text(s)
print("evidence_catalog classification applied")

# ---- 2. research_status: non-strict composition + incompatible section ----
r = pathlib.Path("src/quantlab/research/research_status.py")
t = r.read_text()

old_call = '''    catalog = build_evidence_catalog(experiment_root, strict=True)'''
new_call = '''    # Legacy experiment summaries and malformed artifacts are classified and
    # reported instead of crashing the whole status view; the strict entry
    # remains available to consumers that must hard-fail.
    catalog = build_evidence_catalog(experiment_root, strict=False)'''
assert old_call in t
t = t.replace(old_call, new_call, 1)

old_doc = '''    """Compose claim-neutral readiness from existing immutable local evidence.

    Missing optional evidence roots are reported as absent and yield zero
    evidence. Existing malformed artifacts propagate an error and are never
    silently skipped.
    """'''
new_doc = '''    """Compose claim-neutral readiness from existing immutable local evidence.

    Missing optional evidence roots are reported as absent and yield zero
    evidence. Legacy experiment summaries and malformed artifacts are
    classified into ``incompatible_artifacts`` with explicit reasons; they
    never gain evidence eligibility and corrupt current-format artifacts
    keep the overall status from reading as clean.
    """'''
assert old_doc in t
t = t.replace(old_doc, new_doc, 1)

old_payload = '''        "evidence_catalog": {
            "summary": summarize_evidence_catalog(catalog),
            "entries": [asdict(item) for item in catalog.entries],
            "issues": [asdict(item) for item in catalog.issues],
        },'''
new_payload = '''        "evidence_catalog": {
            "summary": summarize_evidence_catalog(catalog),
            "entries": [asdict(item) for item in catalog.entries],
            "issues": [asdict(item) for item in catalog.issues],
        },
        "incompatible_artifacts": [
            {
                "relative_path": issue.relative_path,
                "classification": issue.classification,
                "message": issue.message,
            }
            for issue in catalog.issues
        ],'''
assert old_payload in t
t = t.replace(old_payload, new_payload, 1)

old_core_end = '''        "performance_claim": False,
        "strategy_promotion_authority": False,
        "broker_order_authority": False,
        "claim": "read_only_structural_research_status_not_performance_or_execution_authority",
    }
    return {**core, "status_fingerprint": _canonical_hash(core)}'''
new_core_end = '''        "performance_claim": False,
        "strategy_promotion_authority": False,
        "broker_order_authority": False,
        "claim": "read_only_structural_research_status_not_performance_or_execution_authority",
    }
    has_corrupt = any(
        issue.classification == "malformed_evidence" for issue in catalog.issues
    )
    has_legacy = any(
        issue.classification == "legacy_experiment_summary" for issue in catalog.issues
    )
    if has_corrupt:
        core["overall_status"] = "ready_with_corrupt_evidence"
    elif has_legacy:
        core["overall_status"] = "ready_with_legacy_artifacts"
    else:
        core["overall_status"] = "clean"
    return {**core, "status_fingerprint": _canonical_hash(core)}'''
assert old_core_end in t
t = t.replace(old_core_end, new_core_end, 1)

old_format = '''    sources = payload["sources"]
    catalog = payload["evidence_catalog"]["summary"]
    strategies = payload["strategy_readiness"]["strategies"]
    lines = [
        "QuantLab research status",
        f"  evidence root: {'present' if sources['experiment_root_exists'] else 'missing'}",
        f"  evidence artifacts: {catalog['entry_count']}",'''
new_format = '''    sources = payload["sources"]
    catalog = payload["evidence_catalog"]["summary"]
    strategies = payload["strategy_readiness"]["strategies"]
    incompatible = payload.get("incompatible_artifacts", [])
    lines = [
        "QuantLab research status",
        f"  overall status: {payload.get('overall_status', 'clean')}",
        f"  evidence root: {'present' if sources['experiment_root_exists'] else 'missing'}",
        f"  evidence artifacts: {catalog['entry_count']}",'''
assert old_format in t
t = t.replace(old_format, new_format, 1)

old_tail = '''    lines.append("  performance claim: false")
    return "\\n".join(lines)'''
new_tail = '''    if incompatible:
        lines.append("  incompatible artifacts:")
        for artifact in incompatible:
            lines.append(
                f"    {artifact['relative_path']}: "
                f"{artifact['classification']} - {artifact['message']}"
            )
    lines.append("  performance claim: false")
    return "\\n".join(lines)'''
assert old_tail in t
t = t.replace(old_tail, new_tail, 1)
r.write_text(t)
print("research_status composition updated")

# ---- 3. __main__: exit-code convention ----
m = pathlib.Path("src/quantlab/__main__.py")
u = m.read_text()
old_main = '''    payload = build_research_status(
        registry_path=PROJECT_ROOT / "config" / "strategy_registry_v1.json",
        forward_config_path=PROJECT_ROOT / "config" / "forward_shadow_v1.json",
        experiment_root=PROJECT_ROOT / "data" / "experiments",
        shadow_root=PROJECT_ROOT / "data" / "predictions" / "forward_shadow",
    )
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_research_status(payload))
    return 0'''
new_main = '''    try:
        payload = build_research_status(
            registry_path=PROJECT_ROOT / "config" / "strategy_registry_v1.json",
            forward_config_path=PROJECT_ROOT / "config" / "forward_shadow_v1.json",
            experiment_root=PROJECT_ROOT / "data" / "experiments",
            shadow_root=PROJECT_ROOT / "data" / "predictions" / "forward_shadow",
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports, never hides
        print(f"research status could not be composed: {exc}")
        return 2
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_research_status(payload))
    if payload.get("overall_status") == "ready_with_corrupt_evidence":
        return 1
    return 0'''
assert old_main in u
u = u.replace(old_main, new_main, 1)
m.write_text(u)
print("__main__ exit-code convention applied")
