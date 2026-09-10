from datetime import date

from quantlab.research.cadence_audit import _contained_end
from quantlab.research.factor_experiment import _cadence_signal_dates


def test_cadence_choice_is_bounded_and_weekly_uses_first_open() -> None:
    sessions = [date(2026, 9, day) for day in (1, 2, 3, 4, 7, 8, 9)]
    assert _cadence_signal_dates(sessions, sessions[0], sessions[-1], "daily") == sessions
    assert _cadence_signal_dates(sessions, sessions[0], sessions[-1], "weekly") == [
        date(2026, 9, 1),
        date(2026, 9, 7),
    ]


def test_label_containment_removes_last_horizon_sessions() -> None:
    sessions = [date(2026, 9, day) for day in (1, 2, 3, 4, 7, 8, 9)]
    assert _contained_end(sessions, date(2026, 9, 9), 2) == date(2026, 9, 7)
