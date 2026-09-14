# S5-B v1.3 self-review

## User-directed correction

The original task card proposed only 5/10/20-session diagnostic horizons. The
user explicitly allowed a one-day holding intent. The protocol now includes a
1-session signal horizon while keeping diagnostic labels separate from a later
executable holding policy. It cannot claim that close-to-next-close signal
return is tradable P&L.

## Verified properties

- exact S5-B component and membership-audit schema identities are bound;
- horizons are fixed at 1/5/10/20, unique, positive and increasing;
- 100% membership coverage and the four evidence/boundary blockers are fixed;
- state, transition, cash, downside and regime metrics remain explicit;
- comparisons, one-run budget and no-rescan policy are fixed before outcomes;
- the dataclass is immutable and its fingerprint changes with material fields;
- performance claims, frozen holding policy and broker authority fail closed;
- the protocol contains no return, NAV or fitted outcome values.

Targeted S5-B suite: 74 passed. Ruff passed. Full repository CI remains required
before merge.
