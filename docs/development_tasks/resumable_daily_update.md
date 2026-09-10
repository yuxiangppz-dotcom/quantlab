# Bounded resumable Daily update — Issue #38

Direct user commission to deliver a usable local tool. Starting clean, pushed
master: `835a5eb0283324a0651bae14ae8d4cb9e980f51c`. The previous fill-time PR #37
passed both CI jobs before merge. Branch: `codex/resumable-daily-update`.

Daily update used calendar progress as a data cursor, skipping a previous
incomplete close after the calendar had advanced. Inspect actual core/index
partitions in the latest five known sessions instead. Report older missing
sessions without downloading history. Reuse existing partitions and stop on
incomplete core or index evidence. Refresh a bounded calendar range including
35 future calendar days for next-session planning, without requesting future bars.
Invalid bounds fail before provider calls. A missing requested calendar record is
unknown, not a declared closed session.

Test only with fake providers and temporary storage, covering interrupted retry,
future calendar, missing core/index, historical gaps, bounds, and unknown calendar.
Run full pytest/Ruff/diff-check/optional runtimes. No real provider or Canonical
operation is authorized by this development card. A real workflow check requires
a separate exact user authorization. Financial and accounting semantics stay frozen.
