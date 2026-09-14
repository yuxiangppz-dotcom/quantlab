import pathlib

p = pathlib.Path("tests/research/test_s4_replay_admission.py")
s = p.read_text()
old = '"context.fees.additional_fee_fixed_fen: unconfirmed additional-fee component stays unknown",'
new = '"context.fees.additional_fee_fixed_fen: missing (unknown)",'
assert old in s, "old wording not found"
p.write_text(s.replace(old, new))
print("fixed")
