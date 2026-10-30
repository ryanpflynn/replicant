"""MEM-D20 repo-scan verification (documented vs inferred, citation and pattern checks) and MEM-PR-D20 human-edit
diff summaries (bot commits ignored, caps, redaction)."""
import asyncio

from replicant_worker.activities import memory, pr_inspect, scan_verify
from replicant_worker.activities.scan_verify import Snapshot

CLAUDE = """# ledger-api conventions

- Money is integer minor units (`int64`); never use floating point for amounts.
- Rounding is half-even, through the helpers in `internal/ledger` (`Prorate`).
- Tests are table-driven (`t.Run` per case) and live next to the code.
"""
GO = {"internal/ledger/ledger.go": "// Package ledger posts entries.\npackage ledger\n\ntype Charge struct {\n\tAmountMinor int64\n}\n",
      "internal/ledger/prorate.go": "package ledger\n\n// Prorate returns a share.\nfunc Prorate() {}\n",
      "cmd/ledger-api/main.go": "// Command ledger-api serves.\npackage main\n",
      "internal/ledger/ledger_test.go": "package ledger\n\nfunc TestX(t *testing.T) { t.Run(\"a\", nil) }\n"}


def snap(extra=None, tree_extra=()):
    texts = {"CLAUDE.md": CLAUDE, **GO, **(extra or {})}
    return Snapshot("a" * 40, sorted(set(texts) | {"go.mod", *tree_extra}), texts)


def prop(stmt, files, pattern=None, applies=None, **kw):
    ev = {"files": files}
    if pattern:
        ev["pattern"] = pattern
    if applies:
        ev["applies_to"] = applies
    return {"memory_type": "rule", "statement": stmt, "proposed_scope": {"level": "repo"}, "evidence": ev, **kw}


def test_documented_in_instruction_files_is_dropped():
    kept, dropped = scan_verify.verify([prop("Represent money as int64 minor units and never use floating point for "
                                             "amounts.", ["internal/ledger/ledger.go"], r"AmountMinor\s+int64",
                                             ["**/*.go"])], snap())
    assert kept == [] and dropped[0]["documented_in"] == ["CLAUDE.md"]
    assert dropped[0]["reason"].startswith("already documented")


def test_documented_by_linter_config():
    s = snap({".golangci.yml": "linters:\n  enable:\n    - errcheck\n    - wrapcheck\n",
              ".editorconfig": "[*]\nindent_style = tab\n"})
    kept, dropped = scan_verify.verify([
        prop("Always check returned errors; errcheck must stay clean.", ["internal/ledger/ledger.go"], "err", ["**/*.go"]),
        prop("Indent Go code with tabs, not spaces.", ["internal/ledger/ledger.go"], "\t", ["**/*.go"]),
        prop("Format Go code with gofmt before committing.", ["internal/ledger/ledger.go"], "package", ["**/*.go"])],
        s)
    assert kept == []
    assert dropped[0]["documented_in"] == [".golangci.yml"]
    assert ".editorconfig" in dropped[1]["documented_in"]
    assert dropped[2]["documented_in"]  # gofmt: the Go formatter (go.mod)


def test_inferred_convention_verified_and_labelled():
    p = prop("Give every Go file a leading // doc comment naming the declaration.",
             ["internal/ledger/ledger.go", "cmd/ledger-api/main.go"], r"^// [A-Z][A-Za-z0-9-]* ", ["**/*.go"])
    kept, dropped = scan_verify.verify([p], snap())
    assert dropped == [] and len(kept) == 1
    v = kept[0]["verification"]
    assert (v["matched"], v["sampled"], v["missing"]) == (3, 4, []) and v["pattern"] == p["evidence"]["pattern"]
    assert kept[0]["label"] == "Inferred convention (seen in 3 of 4 files)"
    assert kept[0]["rationale"].startswith("Inferred convention (seen in 3 of 4 files)")
    assert kept[0]["documented_in"] == [] and "evidence" not in kept[0]


def test_citation_and_threshold_failures():
    kept, dropped = scan_verify.verify([
        prop("Round through rounding helpers in the shared module.", ["internal/ledger/rounding.go"], "func Round",
             ["**/*.go"]),
        prop("Wrap every returned error with the operation name.", [], "Errorf"),
        prop("Wrap every returned error with the operation name.", ["internal/ledger/ledger.go"], None),
        prop("Every Go file declares a struct type for its inputs.", ["internal/ledger/ledger.go"], r"^type \w+ struct",
             ["**/*.go"]),
        prop("Every Go file avoids panics in library code paths.", ["internal/ledger/ledger.go"], "(a+)+", ["**/*.go"]),
    ], snap())
    assert kept == []
    reasons = [d["reason"] for d in dropped]
    assert "cited files missing" in reasons[0] and dropped[0]["verification"]["missing"] == ["internal/ledger/rounding.go"]
    assert reasons[1] == "unverified: no cited files"
    assert reasons[2] == "unverified: no checkable pattern"
    assert reasons[3] == "unverified: pattern holds in 1 of 4 files"
    assert reasons[4] == "unverified: no checkable pattern"  # nested quantifier refused


def test_expect_absent_and_scope_defaults():
    p = prop("Never call float64 conversions in ledger code.", ["internal/ledger/ledger.go"], r"float64\(",
             None, proposed_scope={"level": "path", "path_globs": ["internal/ledger/**"]})
    p["evidence"]["expect"] = "absent"
    kept, _ = scan_verify.verify([p], snap())
    assert kept[0]["verification"]["sampled"] == 3 and kept[0]["verification"]["matched"] == 3  # .go under ledger/


def test_glob_match():
    assert scan_verify.glob_match("a/b/c.go", "**/*.go") and scan_verify.glob_match("c.go", "**/*.go")
    assert scan_verify.glob_match("internal/x/y.go", "internal/**") and not scan_verify.glob_match("cmd/y.go", "internal/**")
    assert scan_verify.glob_match("internal/ledger/a.go", "internal/ledger/*.go")


PATCH = """diff --git a/internal/ledger/ledger.go b/internal/ledger/ledger.go
index 1..2 100644
--- a/internal/ledger/ledger.go
+++ b/internal/ledger/ledger.go
@@ -1,3 +1,4 @@ package ledger
 package ledger
+// token: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
-var x = 1
+var x = 2
""" + "".join(f"""diff --git a/f{i}.go b/f{i}.go
new file mode 100644
--- /dev/null
+++ b/f{i}.go
@@ -0,0 +1,40 @@
""" + "".join(f"+line {j}\n" for j in range(40)) for i in range(25))


def test_summarize_patch_caps_and_redaction():
    files, truncated = pr_inspect.summarize_patch(PATCH)
    assert truncated and len(files) == pr_inspect.MAX_FILES
    f0 = files[0]
    assert (f0["path"], f0["status"], f0["additions"], f0["deletions"]) == ("internal/ledger/ledger.go", "modified", 2, 1)
    assert "ghp_" not in f0["hunks"][0] and "REDACTED" in f0["hunks"][0]
    assert files[1]["status"] == "added" and files[1]["hunks"][0].endswith("more lines]")
    assert len(files[1]["hunks"][0].splitlines()) == pr_inspect.MAX_HUNK_LINES + 1


def test_bot_commits():
    assert pr_inspect.is_bot_commit({"author_login": "replicant-bot"})
    assert pr_inspect.is_bot_commit({"author_email": "replicant-bot@replicant.localhost"})
    assert pr_inspect.is_bot_commit({"message": "fix\n\nReplicant-Activity: x"})
    assert not pr_inspect.is_bot_commit({"author_login": "dana", "message": "tidy"})


class FakeGitea:
    def __init__(self, commits, patches):
        self.commits, self.patches = commits, patches

    async def commit_patch(self, sha):
        return self.patches[sha]


def test_human_edit_items_ignore_bots_and_unlinked():
    commits = [{"sha": "b1", "is_bot": True, "author_login": "replicant-bot", "author_id": 9, "message": "bot"},
               {"sha": "h1", "is_bot": False, "author_login": "dana", "author_id": 2, "message": "Explain proration"},
               {"sha": "h2", "is_bot": False, "author_login": "eve", "author_id": 3, "message": "drive-by"}]
    g = FakeGitea(commits, {"h1": PATCH.split("diff --git a/f0.go")[0], "h2": ""})
    summ = asyncio.run(pr_inspect.Gitea.diff_summary(g, "base", "head", commits=[commits[1]]))  # type: ignore[arg-type]
    authors = {"2": {"id": 2, "login": "dana", "principal_id": "usr_" + "d" * 26, "repo_permission": "write"},
               "3": {"id": 3, "login": "eve", "linked": False}}
    items, non = memory.human_edit_items(commits, authors, {"2": summ}, job="job_x", repo="git.example/o/r",
                                         pr_number=4)
    assert len(items) == 1 and len(non) == 1 and non[0]["reason"] == "unlinked author"
    src = items[0]["source"]
    assert src["kind"] == "human_edit" and src["source_key"] == "edit:job_x:0" and src["commit_shas"] == ["h1"]
    assert src["author_principal_id"].startswith("usr_") and src["author_repo_permission"] == "write"
    assert "Explain proration" in items[0]["text"] and "internal/ledger/ledger.go" in items[0]["text"]
    assert "ghp_" not in items[0]["text"]
    assert len(memory.render_human_edit({"commits": [], "files": [{"path": "x", "hunks": ["y" * 9000]}]})) \
        <= memory.MAX_EDIT_TEXT


def test_fixture_scan_against_sample_repo():
    """The scripted scanner's proposals (scan.yaml) against dev/sample-repos/ledger-api with its real CLAUDE.md: the
    two restates are documented, the fx.go citation is missing, only the doc-comment convention is kept. Without
    CLAUDE.md at the scanned commit (the initial import) the restates are not documented."""
    import json
    import pathlib

    import yaml
    root = pathlib.Path(__file__).resolve().parents[3]
    repo = root / "dev" / "sample-repos" / "ledger-api"
    files = {str(p.relative_to(repo)): p.read_text() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    script = yaml.safe_load((root / "tests/fixtures/gateway-scripts/walkthrough-ledger-api/scan.yaml").read_text())
    cmd = script["turns"][2]["respond"]["tool_use"]["input"]["command"]
    props = json.loads(cmd.split("<<'JSON'\n")[1].split("\nJSON")[0])["proposals"]
    kept, dropped = scan_verify.verify(props, Snapshot("b" * 40, sorted(files), files))
    assert [k["subject_key"] for k in kept] == ["go.doc_comments"]
    assert [d.get("documented_in") for d in dropped] == [["CLAUDE.md"], ["CLAUDE.md"], None]
    assert dropped[2]["verification"]["missing"] == ["internal/ledger/fx.go"]
    no_md = {k: v for k, v in files.items() if k != "CLAUDE.md"}
    _, dropped = scan_verify.verify(props, Snapshot("b" * 40, sorted(no_md), no_md))
    assert not any(d.get("documented_in") for d in dropped)
