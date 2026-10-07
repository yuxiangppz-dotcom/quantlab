from copy import deepcopy

from test_nextday_contract import nextday_packet, nextday_result

from quantlab.scout.daily_contract import compact_fact_refs, unpack_facts, validate_output


def test_typed_dictionary_preserves_every_fact_subject_and_value():
    packet = nextday_packet()
    original = deepcopy(packet)
    encoded, aliases = compact_fact_refs(packet)
    assert packet == original
    before, after = unpack_facts(packet), unpack_facts(encoded)
    for identity, fact in before.items():
        restored = after[aliases[identity]]
        for key in ("subject_id", "metric", "period", "value", "unit", "benchmark", "asof_session"):
            assert restored.get(key) == fact.get(key)
        assert restored.get("source_ann_date") == fact.get("source_ann_date")
        assert restored.get("calculation_version") == fact.get("calculation_version")


def test_insufficient_type_cannot_be_ranked_and_displayed_as_watch():
    packet = nextday_packet()
    result = nextday_result(packet)
    row = result["comparisons"][0]
    row.update(
        primary_type="insufficient_evidence",
        type_labels=["insufficient_evidence"],
        final_status="watch",
        evidence_reliability="insufficient",
        comparator_id="000002.SZ",
    )
    assert validate_output(result, packet)
