"""Pure metrics core for classifier accuracy evaluation.

No I/O, no LLM — fully unit-testable. Computes precision / recall / F1,
confusion matrices, accuracy and macro-F1 from (expected, predicted) label
pairs, for both the multi-class entry buckets {HIGH, LOW, SKIP} and the binary
sell-detection / scope labels.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClassMetrics:
    """One-vs-rest counts and derived scores for a single label."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


def confusion_matrix(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """expected -> predicted -> count over (expected, predicted) pairs."""
    cm: dict[str, dict[str, int]] = {}
    for expected, predicted in pairs:
        row = cm.setdefault(expected, {})
        row[predicted] = row.get(predicted, 0) + 1
    return cm


def per_class_metrics(
    pairs: list[tuple[str, str]], labels: list[str]
) -> dict[str, ClassMetrics]:
    """One-vs-rest metrics for each label.

    For label L: tp = expected L & predicted L; fp = predicted L & expected
    != L; fn = expected L & predicted != L.
    """
    metrics = {label: ClassMetrics() for label in labels}
    for expected, predicted in pairs:
        for label in labels:
            m = metrics[label]
            if expected == label and predicted == label:
                m.tp += 1
            elif predicted == label and expected != label:
                m.fp += 1
            elif expected == label and predicted != label:
                m.fn += 1
    return metrics


def accuracy(pairs: list[tuple[str, str]]) -> float:
    """Fraction of pairs where expected == predicted (empty -> 0.0)."""
    if not pairs:
        return 0.0
    correct = sum(1 for e, p in pairs if e == p)
    return correct / len(pairs)


def macro_f1(metrics: dict[str, ClassMetrics]) -> float:
    """Unweighted mean of per-class F1 (empty -> 0.0).

    A label with zero support (never expected and never predicted) contributes
    F1=0 to the mean, matching sklearn's macro average. As a result a per-slice
    macro-F1 can look low when a class is simply absent from that slice.
    """
    if not metrics:
        return 0.0
    return sum(m.f1 for m in metrics.values()) / len(metrics)


@dataclass
class EvalReport:
    """JSON-serializable summary of one classifier evaluation."""

    classifier: str
    n: int
    labels: list[str]
    per_class: dict[str, ClassMetrics]
    confusion: dict[str, dict[str, int]]
    accuracy: float
    macro_f1: float

    def to_dict(self) -> dict:
        return {
            "classifier": self.classifier,
            "n": self.n,
            "labels": list(self.labels),
            "per_class": {k: v.to_dict() for k, v in self.per_class.items()},
            "confusion": {k: dict(v) for k, v in self.confusion.items()},
            "accuracy": self.accuracy,
            "macro_f1": self.macro_f1,
        }


def build_report(
    name: str, pairs: list[tuple[str, str]], labels: list[str]
) -> EvalReport:
    per_class = per_class_metrics(pairs, labels)
    return EvalReport(
        classifier=name,
        n=len(pairs),
        labels=list(labels),
        per_class=per_class,
        confusion=confusion_matrix(pairs),
        accuracy=accuracy(pairs),
        macro_f1=macro_f1(per_class),
    )
