import pathlib

p = pathlib.Path("tests/research/test_s4_admission_verifier.py")
s = p.read_text()
old = '''class TestSemanticFalsePassRegressions:
    def _prepared(self, tmp: Path):
        source, canonical, frozen = self._prepared(tmp_path)'''
# actually inspect the file for the real structure first
new = s  # placeholder
p.write_text(s)
print("inspecting separately")
