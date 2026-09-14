import pathlib

p = pathlib.Path("scripts/s4_admission_independent_verify.py")
s = p.read_text()
old = '''    missing_required = sorted(required_sources - set(manifest_files))
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
        )'''
new = '''    missing_required = sorted(required_sources - set(manifest_files))
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
assert old in s, "extra block not found"
p.write_text(s.replace(old, new))
print("extra detection cleaned")
