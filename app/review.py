"""Pre-release impact review of a draft ruleset revision.

Before activating a draft revision, a publisher may ask: for this set of
already-recorded accountings, what would their conclusions become under
the *target* revision's immutable rules snapshot?

For every accepted assessment the stored normalized request is re-run
through :func:`app.solver.assess_normalized` against the target revision
rules, and the report gives, sorted by assessment id:

* the old (recorded) and new (re-evaluated) status,
* both ``summary_hash`` values,
* the adopted branches on both sides (``None`` on a side without a
  release conclusion),
* the obligation difference (artifact-level and per-component).

The summary counts every transition and, in particular, lists the
assessments that would flip from a release (``ok``) to ``conflict`` or
to an error status (``invalid-expression`` / ``missing-rules`` /
``dependency-cycle``).

The report is a pure, deterministic function of the rows loaded inside
one read transaction and the target rules snapshot: every id list is
sorted, so request ordering changes neither the report nor
``report_hash`` (the SHA-256 of the canonical report without the hash
field itself).  Nothing is persisted -- reviewing never publishes the
draft and never rewrites an assessment.
"""

from .common import stable_hash
from .solver import assess_normalized

# Non-ok statuses that are not a licensing conflict but a hard evaluation
# error (expression parsing, missing rules, cyclic dependency graph).
ERROR_STATUSES = ("invalid-expression", "missing-rules", "dependency-cycle")


# ---------------------------------------------------------------------------
# result projections
# ---------------------------------------------------------------------------

def _obligation_view(result):
    """Project a result onto (artifact obligations, per-component obligations).

    An ``ok`` result carries artifact-level obligations and a full
    component report.  A non-ok result has no release conclusion, hence
    no artifact-level obligations; the only obligations that still hold
    are those attached to its confirmed subgraph.
    """
    if result.get("status") == "ok":
        artifact = set(result.get("artifact_obligations", []))
        components = {
            c["id"]: set(c.get("obligations", []))
            for c in result.get("components", [])
        }
        return artifact, components
    components = {
        c["id"]: set(c.get("obligations", []))
        for c in result.get("confirmed_subgraph", {}).get("components", [])
    }
    return set(), components


def _adopted(result):
    """Adopted branches; only an ``ok`` result has a release conclusion."""
    return result.get("adopted") if result.get("status") == "ok" else None


def _side(result):
    return {
        "status": result.get("status"),
        "release": bool(result.get("release")),
        "summary_hash": result.get("summary_hash"),
        "adopted": _adopted(result),
    }


def _obligation_diff(old_result, new_result):
    """Added/removed obligations at artifact level and per component."""
    old_art, old_comps = _obligation_view(old_result)
    new_art, new_comps = _obligation_view(new_result)
    components = []
    for cid in sorted(old_comps.keys() | new_comps.keys()):
        before = old_comps.get(cid, set())
        after = new_comps.get(cid, set())
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            components.append(
                {"component": cid, "added": added, "removed": removed}
            )
    return {
        "artifact": {
            "added": sorted(new_art - old_art),
            "removed": sorted(old_art - new_art),
            "unchanged_count": len(old_art & new_art),
        },
        "components": components,
    }


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def review_impacts(ruleset_name, target_revision, active_revision, target_rules, rows):
    """Re-evaluate persisted assessment rows against a target revision.

    ``rows`` is a list of ``{"id", "request", "result"}`` mappings loaded
    from one consistent store snapshot; ``target_rules`` is the immutable
    normalized rules snapshot of the draft under review.  Returns the
    complete report (items sorted by assessment id) with a stable
    ``report_hash``.
    """
    items = []
    for row in sorted(rows, key=lambda r: r["id"]):
        old_result = row["result"]
        new_result = assess_normalized(row["request"], target_rules)
        items.append(
            {
                "assessment_id": row["id"],
                "changed": old_result.get("summary_hash")
                != new_result.get("summary_hash"),
                "old": _side(old_result),
                "new": _side(new_result),
                "obligation_diff": _obligation_diff(old_result, new_result),
            }
        )

    transitions = {}
    to_conflict, to_error = [], []
    for item in items:
        aid = item["assessment_id"]
        old_status, new_status = item["old"]["status"], item["new"]["status"]
        transitions.setdefault((old_status, new_status), []).append(aid)
        if item["old"]["release"]:
            if new_status == "conflict":
                to_conflict.append(aid)
            elif new_status in ERROR_STATUSES:
                to_error.append(aid)

    status_transitions = [
        {
            "from_status": old_status,
            "to_status": new_status,
            "count": len(ids),
            "assessment_ids": ids,
        }
        for (old_status, new_status), ids in sorted(transitions.items())
    ]
    changed = sum(1 for item in items if item["changed"])
    regressions = sorted(set(to_conflict) | set(to_error))
    report = {
        "ruleset": ruleset_name,
        "active_revision": active_revision,
        "target_revision": target_revision,
        "assessment_count": len(items),
        "items": items,
        "summary": {
            "total": len(items),
            "changed": changed,
            "unchanged": len(items) - changed,
            "status_transitions": status_transitions,
            "release_regressions": {
                "total": len(regressions),
                "to_conflict": sorted(to_conflict),
                "to_error": sorted(to_error),
                "assessment_ids": regressions,
            },
        },
    }
    report["report_hash"] = stable_hash(report)
    return report
