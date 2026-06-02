import json

import pytest

from agent.classifier_eval import (
    ClassMetrics,
    EvalReport,
    accuracy,
    build_report,
    confusion_matrix,
    macro_f1,
    per_class_metrics,
)


# --- ClassMetrics ---------------------------------------------------------

def test_class_metrics_precision_recall_f1_known_counts():
    m = ClassMetrics(tp=8, fp=2, fn=2)
    assert m.precision == pytest.approx(0.8)
    assert m.recall == pytest.approx(0.8)
    assert m.f1 == pytest.approx(0.8)


def test_class_metrics_asymmetric():
    # precision = 6/(6+4)=0.6, recall = 6/(6+2)=0.75
    # f1 = 2*0.6*0.75/(0.6+0.75) = 0.9/1.35 = 0.6667
    m = ClassMetrics(tp=6, fp=4, fn=2)
    assert m.precision == pytest.approx(0.6)
    assert m.recall == pytest.approx(0.75)
    assert m.f1 == pytest.approx(0.6666666, abs=1e-5)


def test_class_metrics_div_by_zero_guards():
    empty = ClassMetrics(tp=0, fp=0, fn=0)
    assert empty.precision == 0.0
    assert empty.recall == 0.0
    assert empty.f1 == 0.0
    # precision defined but recall zero -> f1 zero
    only_fp = ClassMetrics(tp=0, fp=3, fn=0)
    assert only_fp.precision == 0.0
    assert only_fp.f1 == 0.0


# --- confusion_matrix -----------------------------------------------------

def test_confusion_matrix_counts():
    pairs = [
        ("HIGH", "HIGH"),
        ("HIGH", "LOW"),
        ("LOW", "LOW"),
        ("SKIP", "SKIP"),
        ("SKIP", "HIGH"),
    ]
    cm = confusion_matrix(pairs)
    assert cm["HIGH"]["HIGH"] == 1
    assert cm["HIGH"]["LOW"] == 1
    assert cm["LOW"]["LOW"] == 1
    assert cm["SKIP"]["SKIP"] == 1
    assert cm["SKIP"]["HIGH"] == 1


def test_confusion_matrix_empty():
    assert confusion_matrix([]) == {}


# --- per_class_metrics (one-vs-rest) -------------------------------------

def test_per_class_metrics_one_vs_rest_hand_computed():
    # Build a known set:
    #   HIGH: 2 correct, 1 expected-HIGH-predicted-LOW (fn for HIGH),
    #         1 expected-LOW-predicted-HIGH (fp for HIGH)
    pairs = [
        ("HIGH", "HIGH"),   # HIGH tp
        ("HIGH", "HIGH"),   # HIGH tp
        ("HIGH", "LOW"),    # HIGH fn ; LOW fp
        ("LOW", "HIGH"),    # HIGH fp ; LOW fn
        ("LOW", "LOW"),     # LOW tp
        ("SKIP", "SKIP"),   # SKIP tp
    ]
    labels = ["HIGH", "LOW", "SKIP"]
    m = per_class_metrics(pairs, labels)

    assert (m["HIGH"].tp, m["HIGH"].fp, m["HIGH"].fn) == (2, 1, 1)
    assert (m["LOW"].tp, m["LOW"].fp, m["LOW"].fn) == (1, 1, 1)
    assert (m["SKIP"].tp, m["SKIP"].fp, m["SKIP"].fn) == (1, 0, 0)

    assert m["HIGH"].precision == pytest.approx(2 / 3)
    assert m["HIGH"].recall == pytest.approx(2 / 3)
    assert m["SKIP"].precision == 1.0
    assert m["SKIP"].recall == 1.0


def test_per_class_metrics_label_never_seen():
    pairs = [("sell", "sell"), ("sell", "not_sell")]
    m = per_class_metrics(pairs, ["sell", "not_sell"])
    assert (m["sell"].tp, m["sell"].fp, m["sell"].fn) == (1, 0, 1)
    # not_sell never expected, but predicted once -> fp 1
    assert (m["not_sell"].tp, m["not_sell"].fp, m["not_sell"].fn) == (0, 1, 0)


# --- accuracy -------------------------------------------------------------

def test_accuracy():
    pairs = [("a", "a"), ("a", "b"), ("b", "b"), ("c", "c")]
    assert accuracy(pairs) == pytest.approx(0.75)


def test_accuracy_empty():
    assert accuracy([]) == 0.0


# --- macro_f1 -------------------------------------------------------------

def test_macro_f1_mean_of_class_f1():
    metrics = {
        "a": ClassMetrics(tp=1, fp=0, fn=0),   # f1 = 1.0
        "b": ClassMetrics(tp=0, fp=0, fn=0),   # f1 = 0.0
    }
    assert macro_f1(metrics) == pytest.approx(0.5)


def test_macro_f1_empty():
    assert macro_f1({}) == 0.0


# --- EvalReport -----------------------------------------------------------

def test_build_report_and_to_dict_json_serializable():
    pairs = [("HIGH", "HIGH"), ("LOW", "SKIP"), ("SKIP", "SKIP")]
    labels = ["HIGH", "LOW", "SKIP"]
    report = build_report("entry", pairs, labels)
    assert report.classifier == "entry"
    assert report.n == 3
    assert report.accuracy == pytest.approx(2 / 3)
    d = report.to_dict()
    # round-trips through JSON
    s = json.dumps(d)
    back = json.loads(s)
    assert back["classifier"] == "entry"
    assert back["n"] == 3
    assert "per_class" in back
    assert back["per_class"]["HIGH"]["tp"] == 1
    assert "confusion" in back
    assert "accuracy" in back
    assert "macro_f1" in back
