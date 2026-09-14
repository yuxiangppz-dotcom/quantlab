# S5-B v1.3 frozen diagnostic protocol

Issue: #145. Starting master: `6ff6c4a84ef20e50df42e6cd924685b288a47efd`.

This outcome-free task freezes the first S5-B retrospective diagnostic before
any S5-B forward return is loaded. It binds the exact v1 kernel, v1.1 PIT
materializer and v1.2 decision assembler, requires 100% verified membership and
positive eligibility, and permits only a date range chosen from fully covered
membership evidence.

Signal-diagnostic horizons are 1/5/10/20 market sessions. The added one-session
label measures immediate signal resolution; it is not an executable holding
return. A later execution protocol must separately bind the signal timestamp,
next-session entry, earliest legally sellable exit, suspension/limit evidence
and costs. Therefore these diagnostic horizons do not lock the user's holding
period.

The report must retain every state, transition, selected-N, cash date, negative
month/regime and unavailable comparison. Comparisons and one-run/no-rescan
rules are fixed in Issue #145.

Insufficient membership/eligibility/reference evidence or a forward window
crossing the frozen end date stops the diagnostic. No denominator shrinking,
period reselection, outcome loading, provider access, Canonical write, formula
change, tuning, replay, promotion, account mutation or order is authorized.
