"""Unit tests for the order-flow domain package's own interfaces: classifiers,
registry, volume extraction, the pipeline's trade events, and the package's
isolation from web/broker/app code."""
import ast
from pathlib import Path

import pytest

from backend.app.domain.orderflow import (CLASSIFIERS, ClassificationContext, CumulativeVolumeExtractor,
                                          LeeReadyClassifier, TickProcessorState, TickRuleClassifier,
                                          VtrenderReconstructedClassifierV1, get_classifier, process_tick)

DOMAIN_DIR = Path(__file__).resolve().parent.parent / "backend" / "app" / "domain" / "orderflow"


def ctx(price, bid=None, ask=None, prev_price=None, prev_side=None, quote_age_ms=None):
    return ClassificationContext(price, bid, ask, prev_price, prev_side, quote_age_ms)


# ---- VtrenderReconstructedClassifierV1 ------------------------------------------

@pytest.mark.parametrize("price,expected", [(99.9, "BUY"), (100.1, "SELL"), (100.0, "SELL")])
def test_v1_midpoint_rule_inverted_polarity_and_at_mid_is_sell(price, expected):
    # bid 99.8 / ask 100.2 -> mid exactly 100.0; at mid is SELL (P < mid is false)
    c = VtrenderReconstructedClassifierV1().classify(ctx(price, 99.8, 100.2))
    assert (c.side, c.method) == (expected, "Midpoint Rule (VTRenders)")


def test_v1_at_mid_decision_depends_on_float_rounding_known_quirk():
    """Pinned known quirk of v1, kept for reproducibility: the midpoint is a
    float, so a trade exactly at mid can land on either side of it.
    (99.9 + 100.2) / 2 == 100.05000000000001, so 100.05 counts as below mid
    and is classified BUY, not SELL. In the live sessions 418 of 40,210
    midpoint-rule trades (1.04%) printed exactly at mid; 11 of them went BUY
    this way. A fix (exact paise arithmetic) must ship as a new classifier
    version, not as a change to v1."""
    c = VtrenderReconstructedClassifierV1().classify(ctx(100.05, 99.9, 100.2))
    assert c.side == "BUY"


def test_v1_stale_quote_falls_back_to_tick_rule_at_the_threshold():
    v1 = VtrenderReconstructedClassifierV1()
    at = v1.classify(ctx(100.2, 99.9, 100.3, prev_price=100.0, quote_age_ms=14000))
    below = v1.classify(ctx(100.2, 99.9, 100.3, prev_price=100.0, quote_age_ms=13999))
    assert (at.side, at.method) == ("BUY", "Tick Rule (stale-quote fallback)")
    assert below.method == "Midpoint Rule (VTRenders)"


def test_v1_one_sided_book_uses_tick_rule():
    v1 = VtrenderReconstructedClassifierV1()
    assert v1.classify(ctx(100.0, 99.9, None)).method == "Cold Start"
    assert v1.classify(ctx(100.0, None, 100.2, prev_price=100.1)).side == "SELL"
    assert v1.classify(ctx(100.0, None, None, prev_price=100.0, prev_side="SELL")).method == "Zero Tick Rule"


def test_v1_stale_threshold_is_configurable():
    c = VtrenderReconstructedClassifierV1(stale_threshold_ms=1000).classify(
        ctx(100.2, 99.9, 100.3, prev_price=100.0, quote_age_ms=1000))
    assert c.method == "Tick Rule (stale-quote fallback)"


# ---- other classifiers ----------------------------------------------------------

def test_lee_ready_quote_rule_then_tick_rule():
    lr = LeeReadyClassifier()
    assert lr.classify(ctx(100.2, 99.9, 100.2)).side == "BUY"      # at ask
    assert lr.classify(ctx(99.9, 99.9, 100.2)).side == "SELL"      # at bid
    inside = lr.classify(ctx(100.0, 99.9, 100.2, prev_price=99.95))
    assert (inside.side, inside.method) == ("BUY", "Tick Rule (uptick)")


def test_tick_rule_ignores_the_book():
    tr = TickRuleClassifier()
    assert tr.classify(ctx(100.0, 99.0, 100.0, prev_price=100.5)).side == "SELL"


def test_every_classification_carries_its_classifier_identity():
    for (name, version), cls in CLASSIFIERS.items():
        c = cls().classify(ctx(100.0, 99.9, 100.2))
        assert (c.classifier_name, c.classifier_version) == (name, version)


# ---- registry ---------------------------------------------------------------------

def test_registry_default_is_v1_and_unknown_versions_fail_loudly():
    assert isinstance(get_classifier(), VtrenderReconstructedClassifierV1)
    assert isinstance(get_classifier("lee_ready", "v1"), LeeReadyClassifier)
    with pytest.raises(ValueError, match="unknown classifier"):
        get_classifier("vtrender_reconstruction", "v2")


# ---- volume extraction ------------------------------------------------------------

def test_cumulative_volume_first_tick_primes_and_reset_resyncs():
    v = CumulativeVolumeExtractor()
    assert [v.extract(x) for x in (1000, 1065, 1065, 500, 630)] == [0, 65, 0, 0, 130]


# ---- pipeline ---------------------------------------------------------------------

def tick(ltt, ltp, cum, bid=99.9, ask=100.2):
    return {"ltp": ltp, "ltt": ltt, "cum_volume": cum,
            "depth_buy": [(bid, 65, 1)], "depth_sell": [(ask, 65, 1)]}


def test_pipeline_emits_trade_events_with_classifier_version():
    state, events = TickProcessorState(0.1), []
    process_tick(state, tick(1_000, 100.0, 0), on_trade=events.append)
    result = process_tick(state, tick(2_000, 100.0, 65), on_trade=events.append)
    assert result == {"discarded": False, "new_trade": True, "candle_ts": 0, "side": "BUY", "qty": 65}
    assert len(events) == 1
    c = events[0].classification
    assert (c.classifier_name, c.classifier_version, c.method) == \
        ("vtrender_reconstruction", "v1", "Midpoint Rule (VTRenders)")
    assert state.last_classification == c


def test_pipeline_uses_the_injected_classifier():
    state = TickProcessorState(0.1, classifier=LeeReadyClassifier())
    process_tick(state, tick(1_000, 100.0, 0))
    assert process_tick(state, tick(2_000, 100.2, 65))["side"] == "BUY"   # at ask: Lee-Ready BUY
    state_v1 = TickProcessorState(0.1)
    process_tick(state_v1, tick(1_000, 100.0, 0))
    assert process_tick(state_v1, tick(2_000, 100.2, 65))["side"] == "SELL"  # above mid: v1 SELL


def test_pipeline_discards_a_crossed_book_without_touching_volume():
    state = TickProcessorState(0.1)
    process_tick(state, tick(1_000, 100.0, 0))
    assert process_tick(state, tick(2_000, 100.0, 65, bid=100.3, ask=100.2)) == \
        {"discarded": True, "reason": "crossed_book"}
    assert process_tick(state, tick(3_000, 100.0, 65))["qty"] == 65   # volume diff not consumed by the discard


def test_compat_observation_records_carry_classifier_version():
    import orderbook_engine as oe
    state, obs = TickProcessorState(0.1), []
    oe.process_tick(state, tick(1_000, 100.0, 0), observation_sink=obs.append)
    oe.process_tick(state, tick(2_000, 100.0, 65), observation_sink=obs.append)
    assert (obs[0]["classifier_name"], obs[0]["classifier_version"]) == ("vtrender_reconstruction", "v1")
    assert obs[0]["algo_reason"] == "Midpoint Rule (VTRenders)"


# ---- isolation ----------------------------------------------------------------------

FORBIDDEN_IMPORTS = {"fastapi", "starlette", "requests", "websockets", "config", "server",
                     "angel_client", "angelone_autologin", "ws_ingest", "observation_store", "research"}


@pytest.mark.parametrize("path", sorted(DOMAIN_DIR.glob("*.py")), ids=lambda p: p.name)
def test_domain_package_imports_nothing_from_app_web_or_broker_layers(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            assert name.split(".")[0] not in FORBIDDEN_IMPORTS, f"{path.name} imports {name}"
