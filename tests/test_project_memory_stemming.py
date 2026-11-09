"""Plural / longer-form matching for plain words (live finding 2026-11-06: torrent vs torrents)."""
from cmi.project_memory import ProjectMemory, _fts_term


def test_fts_term_rules():
    assert _fts_term("torrents") == '"torrent"*'
    assert _fts_term("sqlite") == '"sqlite"*'
    assert _fts_term("log") == '"log"' and _fts_term("sqlite3") == '"sqlite3"'
    assert _fts_term("status") == '"status"*' and _fts_term("seed_board") == '"seed_board"'


def test_plural_and_longer_forms_match_but_unrelated_words_do_not(tmp_path):
    m = ProjectMemory(tmp_path / "m.db")
    m.publish("p", "net/engine", "Native torrent engine on Android, sqlite3 store", source="user:x", model="m")
    for q in ("torrents engine", "torrent", "sqlite", "engines android"):
        assert m.search("p", q), q
    assert not m.search("p", "torrential")
    assert not m.search("p", "payment")
    assert not m.search("other", "torrents")


def test_publish_tags_make_synonyms_searchable_without_changing_the_claim(tmp_path):
    import pytest
    m = ProjectMemory(tmp_path / "t.db")
    ev = m.publish("p", "decision/volet2", "No purchase of extra credits, subscriptions only", source="user:x",
                   model="m", tags="budget, financement, funding, abonnement")
    assert ev["text"] == "No purchase of extra credits, subscriptions only"
    for q in ("financement", "budget funding", "abonnements"):
        assert m.search("p", q), q
    assert not m.search("p", "salaire")
    assert not m.search("other", "financement")
    with pytest.raises(ValueError):
        m.publish("p", "t2", "x", source="s", tags="x" * 301)


def test_exact_user_quote_is_searchable(tmp_path):
    m = ProjectMemory(tmp_path / "q.db")
    m.publish("p", "decision/x", "No extra credit purchase", source="s", model="m",
              quote="Le financement du volet 2 reste sur mes abonnements")
    assert m.search("p", "financement volet")
    assert not m.search("other", "financement volet")


def test_cues_find_paraphrases_without_relaxing_the_and_rule(tmp_path):
    import pytest
    m = ProjectMemory(tmp_path / "c.db")
    m.publish("p", "decision/x", "No purchase of extra credits", source="s", model="m")
    m.publish("p", "decision/y", "Database is SQLite", source="s", model="m")
    assert not m.search("p", "financement budget")                      # strict AND: no hit
    hits = m.search("p", "financement budget", cues=["purchase credits", "payment"])
    assert [h["topic"] for h in hits] == ["decision/x"]                 # a cue matched, nothing else leaks
    assert not m.search("p", "financement", cues=["salaire"])
    assert not m.search("other", "x", cues=["purchase credits"])
    with pytest.raises(ValueError):
        m.search("p", "x", cues=["a"] * 6)


def test_approximate_stage_is_labelled_tool_only_and_does_not_invent(tmp_path):
    m = ProjectMemory(tmp_path / "a.db")
    m.publish("p", "status/loading", "The game starts loading but never reaches gameplay", source="s", model="m")
    m.publish("p", "infra/server", "Host the server on fly.io", source="s", model="m")
    m.publish("p", "goal/scope", "Run it on the owner's own iPhone only", source="s", model="m")
    assert m.search("p", "where is the game at now") == []                       # strict: 'now' is absent
    hit = m.search("p", "where is the game at now", approximate=True)
    assert hit and hit[0]["topic"] == "status/loading" and hit[0]["match"] == "approximate"
    assert 0 < hit[0]["coverage"] <= 1 and "game" in hit[0]["matched_words"]
    assert m.context("p", "where is the game at now") == ""                      # automatic context stays strict
    for q in ("recette tarte pommes", "salaire mensuel équipe", "game weather tomorrow Paris", "server.py bug"):
        assert m.search("p", q, approximate=True) == [], q                       # unknown words, identifiers: nothing
    assert m.search("other", "where is the game at now", approximate=True) == []  # project isolation
    assert [h.get("match") for h in m.search("p", "game loading", approximate=True)] == [None]  # strict hit wins
