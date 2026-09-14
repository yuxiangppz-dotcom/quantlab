import pathlib

p = pathlib.Path("tests/research/test_s4_admission_verifier.py")
s = p.read_text()
old = '''    package = {
        "decision_date": "2021-12-31",'''
new = '''    unknown_by_field: dict[str, list[str]] = {}
    for item in instruments:
        for name in item["open_gaps"]:
            unknown_by_field.setdefault(name, []).append(item["instrument_id"])
    gap_counts = {
        name: {"instruments": len(codes), "examples": codes[:5]}
        for name, codes in sorted(unknown_by_field.items())
    }
    package = {
        "decision_date": "2021-12-31",'''
assert old in s
s = s.replace(old, new, 1)

old2 = '''        "economic_paths_started": 0,
        "model_fits_used": 0,
        "provider_calls": 0,
        "manifest": {"files": _bound_files(source, canonical),'''
new2 = '''        "gap_counts_by_field": gap_counts,
        "economic_paths_started": 0,
        "model_fits_used": 0,
        "provider_calls": 0,
        "manifest": {"files": _bound_files(source, canonical),'''
assert old2 in s
s = s.replace(old2, new2, 1)
p.write_text(s)
print("hand-built report now carries gap summary")
