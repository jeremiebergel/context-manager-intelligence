"""Adversarial retrieval contracts; return stored identities, never answers."""
from cmi.project_memory import ProjectMemory


def test_common_question_words_do_not_retrieve_ui_noise(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    for n in range(200):
        memory.publish("p", f"ui.{n}", "La règle pour la couleur de la barre est bleue.", source=f"ui:{n}")
    target = memory.publish("p", "release.scope", "La règle de publication impose une innovation complète.", source="user:release")
    assert [r["id"] for r in memory.search("p", "Quelle règle pour la publication ?")] == [target["id"]]
    assert memory.search("p", "quelle est la pour la ?") == []


def test_compound_identifiers_are_literal_before_limit(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    target = memory.publish("p", "release.scope", "Complete innovation required.", source="user:literal")
    for n in range(25):
        memory.publish("p", f"noise.{n}", "release scope release scope release scope", source=f"ui:{n}")
    assert [r["id"] for r in memory.search("p", "release.scope", limit=1)] == [target["id"]]
    assert memory.search("p", "noise_12") == []


def test_adversarial_project_and_obsolete_filtering_precedes_limits(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    for n in range(200):
        memory.publish("other", f"foreign.{n}", "SQLite WAL SQLite WAL", source=f"foreign:{n}")
        memory.publish("p", f"obsolete.{n}", "SQLite WAL SQLite WAL", source=f"old:{n}")
        memory.publish("p", f"obsolete.{n}", "Now uses direct files.", source=f"new:{n}", expected_revision=1)
    target = memory.publish("p", "storage", "SQLite WAL", source="user:storage")
    assert [r["id"] for r in memory.search("p", "SQLite WAL", limit=1)] == [target["id"]]
    assert len(memory.history("p", "obsolete.0")) == 2


def test_multibyte_context_is_bounded_and_skips_oversize_claims(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    for n in range(25):
        memory.publish("p", f"huge.{n}", "publication " + "é界" * 2000, source=f"large:{n}")
    target = memory.publish("p", "release", "publication complète", source="user:release")
    context = memory.context("p", "publication", max_bytes=400)
    assert target["id"] in context
    assert "large:" not in context
    assert len(context.encode("utf-8")) <= 400
    assert context == memory.context("p", "publication", max_bytes=400)
    assert memory.context("p", "publication", max_bytes=5) == ""


def test_actual_local_embedding_ranks_paraphrase_without_api():
    import os
    import pytest
    from cmi import project_retrieval
    model_dir = os.environ.get("CMI_TEST_RETRIEVAL_MODEL")
    if not model_dir:
        pytest.skip("optional local model not supplied; no downloads in tests")
    assert hasattr(project_retrieval, "LocalSemanticRetriever"), "local adapter not implemented"
    # Ranking contract is independent of default safety threshold; this is not
    # a claim that the default threshold recalls this phrase.
    adapter = project_retrieval.LocalSemanticRetriever(model_dir, threshold=0.0)
    rows = [
        dict(id="billing", project="p", revision=1, topic="billing", text="Customers pay through Stripe Payment Links.", source="user:billing"),
        dict(id="ui", project="p", revision=1, topic="ui", text="The sidebar uses blue rounded icons.", source="user:ui"),
    ]
    hits = adapter.rank("How do customers send us money?", rows, limit=1)
    assert [r["id"] for r in hits] == ["billing"]
    assert hits[0]["source"] == "user:billing"
    assert hits[0]["text"] == rows[0]["text"]
    assert 0.0 <= hits[0]["retrieval"]["score"] <= 1.000001
    conservative = project_retrieval.LocalSemanticRetriever(model_dir)
    assert conservative.rank("How are volcano eruptions predicted?", rows, limit=1) == []


def test_opt_in_semantic_path_receives_only_current_project_rows(tmp_path):
    class RecordingAdapter:
        max_records = 4096
        def __init__(self):
            self.calls = []
        def rank(self, query, rows, *, limit):
            self.calls.append(rows)
            return rows[:limit]
    adapter = RecordingAdapter()
    memory = ProjectMemory(tmp_path / "memory.db", semantic=adapter)
    memory.publish("foreign", "storage", "Foreign secret", source="private:foreign")
    memory.publish("p", "storage", "Old storage", source="user:old")
    current = memory.publish("p", "storage", "New storage", source="user:new", expected_revision=1)
    assert [r["id"] for r in memory.search("p", "Where are recollections kept?", limit=1)] == [current["id"]]
    assert [[r["id"] for r in rows] for rows in adapter.calls] == [[current["id"]]]
    memory.search("p", "missing.identifier")
    memory.search("p", "what is the ?")
    assert len(adapter.calls) == 1
    assert memory.search("p", "New storage")[0]["id"] == current["id"]
    assert len(adapter.calls) == 1


def test_semantic_runtime_failure_keeps_safe_lexical_fallback(tmp_path, caplog):
    class BrokenAdapter:
        max_records = 4096
        def rank(self, *args, **kwargs):
            raise RuntimeError("local encoder failed")
    memory = ProjectMemory(tmp_path / "memory.db", semantic=BrokenAdapter())
    row = memory.publish("p", "storage", "SQLite WAL", source="user:storage")
    assert memory.search("p", "SQLite WAL")[0]["id"] == row["id"]
    assert memory.search("p", "memory backend engine") == []
    assert "local encoder failed" in caplog.text


def test_unpinned_local_model_is_rejected_before_optional_imports(tmp_path):
    import pytest
    from cmi.project_retrieval import LocalSemanticRetriever
    (tmp_path / "model.onnx").write_bytes(b"not the pinned public encoder")
    (tmp_path / "tokenizer.json").write_text("{}")
    with pytest.raises(ValueError, match="pinned"):
        LocalSemanticRetriever(tmp_path)


def test_query_bounds_abstain_instead_of_dropping_distinctive_tail(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    words = [f"marker{n}" for n in range(32)]
    memory.publish("p", "many", " ".join(words), source="user:many")
    assert memory.search("p", " ".join(words + ["unknownlastterm"])) == []
    memory.publish("p", "simple", "marker", source="user:simple")
    assert memory.search("p", "marker " + " " * 5000 + "unknownlastterm") == []
    assert memory.search("p", "marker " * 700) == []
    from cmi.project_retrieval import lexical_terms
    assert lexical_terms(" ".join(words + ["term33"])) == []


def test_language_identifiers_keep_symbol_suffixes(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    memory.publish("p", "csharp", "Compiler language is C#", source="user:csharp")
    cpp = memory.publish("p", "cpp", "Compiler language is C++", source="user:cpp")
    assert [r["id"] for r in memory.search("p", "C++")] == [cpp["id"]]


def test_leading_underscores_and_absolute_paths_are_not_normalized(tmp_path):
    memory = ProjectMemory(tmp_path / "memory.db")
    memory.publish("p", "normal", "cmi_result_12 tmp/private.env", source="user:normal")
    target = memory.publish("p", "private", "_cmi_result_12 /tmp/private.env", source="user:private")
    assert [r["id"] for r in memory.search("p", "_cmi_result_12")] == [target["id"]]
    assert [r["id"] for r in memory.search("p", "/tmp/private.env")] == [target["id"]]
    assert memory.search("p", "__cmi_result_12") == []

