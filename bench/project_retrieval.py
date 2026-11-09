"""Reproducible source-identity retrieval evaluation, not an answer benchmark.

Run from repo root with CMI_STANDALONE=1; optional --model-dir enables a
locally provisioned encoder. No network, generated answers, or fixture synonyms.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import importlib.metadata
import platform
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmi.project_memory import ProjectMemory

# Frozen relevance judgments made from these source records, independent of scores.
FACTS = [
    ("release.scope", "La règle de publication impose une innovation complète, jamais une alpha partielle."),
    ("storage.backend", "Project memories are stored in SQLite with WAL transactions."),
    ("billing.mode", "Customers pay through Stripe Payment Links."),
    ("backup.schedule", "Encrypted backups run every night at 02:00 UTC and are kept for thirty days."),
    ("auth.requirement", "Administrators must use hardware security keys for two-factor authentication."),
    ("license.code", "The source code is distributed under the Apache-2.0 license."),
    ("locale.default", "El idioma predeterminado de la interfaz es español."),
    ("support.channel", "Customer support tickets go to the dedicated email inbox."),
    ("deploy.region", "Production services run in the Frankfurt data center."),
    ("retention.logs", "Application logs are deleted after seven days."),
    ("network.egress", "Outbound connections are blocked except to the payment gateway."),
    ("build.command", "Run python -m pytest before merging source changes."),
]
# query, relevant topic or None, class. Queries cover more than the initial probe.
QUERIES = [
    *[(topic, topic, "identifier") for topic, _ in FACTS],
    ("Quelle règle pour la publication ?", "release.scope", "literal_question"),
    ("What stores project memories?", "storage.backend", "literal_question"),
    ("How do customers pay?", "billing.mode", "literal_question"),
    ("When do encrypted backups run?", "backup.schedule", "literal_question"),
    ("What authentication must administrators use?", "auth.requirement", "literal_question"),
    ("What license covers the source code?", "license.code", "literal_question"),
    ("Cuál es el idioma predeterminado?", "locale.default", "literal_question"),
    ("Where do customer support tickets go?", "support.channel", "literal_question"),
    ("Where do production services run?", "deploy.region", "literal_question"),
    ("When are application logs deleted?", "retention.logs", "literal_question"),
    ("Are outbound connections blocked?", "network.egress", "literal_question"),
    ("What should run before merging source changes?", "build.command", "literal_question"),
    ("Peut-on sortir une mouture incomplète ?", "release.scope", "paraphrase"),
    ("Quel moteur sert à stocker les souvenirs ?", "storage.backend", "paraphrase"),
    ("How do customers send us money?", "billing.mode", "paraphrase"),
    ("How frequently are recovery copies created?", "backup.schedule", "paraphrase"),
    ("What extra protection is required for privileged accounts?", "auth.requirement", "paraphrase"),
    ("What legal terms permit reusing the software?", "license.code", "paraphrase"),
    ("How can clients get help with a problem?", "support.channel", "paraphrase"),
    ("Where is the live system hosted?", "deploy.region", "paraphrase"),
    ("How long do we keep diagnostic records?", "retention.logs", "paraphrase"),
    ("Can the server reach arbitrary external websites?", "network.egress", "paraphrase"),
    ("Which verification must pass before integrating a patch?", "build.command", "paraphrase"),
    ("Can we ship an incomplete early version?", "release.scope", "multilingual"),
    ("Où sont conservées les données de mémoire du projet ?", "storage.backend", "multilingual"),
    ("Comment les clients effectuent-ils leurs paiements ?", "billing.mode", "multilingual"),
    ("Wann werden die Sicherungskopien erstellt?", "backup.schedule", "multilingual"),
    ("What language does the interface use initially?", "locale.default", "multilingual"),
    ("¿Dónde se ejecutan los servicios de producción?", "deploy.region", "multilingual"),
    ("What is the CEO's phone number?", None, "absence"),
    ("Quelle règle pour les volcans ?", None, "absence"),
    ("How are volcano eruptions predicted?", None, "absence"),
    ("What is the PayPal merchant account identifier?", None, "absence"),
    ("What port does the PostgreSQL replication service listen on?", None, "absence"),
    ("release.scopes", None, "absence"),
    ("Quelle est la pour la ?", None, "absence"),
    ("How do we handle refunds?", None, "absence"),
]


def legacy_search(memory, project, query, limit=3):
    """Frozen pre-change algorithm: OR of quoted whitespace terms, original SQL.

    Latest revision/project constraints already existed in the baseline.
    """
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    db = memory._connect()
    try:
        return [dict(row) for row in db.execute('''SELECT e.*
            FROM memory_fts f JOIN events e ON e.id=f.id
            WHERE memory_fts MATCH ? AND e.project=?
            AND e.revision=(SELECT MAX(n.revision) FROM events n
                WHERE n.project=e.project AND n.topic=e.topic)
            ORDER BY bm25(memory_fts), e.ts DESC LIMIT ?''', (match, project, limit))]
    finally:
        db.close()


def populate(memory):
    receipts = {}
    for topic, text in FACTS:
        receipts[topic] = memory.publish("evaluation", topic, text, source=f"fixture:{topic}")
    for n in range(200):
        memory.publish("evaluation", f"ui.{n}",
            "La règle pour la couleur de la barre est bleue. The interface shows the current view and is in the sidebar.",
            source=f"distractor:ui:{n}")
        memory.publish("foreign", f"fact.{n}",
            "SQLite WAL Stripe publication backups license authentication production source changes " * 2,
            source=f"distractor:foreign:{n}")
    for n in range(25):
        memory.publish("evaluation", f"literal.{n}", "release scope release scope release scope", source=f"distractor:literal:{n}")
        memory.publish("evaluation", f"old.{n}", "SQLite WAL Stripe Payment Links", source=f"distractor:obsolete:{n}")
        memory.publish("evaluation", f"old.{n}", "Superseded layout preference: gray icons.", source=f"distractor:replacement:{n}", expected_revision=1)
    return receipts


def summarize(rows):
    relevant = sum(r["relevant_returned"] for r in rows)
    returned = sum(len(r["returned"]) for r in rows)
    expected = sum(len(r["expected_ids"]) for r in rows)
    latency = sorted(r["latency_ms"] for r in rows)
    absent = [r for r in rows if not r["expected_ids"]]
    return dict(queries=len(rows), returned=returned, relevant_returned=relevant,
        precision=relevant / returned if returned else None,
        recall=relevant / expected if expected else None,
        irrelevant_text_bytes=sum(r["irrelevant_text_bytes"] for r in rows),
        correct_abstentions=sum(not r["returned"] for r in absent), absence_queries=len(absent),
        latency_median_ms=statistics.median(latency), latency_p95_ms=latency[min(len(latency)-1, int(len(latency)*0.95))],
        latency_max_ms=max(latency))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--model-dir")
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--encoder", choices=("minilm", "e5"), default="minilm")
    parser.add_argument("--scratch-dir", default=str(Path.home() / ".hermes/cache/scratch"))
    args = parser.parse_args()
    scratch = Path(args.scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cmi-retrieval-bench-", dir=scratch) as temp:
        memory = ProjectMemory(Path(temp) / "memory.db")
        receipts = populate(memory)
        db = memory._connect()
        try:
            corpus_rows = [dict(r) for r in db.execute("SELECT project,topic,revision,text,source FROM events ORDER BY project,topic,revision")]
        finally:
            db.close()
        latest = {(r["project"], r["topic"]): r["revision"] for r in corpus_rows}
        counts = dict(target_topics=len(receipts),
            current_project_rows=sum(r["project"] == "evaluation" and r["revision"] == latest[(r["project"], r["topic"])] for r in corpus_rows),
            foreign_rows=sum(r["project"] != "evaluation" for r in corpus_rows),
            obsolete_rows=sum(r["revision"] != latest[(r["project"], r["topic"])] for r in corpus_rows),
            all_events=len(corpus_rows))
        assert counts == dict(target_topics=12, current_project_rows=262, foreign_rows=200, obsolete_rows=25, all_events=487)
        methods = {"baseline_or": lambda q: legacy_search(memory, "evaluation", q),
                   "candidate_lexical": lambda q: memory.search("evaluation", q, limit=3)}
        semantic = None
        if args.model_dir:
            from cmi.project_retrieval import LocalSemanticRetriever, LocalQueryDocumentRetriever
            started = time.perf_counter()
            cls = LocalQueryDocumentRetriever if args.encoder == "e5" else LocalSemanticRetriever
            adapter = cls(args.model_dir, **({"threshold": args.threshold} if args.threshold is not None else {}))
            semantic = {"threshold": adapter.threshold, "model_load_ms": (time.perf_counter()-started)*1000,
                        "model_sha256": adapter.model_sha256, "tokenizer_sha256": adapter.tokenizer_sha256,
                        "model": adapter.model_name, "revision": adapter.model_revision,
                        "runtime_versions": {p: importlib.metadata.version(p) for p in ("numpy", "onnxruntime", "tokenizers")}}
            enriched = ProjectMemory(Path(temp) / "memory.db", semantic=adapter)
            methods["candidate_local_fallback"] = lambda q: enriched.search("evaluation", q, limit=3)
        results = {}
        for method, search in methods.items():
            observations = []
            for query, topic, category in QUERIES:
                expected = {receipts[topic]["id"]} if topic else set()
                started = time.perf_counter()
                hits = search(query)
                assert all(r["project"] == "evaluation" and r["revision"] == latest[(r["project"], r["topic"])] for r in hits)
                elapsed = (time.perf_counter() - started) * 1000
                observations.append(dict(query=query, category=category, expected_ids=sorted(expected),
                    returned=[{k: r[k] for k in ("id", "project", "topic", "revision", "source", "text")}
                              | ({"retrieval": r["retrieval"]} if "retrieval" in r else {}) for r in hits],
                    relevant_returned=sum(r["id"] in expected for r in hits),
                    irrelevant_text_bytes=sum(len(r["text"].encode()) for r in hits if r["id"] not in expected),
                    latency_ms=elapsed))
            results[method] = {"overall": summarize(observations),
                "categories": {c: summarize([r for r in observations if r["category"] == c]) for c in sorted({r["category"] for r in observations})},
                "observations": observations}
        report = dict(format=1, scope="synthetic direct retrieval; not real sessions or generative-answer evaluation",
            corpus_sha256=hashlib.sha256(json.dumps(corpus_rows, ensure_ascii=False).encode()).hexdigest(),
            query_sha256=hashlib.sha256(json.dumps(QUERIES, ensure_ascii=False).encode()).hexdigest(),
            corpus=counts, platform=dict(python=platform.python_version(), system=platform.system(), machine=platform.machine()),
            limit=3, semantic=semantic, results=results)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v["overall"] for k, v in results.items()}, indent=2))


if __name__ == "__main__":
    main()
