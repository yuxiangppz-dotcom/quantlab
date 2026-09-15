import pathlib

# ================= evidence_catalog.py =================
p = pathlib.Path("src/quantlab/research/evidence_catalog.py")
s = p.read_text()

old_issue = """@dataclass(frozen=True)
class EvidenceCatalogIssue:
    relative_path: str
    message: str"""
new_issue = """@dataclass(frozen=True)
class EvidenceCatalogIssue:
    relative_path: str
    message: str
    classification: str = "malformed_evidence\""""
assert old_issue in s, "ec1"
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
                probe = json.loads(path.read_bytes().decode("utf-8"))
            except Exception:
                probe = None
            if isinstance(probe, dict):
                legacy_schema = probe.get("experiment_schema")
                if isinstance(legacy_schema, str) and legacy_schema.strip():
                    classification = "legacy_experiment_summary"
                    message = (
                        f"{message}; legacy experiment summary "
                        f"(experiment_schema={legacy_schema.strip()!r}) is not "
                        "an evidence-catalog artifact and gains no eligibility"
                    )'''
assert old_scan in s, "ec2"
s = s.replace(old_scan, new_scan, 1)

old_append = "            issues.append(EvidenceCatalogIssue(relative, message))"
new_append = """            issues.append(
                EvidenceCatalogIssue(
                    relative, message, classification=classification
                )
            )"""
assert old_append in s, "ec3"
s = s.replace(old_append, new_append, 1)
p.write_text(s)
print("evidence_catalog patched")

# ================= research_status.py =================
r = pathlib.Path("src/quantlab/research/research_status.py")
t = r.read_text()

old_call = "    catalog = build_evidence_catalog(experiment_root, strict=True)"
new_call = """    # Legacy experiment summaries and malformed artifacts are classified and
    # reported instead of crashing the whole status view; the strict entry
    # remains for consumers that must hard-fail.
    catalog = build_evidence_catalog(experiment_root, strict=False)"""
assert old_call in t, "rs1"
t = t.replace(old_call, new_call, 1)

old_payload = '''        "evidence_catalog": {
            "summary": summarize_evidence_catalog(catalog),
            "entries": [asdict(item) for item in catalog.entries],
            "issues": [asdict(item) for item in catalog.issues],
        },'''
new_payload = old_payload + '''
        "incompatible_artifacts": [
            {
                "relative_path": issue.relative_path,
                "classification": issue.classification,
                "message": issue.message,
            }
            for issue in catalog.issues
        ],'''
assert old_payload in t, "rs2"
t = t.replace(old_payload, new_payload, 1)

old_core = '''        "claim": "read_only_structural_research_status_not_performance_or_execution_authority",
    }
    return {**core, "status_fingerprint": _canonical_hash(core)}'''
new_core = '''        "claim": "read_only_structural_research_status_not_performance_or_execution_authority",
    }
    has_corrupt = any(
        issue.classification == "malformed_evidence" for issue in catalog.issues
    )
    has_legacy = any(
        issue.classification == "legacy_experiment_summary"
        for issue in catalog.issues
    )
    if has_corrupt:
        core["overall_status"] = "ready_with_corrupt_evidence"
    elif has_legacy:
        core["overall_status"] = "ready_with_legacy_artifacts"
    else:
        core["overall_status"] = "clean"
    return {**core, "status_fingerprint": _canonical_hash(core)}'''
assert old_core in t, "rs3"
t = t.replace(old_core, new_core, 1)

old_fmt = '''    strategies = payload["strategy_readiness"]["strategies"]
    lines = [
        "QuantLab research status",'''
new_fmt = '''    strategies = payload["strategy_readiness"]["strategies"]
    incompatible = payload.get("incompatible_artifacts", [])
    lines = [
        "QuantLab research status",
        f"  overall status: {payload.get('overall_status', 'clean')}",'''
assert old_fmt in t, "rs4"
t = t.replace(old_fmt, new_fmt, 1)

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
assert old_tail in t, "rs5"
t = t.replace(old_tail, new_tail, 1)
r.write_text(t)
print("research_status patched")

# ================= __main__.py =================
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
    except Exception as exc:  # CLI boundary reports the failure explicitly
        print(f"research status could not be composed: {exc}")
        return 2
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_research_status(payload))
    if payload.get("overall_status") == "ready_with_corrupt_evidence":
        return 1
    return 0'''
assert old_main in u, "main1"
u = u.replace(old_main, new_main, 1)
m.write_text(u)
print("__main__ patched")
