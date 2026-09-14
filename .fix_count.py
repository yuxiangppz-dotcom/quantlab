import pathlib

p = pathlib.Path("scripts/s4_admission_independent_verify.py")
s = p.read_text()
old = '''    reported_counts = report.get("gap_counts_by_field", {})
    count_failures = []
    rederived_counts = {
        name: len(codes)
        for name, codes in unknown_fields_by_instrument.items()
    }'''
new = '''    reported_counts = report.get("gap_counts_by_field", {})
    count_failures = []
    rederived_counts = {
        name: len(codes)
        for name, codes in unknown_by_field.items()
    }'''
assert old in s, "count block not found"
p.write_text(s.replace(old, new))
print("count loop fixed")
