import pathlib

p = pathlib.Path("scripts/s4_admission_independent_verify.py")
s = p.read_text()
old = '''    # 5. prior20 classifications re-derived from raw evidence.
    gap_failures = []
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        year, month, _ = trade_date.split("-")
        stamp = trade_date.replace("-", "")
        susp_dir = (
            canonical_dir / f"lifecycle_context_v1/suspensions/year={year}/month={month}"
        )
        day = pd.DataFrame()
        if susp_dir.is_dir():
            frame = pd.concat(pd.read_parquet(p) for p in sorted(susp_dir.glob("*.parquet")))
            day = frame[
                (frame["instrument_id"] == code)
                & (frame["trade_date"].astype(str).str[:10] == trade_date)
            ]
        timing = [
            None if pd.isna(v) or str(v).strip() in ("", "None") else str(v)
            for v in day.get("suspend_timing", [])
        ]
        full_day_local = (
            len(day) > 0
            and (day["suspend_type"].astype(str) == "S").all()
            and all(t is None for t in timing)
        )
        attempt = (
            source_dir
            / f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}/result.json"
        )
        result = json.loads(attempt.read_text())
        body = (attempt / "response.body").read_bytes()
        inner = intent["request"]["parameters"]["params"]
        payload = json.loads(body)
        response_empty = (
            result.get("intent_fingerprint") == intent.get("fingerprint")
            and inner.get("ts_code") == code
            and inner.get("start_date") == stamp
            and result.get("rows") == 0
            and result.get("status") == "empty"
            and payload.get("code") == 0
            and payload.get("data", {}).get("items") == []
        )
        expected = (
            "proven_full_day_suspension_supplier_basis"
            if response_empty and full_day_local and gap.get("local_bar_present") is False
            else "other"
        )
        if gap["classification"] != expected or gap["suspension_on_date"] is not True:
            gap_ok = False
            gap_notes.append(f"{code} {trade_date}: {gap['classification']}")'''
new = '''    # 5. prior20 classifications re-derived from raw evidence. The daily
    # coverage is re-derived here as well: target-date absence can never
    # back a full-day claim.
    gap_failures = []
    for gap in report.get("prior20_gaps", []):
        code, trade_date = gap["instrument_id"], gap["trade_date"]
        year, month, _ = trade_date.split("-")
        stamp = trade_date.replace("-", "")
        attempt = source_dir / f"s4_entry_raw_precision/attempts/daily_{code}_{stamp}"
        response_empty = False
        try:
            intent = json.loads((attempt / "intent.json").read_text())
            result = json.loads((attempt / "result.json").read_text())
            body = (attempt / "response.body").read_bytes()
            inner = intent["request"]["parameters"]["params"]
            payload = json.loads(body)
            response_empty = (
                result.get("intent_fingerprint") == intent.get("fingerprint")
                and inner.get("ts_code") == code
                and inner.get("start_date") == stamp
                and result.get("rows") == 0
                and result.get("status") == "empty"
                and payload.get("code") == 0
                and payload.get("data", {}).get("items") == []
            )
        except Exception:
            response_empty = False
        month_dir = canonical_dir / f"daily/year={year}/month={month}"
        parts = sorted(month_dir.glob("*.parquet")) if month_dir.is_dir() else []
        date_present_locally = False
        actual_bar = False
        if parts:
            frame = pd.concat(pd.read_parquet(p) for p in parts)
            date_col = "trade_date" if "trade_date" in frame.columns else "session"
            date_rows = frame[frame[date_col].astype(str).str[:10] == trade_date]
            if not date_rows.empty:
                date_present_locally = True
                code_col = "ts_code" if "ts_code" in frame.columns else "instrument_id"
                actual_bar = bool(len(date_rows[date_rows[code_col] == code]) > 0)
        susp_dir = canonical_dir / (
            f"lifecycle_context_v1/suspensions/year={year}/month={month}"
        )
        day = pd.DataFrame()
        if susp_dir.is_dir():
            frame = pd.concat(
                pd.read_parquet(p) for p in sorted(susp_dir.glob("*.parquet"))
            )
            day = frame[
                (frame["instrument_id"] == code)
                & (frame["trade_date"].astype(str).str[:10] == trade_date)
            ]
        timing = [
            None if pd.isna(v) or str(v).strip() in ("", "None") else str(v)
            for v in day.get("suspend_timing", [])
        ]
        full_day_local = (
            len(day) > 0
            and (day["suspend_type"].astype(str) == "S").all()
            and all(t is None for t in timing)
        )
        if not response_empty:
            expected = "response_missing_or_invalid"
        elif not date_present_locally:
            expected = "local_coverage_incomplete"
        elif actual_bar:
            expected = "conflicting_local_bar"
        elif full_day_local:
            expected = "proven_full_day_suspension_supplier_basis"
        elif day.empty:
            expected = "undetermined"
        else:
            expected = "suspension_timing_present"
        if gap["classification"] != expected:
            gap_ok = False
            gap_notes.append(f"{code} {trade_date}: {gap['classification']}")'''
assert old in s, "prior20 block not found"
p.write_text(s.replace(old, new))
print("verifier prior20 re-derivation completed")
