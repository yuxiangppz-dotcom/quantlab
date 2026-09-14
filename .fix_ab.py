import pathlib

p = pathlib.Path("tests/research/test_s4_admission_verifier.py")
s = p.read_text()

old = '''class TestSemanticFalsePassRegressions:
    def _prepared(self, tmp: Path):
        source, canonical, plan, recon_body, frozen = self._world(tmp)
        output = tmp / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        return source, canonical, frozen

    def test_forced_admitted_labels_with_none_fees_fail(self, tmp_path):
        # Scenario A: every field forced admitted, reasons and gaps cleared,
        # verdict flipped to ready 鈥 while the actual fee facts stay None.
        source, canonical, frozen = self._prepared(tmp_path)
        package_path = output_pkg = None
        package_path = Path(source).parent / "pkg" / "input_package.json"
        report_path = Path(source).parent / "pkg" / "preflight_report.json"'''
new = '''class TestSemanticFalsePassRegressions(TestVerifierTamperRegressions):
    def _prepared(self, tmp: Path):
        source, canonical, plan, recon_body, frozen = self._world(tmp)
        output = tmp / "pkg"
        self._package(output, plan, recon_body, source, canonical)
        return source, canonical, frozen

    def test_forced_admitted_labels_with_none_fees_fail(self, tmp_path):
        # Scenario A: every field forced admitted, reasons and gaps cleared,
        # verdict flipped to ready - while the actual fee facts stay None.
        source, canonical, frozen = self._prepared(tmp_path)
        package_path = tmp_path / "pkg" / "input_package.json"
        report_path = tmp_path / "pkg" / "preflight_report.json"'''
assert old in s, "class head not found"
s = s.replace(old, new)

old2 = '''        report["precise_stop_date"] = None
        report["package"] = package
        report_path.write_text(json.dumps(report))
        _write_completed(Path(source).parent / "pkg")
        assert (
            _run_verifier(
                Path(source).parent / "pkg", source, canonical, frozen
            )
            != 0
        )'''
new2 = '''        report["precise_stop_date"] = None
        report["package"] = package
        report_path.write_text(json.dumps(report))
        _write_completed(tmp_path / "pkg")
        assert _run_verifier(tmp_path / "pkg", source, canonical, frozen) != 0'''
assert old2 in s, "scenario A tail not found"
s = s.replace(old2, new2)

old3 = '''        source, canonical, frozen = self._prepared(tmp_path)
        report_path = Path(source).parent / "pkg" / "preflight_report.json"'''
new3 = '''        source, canonical, frozen = self._prepared(tmp_path)
        report_path = tmp_path / "pkg" / "preflight_report.json"'''
assert old3 in s
s = s.replace(old3, new3)

old4 = '''        report_path.write_text(json.dumps(report))
        _write_completed(Path(source).parent / "pkg")
        assert (
            _run_verifier(Path(source).parent / "pkg", source, canonical, frozen)
            != 0
        )'''
new4 = '''        report_path.write_text(json.dumps(report))
        _write_completed(tmp_path / "pkg")
        assert _run_verifier(tmp_path / "pkg", source, canonical, frozen) != 0'''
assert old4 in s, "scenario B tail not found"
s = s.replace(old4, new4)
p.write_text(s)
print("A/B regressions repaired")
