"""Once-only alternate scalar/Fraction verification; no scorer or writer reuse."""

from __future__ import annotations

import json
import math
import subprocess
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.data.program_intake import now
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.financing_diagnostic import COPY, KEYS, OUTPUT, load_contract
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries


def number(value, positive=False):
    if isinstance(value, (bool, str)) or value is None:
        return None
    if not isinstance(value, (int, float, Decimal, np.number)) or not math.isfinite(value):
        return None
    invalid_sign = value <= 0 if positive else value < 0
    if invalid_sign:
        return None
    return Fraction(str(value))


def close(actual, expected):
    if expected is None or pd.isna(expected):
        assert pd.isna(actual), (actual, expected)
    else:
        assert math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-12), (
            actual,
            expected,
        )


def ranks(values):
    ordered = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    first = 0
    while first < len(ordered):
        last = first + 1
        while last < len(ordered) and values[ordered[last]] == values[ordered[first]]:
            last += 1
        for j in ordered[first:last]:
            result[j] = (first + 1 + last) / 2
        first = last
    return result


def correlation(xs, ys):
    pairs = [
        (float(x), float(y))
        for x, y in zip(xs, ys, strict=True)
        if pd.notna(x) and pd.notna(y) and math.isfinite(x) and math.isfinite(y)
    ]
    if len(pairs) < 30:
        return None
    x, y = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    center = (len(x) + 1) / 2
    xx = math.fsum((v - center) ** 2 for v in x)
    yy = math.fsum((v - center) ** 2 for v in y)
    return (
        math.fsum((a - center) * (b - center) for a, b in zip(x, y, strict=True))
        / math.sqrt(xx * yy)
        if xx and yy
        else None
    )


def interval(values):
    n = len(values)
    if n < 40 or sum(math.isfinite(v) for v in values) < 40:
        return None
    generator = np.random.default_rng(20260912)
    means = []
    for _ in range(1000):
        starts = generator.integers(0, n - 19, size=math.ceil(n / 20))
        indices = [int(s) + j for s in starts for j in range(20)][:n]
        finite = [values[i] for i in indices if math.isfinite(values[i])]
        if finite:
            means.append(math.fsum(finite) / len(finite))
    means.sort()

    def quantile(q):
        position = (len(means) - 1) * q
        lo, hi = math.floor(position), math.ceil(position)
        return means[lo] + (means[hi] - means[lo]) * (position - lo)

    return [quantile(0.025), quantile(0.975)] if means else None


def reconcile(root, config, codes, sessions, decisions, report):
    out = root / OUTPUT
    features = pd.read_parquet(out / "features.parquet", use_threads=False)
    metrics = pd.read_parquet(out / "daily_metrics.parquet", use_threads=False)
    csv = pd.read_csv(out / "daily_metrics.csv", parse_dates=["trade_date"])
    pd.testing.assert_frame_equal(metrics, csv, check_dtype=False, rtol=1e-10, atol=1e-12)
    expected_index = pd.MultiIndex.from_product([codes, pd.DatetimeIndex(decisions)], names=KEYS)
    assert pd.MultiIndex.from_frame(features[KEYS]).equals(expected_index)
    assert metrics.trade_date.tolist() == list(pd.DatetimeIndex(decisions))
    parent = pd.read_parquet(
        root / config["parent_path"], columns=[*KEYS, "amount", *COPY], use_threads=False
    )
    pd.testing.assert_frame_equal(
        features[[*KEYS, *COPY]],
        parent.sort_values(KEYS)[[*KEYS, *COPY]].reset_index(drop=True),
        check_dtype=False,
    )
    amounts = {
        (r.instrument_id, r.trade_date.strftime("%Y-%m-%d")): number(r.amount, True)
        for r in parent.itertuples(index=False)
    }
    for path in config["warmup_paths"]:
        warm = pd.read_parquet(root / path, columns=[*KEYS, "amount"], use_threads=False)
        for r in warm.itertuples(index=False):
            if r.instrument_id in codes:
                key = (r.instrument_id, pd.Timestamp(r.trade_date).strftime("%Y-%m-%d"))
                assert key not in amounts
                amounts[key] = number(r.amount, True)
    balances = {}
    for code in codes:
        raw = root / config["intake_path"] / "attempts" / ("margin_" + code) / "response.body"
        data = json.loads(raw.read_bytes(), parse_float=Decimal)["data"]
        for values in data["items"]:
            row = dict(zip(data["fields"], values, strict=True))
            assert row["ts_code"] == code
            day = row["trade_date"]
            key = (code, day[:4] + "-" + day[4:6] + "-" + day[6:])
            assert key not in balances
            balances[key] = number(row["rzye"])
    positions = {d: i for i, d in enumerate(sessions)}
    valid = 0
    for row in features.itertuples(index=False):
        day = row.trade_date.strftime("%Y-%m-%d")
        end = positions[day] - 1
        bs = [balances.get((row.instrument_id, sessions[i])) for i in range(end - 5, end + 1)]
        aa = [amounts.get((row.instrument_id, sessions[i])) for i in range(end - 4, end + 1)]
        bc, ac = all(v is not None for v in bs), all(v is not None for v in aa)
        denominator = sum(aa, Fraction(0)) if ac else None
        expected = (bs[-1] - bs[0]) / denominator if bc and ac else None
        assert row.balance_window_complete == bc and row.amount_window_complete == ac
        assert row.source_trade_date == pd.Timestamp(sessions[end])
        close(row.source_rzye, bs[-1])
        close(row.source_rzye_five_sessions_before, bs[0])
        close(row.source_amount5_cny, denominator)
        close(row.F7, expected)
        valid += expected is not None
    assert report["grid_rows"] == len(features) == 310272
    assert report["feature_valid"] == valid and report["feature_unknown"] == len(features) - valid
    for (_, group), row in zip(
        features.groupby("trade_date", sort=True), metrics.itertuples(index=False), strict=True
    ):
        common = group[np.isfinite(group[["F7", "momentum20", "label_5"]]).all(axis=1)]
        primary = correlation(common.F7.tolist(), common.label_5.tolist())
        reference = correlation((-common.momentum20).tolist(), common.label_5.tolist())
        if primary is None or reference is None:
            primary = reference = None
        assert row.pairs == len(common)
        assert row.feature_valid == int(np.isfinite(group.F7).sum())
        assert row.balance_complete == int(group.balance_window_complete.sum())
        assert row.amount_complete == int(group.amount_window_complete.sum())
        close(row.rank_ic, primary)
        close(row.reference_rank_ic, reference)
        close(row.paired_difference, primary - reference if primary is not None else None)
        for column, values in (
            (
                "size_rank_correlation",
                [math.log(x) if math.isfinite(x) and x > 0 else math.nan for x in group.circ_mv],
            ),
            ("turnover_rank_correlation", group.turnover_rate.tolist()),
            ("reversal_rank_correlation", (-group.momentum20).tolist()),
        ):
            close(getattr(row, column), correlation(group.F7.tolist(), values))
    periods = [(str(y), y, y) for y in range(2020, 2025)] + [
        ("2020-2022", 2020, 2022),
        ("2023-2024", 2023, 2024),
        ("2020-2024", 2020, 2024),
    ]
    for (name, first, last), saved in zip(periods, report["summaries"], strict=True):
        g = metrics[metrics.trade_date.dt.year.between(first, last)]
        assert saved["period"] == name and saved["calendar_sessions"] == len(g)
        assert saved["valid_ic_sessions"] == int(g.rank_ic.notna().sum())
        assert saved["paired_observations"] == int(g.pairs.sum())
        for key in (
            "rank_ic",
            "reference_rank_ic",
            "paired_difference",
            "size_rank_correlation",
            "turnover_rank_correlation",
            "reversal_rank_correlation",
        ):
            finite = [float(v) for v in g[key] if pd.notna(v)]
            close(saved["mean_" + key], math.fsum(finite) / len(finite) if finite else None)
        for key, target in (
            ("rank_ic", "ic_block_95_interval"),
            ("paired_difference", "difference_block_95_interval"),
        ):
            expected = interval(g[key].tolist())
            if expected is None:
                assert saved[target] is None
            else:
                for actual, wanted in zip(saved[target], expected, strict=True):
                    close(actual, wanted)
    return valid


def prove(root):
    binding = InputBinding(root)
    head = code_binding(root, binding)
    assert (
        head
        == subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    )
    config, codes, sessions, decisions = load_contract(root)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, config["resources"])
        budget.check()
        intent = atomic_seal(
            out / "proof_intent.json", {"at": now(), "source_head": head, "attempt": 1}
        )
        try:
            with budget.watchdog():
                report = sealed_read(out / "report.json")
                actual_intent = sealed_read(out / "intent.json")
                assert report["source_head"] == actual_intent["source_head"] == head
                assert report["intent_fingerprint"] == actual_intent["fingerprint"]
                assert (
                    report["funding_identity"] == "F7"
                    and report["funding_identities_cumulative"] == 5
                )
                assert all(
                    report[k] is False
                    for k in (
                        "historical_pit_certified",
                        "performance_evidence",
                        "execution_authority",
                        "candidate_promotion_eligible",
                    )
                )
                assert (
                    report["provider_calls"]
                    == report["economic_paths"]
                    == report["model_fits"]
                    == 0
                )
                verify_entries(out, report["artifacts"])
                valid = reconcile(root, config, codes, sessions, decisions, report)
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "proof.json",
                    {
                        "at": now(),
                        "source_head": head,
                        "report_fingerprint": report["fingerprint"],
                        "intent_fingerprint": intent["fingerprint"],
                        "rows_checked": 310272,
                        "valid_features_checked": valid,
                        "daily_summaries_checked": 1212,
                        "period_summaries_checked": 8,
                        "method": (
                            "raw Decimal/Fraction six/five-session windows; "
                            "scalar average-tie rank; calendar block resampling; "
                            "all rows; same agent, not independent personnel"
                        ),
                        "relative_tolerance": 1e-10,
                        "absolute_tolerance": 1e-12,
                        "historical_pit_certified": False,
                        "performance_evidence": False,
                        "execution_authority": False,
                        "seconds": time.monotonic() - budget.started,
                        "peak_rss_bytes": peak_rss_bytes(),
                        "generated_bytes": budget.check(),
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "proof_failed.json",
                {
                    "at": now(),
                    "intent_fingerprint": intent["fingerprint"],
                    "error_type": type(exc).__name__,
                },
            )
            raise


if __name__ == "__main__":
    print(prove(Path.cwd())["fingerprint"])
