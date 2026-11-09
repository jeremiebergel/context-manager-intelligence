# Frozen pre-improvement selector; source SHA256: c58810ac71864413f0c03e4d992c9b6ea5108019ea5b8b15c4f7f2e63bb66324
"""Conservative lexical query preparation; no semantic/synonym claims."""
from __future__ import annotations

import re

# Question/function words only: domain nouns (rule, release, memory...) remain.
_STOPWORDS = frozenset("""
a an the and or of for to in on at by with from is are was were be been being
it its this that these those what which who where when why how do does did can
could should would will please tell show me about explain
le la les un une des du de d l et ou pour par dans sur avec en au aux est sont
était être ce cet cette ces quel quelle quels quelles qui que quoi où quand
comment pourquoi peut peux on il elle ils elles nous vous tu je mon ma mes
el los las una unos unas del y o para por con es son qué cuál cómo dónde
der die das ein eine einer eines und oder für von zu im ist sind was wie
""".split())
_TOKEN = re.compile(r"(?:/)?\w+(?:[./-]\w+)*(?:\+\+|#)?", re.UNICODE)


def lexical_terms(query: str) -> list[str]:
    """Keep literal content terms, stripping punctuation and question boilerplate.

    Requiring all remaining terms intentionally favors precision over paraphrase
    recall. Unknown content words cannot silently broaden into common-word hits.
    """
    if len(query) > 4096:
        return []
    terms = list(dict.fromkeys(t.casefold() for t in _TOKEN.findall(query)
                             if t.casefold() not in _STOPWORDS))
    return terms if len(terms) <= 32 else []


def identifier_filter(terms: list[str]):
    """SQLite predicate: punctuated identifiers cannot become space phrases."""
    identifiers = {t for t in terms if any(c in t for c in "._/-+#")}
    def matches(topic: str, text: str) -> bool:
        if not identifiers:
            return True
        return identifiers <= {t.casefold() for t in _TOKEN.findall(topic + "\n" + text)}
    return matches



def prior_search(self, project, query, *, limit=5, max_record_bytes=None):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("A project identifier is required")
    if not isinstance(query, str):
        raise ValueError("Search query must be a string")
    if "\x00" in query:
        raise ValueError("Search query must not contain NUL characters")
    # Frozen lexical helpers are defined above.
    terms = lexical_terms(query)
    if not terms:
        return []
    # All literal content terms must match. Never expand an unknown subject
    # into unrelated common-word results; paraphrases require an opt-in adapter.
    match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
    db = self._connect()
    try:
        db.create_function("cmi_identifiers", 2, identifier_filter(terms), deterministic=True)
        rows = db.execute("""SELECT e.* FROM memory_fts f JOIN events e ON e.id=f.id
            WHERE memory_fts MATCH ? AND e.project=?
            AND cmi_identifiers(e.topic, e.text)
            AND (? IS NULL OR length(CAST(e.text AS BLOB)) <= ?)
            AND e.revision=(SELECT MAX(n.revision) FROM events n WHERE n.project=e.project AND n.topic=e.topic)
            ORDER BY bm25(memory_fts), e.ts DESC LIMIT ?""",
            (match, project, max_record_bytes, max_record_bytes, limit)).fetchall()
        lexical = [dict(r) for r in rows]
        # Explicit opt-in only; identifiers never become approximate requests.
        if lexical or self.semantic is None or any(any(c in t for c in "._/-+#") for t in terms):
            return lexical
        text_bound = min(16384, max_record_bytes) if max_record_bytes is not None else 16384
        candidates = [dict(row) for row in db.execute("""SELECT e.* FROM events e
            WHERE e.project=? AND length(CAST(e.text AS BLOB)) <= ?
            AND e.revision=(SELECT MAX(n.revision) FROM events n
                WHERE n.project=e.project AND n.topic=e.topic)
            ORDER BY e.topic LIMIT ?""",
            (project, text_bound, self.semantic.max_records + 1)).fetchall()]
        try:
            return self.semantic.rank(query, candidates, limit=limit)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Optional local retrieval unavailable: %s", exc)
            return lexical
    finally:
        db.close()
