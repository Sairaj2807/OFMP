"""Algorithm registry: which algorithms exist, in which versions, and which
one is the default.

Rule: an algorithm's output never changes under an existing version. A
behaviour change is registered as a new version, so data derived with the
old version stays reproducible and identifiable."""
from .classification import (LeeReadyClassifier, TickRuleClassifier, TradeClassifier,
                             VtrenderReconstructedClassifierV1)

CLASSIFIERS = {
    (cls.name, cls.version): cls
    for cls in (VtrenderReconstructedClassifierV1, LeeReadyClassifier, TickRuleClassifier)
}

DEFAULT_CLASSIFIER = (VtrenderReconstructedClassifierV1.name, VtrenderReconstructedClassifierV1.version)

# Versions of the non-classifier engine stages. Bump together with any change
# to their output (the golden test in tests/regression/test_engine_golden.py
# fails when output changes).
ENGINE_ALGORITHMS = {
    "volume_extraction": "v1",    # cumulative-volume diff (volume.py)
    "footprint": "v1",            # 60 s native candles, tick-size price levels (footprint.py)
    "cvd": "v1",                  # sum of closed-candle deltas (cvd.py)
    "value_area": "v1",           # POC-outward expansion, ties go down (analytics.py)
    "stacked_imbalance": "v1",    # engine rule, unvalidated against Vtrender (analytics.py)
}


def get_classifier(name: str = DEFAULT_CLASSIFIER[0], version: str = DEFAULT_CLASSIFIER[1],
                   **kwargs) -> TradeClassifier:
    try:
        cls = CLASSIFIERS[(name, version)]
    except KeyError:
        known = ", ".join(f"{n}/{v}" for n, v in sorted(CLASSIFIERS))
        raise ValueError(f"unknown classifier {name}/{version} (known: {known})") from None
    return cls(**kwargs)
