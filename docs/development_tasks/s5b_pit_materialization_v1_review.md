# S5-B v1.1 self-review

## Findings corrected

1. The first test fixture exposed that equality with a prior low was counted as
   a new low. The implementation now requires a strict undercut of the previous
   60 closes, excluding the endpoint itself.
2. A fixture expecting `base_building` also contained a 10% fall and volatility
   expansion. It was replaced with a controlled shallow decline so the test
   isolates the unreclaimed-low behavior.
3. Empty entity input initially produced a successful empty materialization. It
   now fails explicitly.

## Verified properties

- all windows end at `as_of`; future points do not affect values, issues or the
  fingerprint;
- new-low and breakout reference windows exclude the current endpoint;
- reclaim identity and elapsed sessions are deterministic;
- missing/invalid values and zero denominators remain explicit unknowns;
- entity order is normalized, while source identity changes the fingerprint;
- S5-A and frozen S5-B kernel thresholds are unchanged.

Targeted S5-B suite: 37 passed. Ruff and whitespace checks passed. Full
repository CI remains required before merge.
