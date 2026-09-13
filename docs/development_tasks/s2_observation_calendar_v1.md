# Resolve the planned S2 observation end date, without evaluating future prices

Direct approved v3 data commission; clean pushed expected_head
12bded79268563499705e2a7465db63e488e72d3, PR116/master CI34735505916 passed both jobs.
Single agent, paused old loop, no competing writer/heavy job. Push card first.

The new S2 observation's fixed entry isSep14 and exit is its20th subsequent common
trading session. The existing calendar endsOct15. Preserve the original observation
2b9a706a45d08443d9063ae180c7f8c5edd385a90144457d48ee0b28e14f105f and proof
e8ed5c36613c3316d705986f45d47a1a4ebb4f34317c1c8abd7111b77cd88ecc unchanged.
Create only a separate source-bound planned-calendar addendum, never future prices,
labels, return estimates, model fits or new candidate identities.

Exactly two Tushare trade_cal requests, in order SSE then SZSE, start_date20260911,
end_date20261031, fields exchange,cal_date,is_open; each at most64KiB and one attempt,
zero retries/alternative endpoints. Save durable intents, raw bytes, timestamps and
results in a new research output; no canonical writes. One official trade_cal
documentation page may be opened, zero other browsing or original-file downloads.
Stop on any failed/partial/schema-invalid request, freeze the failure and keep the
end date unknown; do not retry, infer one exchange from the other or use weekdays.

Require each source to contain every51 calendar dates exactly once with the exact
exchange and integer0/1 is_open. Require the two ordered open-session lists equal,
andSep14 open in both. Derive only the21-session prefix startingSep14, hence the
20th subsequent session. Treat this as the calendar plan observed now; future
exceptional closures still require revalidation at label evaluation. It is not
evidence that future markets actually opened. Do not mutate existing S4/S2 rows.

Bind card/config and the existing S2 observation/proof before actual. One actual
two-request acquisition/addendum and one independent source/date/arithmetic proof.
Charge up to2 additional provider requests on top of355, no budget reset. No old
intake, old proof or signal rerun. Shared heavy lock,600s actual,2GiB RSS,8MiB output,
8GiB physical D reserve. Target15 minutes useful work. Synthetic tests, fullpytest,
Ruff/diff, self-review, commit/push before requests/proof; PR CI/merge/master CI.
