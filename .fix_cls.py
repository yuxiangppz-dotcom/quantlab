import pathlib

p = pathlib.Path("src/quantlab/research/evidence_catalog.py")
s = p.read_text()
old = '''            try:
                payload = json.loads(path.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None'''
new = '''            try:
                payload = json.loads(path.read_bytes().decode("utf-8"))
            except Exception:
                payload = None'''
assert old in s, "classification try not found"
p.write_text(s.replace(old, new))
print("classification hardened")
