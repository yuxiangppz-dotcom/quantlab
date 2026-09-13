import pathlib

p = pathlib.Path("src/quantlab/research/s4_replay_admission.py")
s = p.read_text()

old = '''    volume = context.get("session_volume_shares")
    if volume is None:
        gaps.append("session_volume_shares: missing (unknown)")
    elif not _is_positive_int(volume):
        gaps.append("session_volume_shares: must be a positive integer")'''
new = '''    volume = context.get("session_volume_shares")
    if volume is None:
        gaps.append("session_volume_shares: missing (unknown)")
    elif type(volume) is not int:
        gaps.append("session_volume_shares: must be an integer")
    elif volume < 0:
        # Zero is a legal known no-trade observation, never a gap.
        gaps.append("session_volume_shares: negative volumes are invalid")'''
assert old in s
s = s.replace(old, new)

old2 = '''    amount = context.get("session_amount_fen")
    if type(amount) is int and amount < 0:
        gaps.append("session_amount_fen: negative amounts are invalid (zero is legal no-trade)")'''
assert old2 in s  # already zero-tolerant

old3 = '''        if fees.get("minimum_commission_fen") is None:
            gaps.append("fees.minimum_commission_fen: missing (unknown)")'''
new3 = '''        minimum = fees.get("minimum_commission_fen")
        if minimum is None:
            gaps.append("fees.minimum_commission_fen: missing (unknown)")
        elif type(minimum) is not int or minimum < 0:
            gaps.append("fees.minimum_commission_fen: must be a nonnegative integer")'''
assert old3 in s
s = s.replace(old3, new3)

p.write_text(s)
print("zero-value semantics fixed")
