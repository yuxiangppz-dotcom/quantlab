import pathlib

p = pathlib.Path("tests/research/test_s4_admission_verifier.py")
s = p.read_text()

old = '''def _synthetic_frozen(source: Path) -> dict:
    plan_payload = json.loads((source / "s4_first_entry_plan/plan.json").read_text())
    recon_payload = json.loads(
        (source / "s4_entry_raw_precision/reconciliation.json").read_text()
    )
    return {
        "plan": plan_payload["fingerprint"],
        "reconciliation": recon_payload["fingerprint"],
    }


def _run_verifier(
    package: Path,
    source: Path,
    canonical: Path,
    expected_gap_count: int = 1,
) -> int:
    """In-process verification with the synthetic world's own frozen map."""
    payload = verifier.verify(
        package,
        source,
        canonical,
        frozen=_synthetic_frozen(source),
        expected_gap_count=expected_gap_count,
    )'''
new = '''def _run_verifier(
    package: Path,
    source: Path,
    canonical: Path,
    frozen: dict,
    expected_gap_count: int = 1,
) -> int:
    """In-process verification against the frozen map captured at generation
    time — never re-read from a source tree that may itself be tampered."""
    payload = verifier.verify(
        package,
        source,
        canonical,
        frozen=frozen,
        expected_gap_count=expected_gap_count,
    )'''
assert old in s
s = s.replace(old, new)

# Every call site: capture the frozen map right after _build_world.
s = s.replace(
    '''        source, canonical, plan, recon_body = self._world(tmp_path)''',
    '''        source, canonical, plan, recon_body, frozen = self._world(tmp_path)''',
)
s = s.replace(
    '''        output = tmp_path / "pkg"
        self._package(output, plan, recon_body)
        assert _run_verifier(output, source, canonical) == 0''',
    '''        output = tmp_path / "pkg"
        self._package(output, plan, recon_body)
        assert _run_verifier(output, source, canonical, frozen) == 0''',
)
s = s.replace(
    '''        package_path.write_text(json.dumps(package))
        self._write_completed(output)
        assert _run_verifier(output, source, canonical) != 0''',
    '''        package_path.write_text(json.dumps(package))
        _write_completed(output)
        assert _run_verifier(output, source, canonical, frozen) != 0''',
)
s = s.replace(
    '''        item["open_gaps"] = [g for g in item["open_gaps"] if g != "prior20_amount_fen"]''',
    '''        item["open_gaps"] = [g for g in item["open_gaps"] if g != "prior20_amount_fen"]''',
)
s = s.replace(
    '''        package_path.write_text(json.dumps(package))
        _write_completed(output)
        assert _run_verifier(output, source, canonical) != 0''',
    '''        package_path.write_text(json.dumps(package))
        _write_completed(output)
        assert _run_verifier(output, source, canonical, frozen) != 0''',
)
s = s.replace(
    '''        completed_path.write_text(json.dumps(completed))
        assert _run_verifier(output, source, canonical) != 0''',
    '''        completed_path.write_text(json.dumps(completed))
        assert _run_verifier(output, source, canonical, frozen) != 0''',
)

old_world_ret = '''    return source, canonical, plan, recon_body'''
new_world_ret = '''    synthetic_frozen = {
        "plan": plan["fingerprint"],
        "reconciliation": recon_body["fingerprint"],
    }
    return source, canonical, plan, recon_body, synthetic_frozen'''
assert old_world_ret in s
s = s.replace(old_world_ret, new_world_ret)

old_world_sig = '''    def _world(self, tmp: Path) -> tuple[Path, Path, dict, dict]:
        return _build_world(tmp)'''
new_world_sig = '''    def _world(self, tmp: Path) -> tuple[Path, Path, dict, dict, dict]:
        return _build_world(tmp)'''
assert old_world_sig in s
s = s.replace(old_world_sig, new_world_sig)

old_builder_ret = '''    return source, canonical, plan, recon_body


def _hand_build_package'''
new_builder_ret = '''    synthetic_frozen = {
        "plan": plan["fingerprint"],
        "reconciliation": recon_body["fingerprint"],
    }
    return source, canonical, plan, recon_body, synthetic_frozen


def _hand_build_package'''
assert old_builder_ret in s
s = s.replace(old_builder_ret, new_builder_ret)

# drop the now-unused _synthetic_frozen helper if still present
s = s.replace('''def _synthetic_frozen(source: Path) -> dict:
    plan_payload = json.loads((source / "s4_first_entry_plan/plan.json").read_text())
    recon_payload = json.loads(
        (source / "s4_entry_raw_precision/reconciliation.json").read_text()
    )
    return {
        "plan": plan_payload["fingerprint"],
        "reconciliation": recon_payload["fingerprint"],
    }


''', "")

p.write_text(s)
print("frozen-map capture-at-generation applied")
