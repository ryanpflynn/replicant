"""MEM worker side: extraction parsing (generalization floor, unknown indexes), MEM-01 triage item selection
(addressed, /replicant no-memory, /replicant remember) and the injection staged into the run inputs."""
from replicant_worker.activities import memory

SRC = {"kind": "gate_comment", "source_key": "gate:job:dec", "text": "wrap errors"}


def test_parse_extraction_floor_and_shape():
    items = [{"text": "a", "source": SRC}, {"text": "b", "source": SRC}]
    reply = ('noise {"proposals": [{"source_index": 0, "memory_type": "rule", "scope": "path", "path_globs": ["x/**"],'
             ' "subject_key": "errors.wrap", "statement": "Wrap errors with fmt.Errorf and %w.", "generalization_score": 0.9},'
             ' {"source_index": 1, "memory_type": "rule", "statement": "Rename this variable now.", "generalization_score": 0.3},'
             ' {"source_index": 7, "memory_type": "rule", "statement": "out of range index", "generalization_score": 1}]}')
    props, non = memory.parse_extraction(reply, items, "git.example/o/r")
    assert len(props) == 1 and props[0]["proposed_scope"] == {"level": "path", "repo": "git.example/o/r",
                                                              "path_globs": ["x/**"]}
    assert props[0]["subject_key"] == "errors.wrap" and props[0]["source"] == SRC
    assert [n["decision"] for n in non] == ["not_generalizable"] and "0.30" in non[0]["reason"]
    assert memory.parse_extraction("not json", items, None) == ([], [
        {"source": SRC, "decision": "not_generalizable", "reason": "not proposed"}] * 2)


def test_triage_items_mem01():
    base = {"source_kind": "pr_review_comment", "author_principal_id": "usr_" + "a" * 26, "author_repo_permission": "write",
            "repo": "git.replicant.localhost:3300/acme-payments/ledger-api", "pr_number": 3, "repo_id": "1"}
    doc = {"items": [{**base, "comment_id": 1, "text": "fix", "classification": "actionable_fix"},
                     {**base, "comment_id": 2, "text": "why?", "classification": "question"},
                     {**base, "comment_id": 3, "text": "nit", "classification": "optional_suggestion",
                      "memory_directive": "remember"},
                     {**base, "comment_id": 4, "text": "fix2", "classification": "actionable_fix",
                      "memory_directive": "no_memory"},
                     {**base, "comment_id": 5, "text": "fix3", "classification": "actionable_fix", "addressed": False}]}
    out, non = memory._triage_items(doc, None)
    assert [o["source"]["comment_id"] for o in out] == [1, 3]
    assert out[0]["source"]["source_key"] == "gh-review-comment:1:1" and out[1]["remember"]
    assert {n["source"]["comment_id"]: n["decision"] for n in non} == {2: "not_generalizable", 4: "opted_out",
                                                                       5: "not_generalizable"}
    out, non = memory._triage_items(doc, False)  # top-level addressed: false (push not published)
    assert out == [] and len(non) == 5


def test_fix_retained_helpers():
    """MEM-D12: a repair's added lines must survive to the merged head (>= 80 % per file, file still present)."""
    from replicant_worker.activities.memory import _added_by_file, lines_retained
    patch = ("diff --git a/svc/fee.py b/svc/fee.py\n--- a/svc/fee.py\n+++ b/svc/fee.py\n@@ -1,2 +1,3 @@\n"
             "+from decimal import Decimal\n+    return Decimal(amount) * rate\n+\n x = 1\n"
             "diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n")
    added = _added_by_file(patch)
    assert added == {"svc/fee.py": ["from decimal import Decimal", "return Decimal(amount) * rate"]}
    kept = "from decimal import Decimal\ndef f(amount, rate):\n    return Decimal(amount) * rate\n"
    assert lines_retained(added, {"svc/fee.py": kept})
    assert not lines_retained(added, {"svc/fee.py": "def f(amount, rate):\n    return float(amount) * rate\n"})
    assert not lines_retained(added, {"svc/fee.py": None})
