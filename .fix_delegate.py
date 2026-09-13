import pathlib

p = pathlib.Path("tests/research/test_s4_replay_admission.py")
s = p.read_text()
old = '''class TestBodyParsingAndCoverage:
    def _attempt_with_body(self, source, instrument, trade_date, body_text, **kwargs):
        TestAttemptVerificationAndGaps._write_attempt(
            self, source, instrument, trade_date, **kwargs
        )'''
new = '''class TestBodyParsingAndCoverage:
    def _write_world(self, tmp_path, **kwargs):
        return TestAttemptVerificationAndGaps._write_world(self, tmp_path, **kwargs)

    def _write_attempt(self, source, instrument, trade_date, **kwargs):
        return TestAttemptVerificationAndGaps._write_attempt(
            self, source, instrument, trade_date, **kwargs
        )

    def _attempt_with_body(self, source, instrument, trade_date, body_text, **kwargs):
        self._write_attempt(source, instrument, trade_date, **kwargs)'''
assert old in s, "class head not found"
p.write_text(s.replace(old, new))
print("delegate added")
