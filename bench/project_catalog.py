"""Dependency-free agent-in-loop catalogue driver; never invokes any model.

prepare RUN_DIR: frozen source fixtures + disjoint question/judge manifests.
serve RUN_DIR: JSONL requests {case_id,name,arguments}, actual scoped native
read-tool results on stdout. Parent feeds ONLY questions.json, schemas.json,
and these results to the active model; judge-private.json never enters prompts.
score RUN_DIR ANSWERS_JSON: {case_id,answer,cited_ids,abstained} submissions.
Checks source identity/opening only; answer entailment needs parent review.
verify RUN_DIR: exhaustive structural navigation, NOT model quality evaluation.
Use a fresh directory under the Hermes scratch directory, never a live home.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmi.project_bridge import ProjectBridge
from bench.project_retrieval import FACTS, QUERIES, populate

ORIGINAL_SHA = "8ceea716ce17ff3e73228c0a062e4756ffbe26f909fb17831a3e489ee4547ec2"
HELDOUT_SHA = "81337fbc36f99f250eb3eb9f2b09e62fbd4939858a42a57cae5219c4d3609dc7"
READ_TOOLS = {"cmi_project_topics", "cmi_project_search", "cmi_project_get"}
MAX_CALLS = 12
MAX_RESULT_BYTES = 45000


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class EvidenceFixture:
    """Attach exact frozen source text to existing populate(), not new answers."""
    def __init__(self, memory):
        self.memory = memory

    def publish(self, project, topic, text, **kwargs):
        return self.memory.publish(project, topic, text, evidence=text, **kwargs)


def prepare(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    original = dict(facts=FACTS, queries=QUERIES)
    if hashlib.sha256(encoded(original)).hexdigest() != ORIGINAL_SHA:
        raise ValueError("Original fixture changed; freeze and review independently")
    heldout_path = Path(__file__).with_name("retrieval_holdout.json")
    if hashlib.sha256(heldout_path.read_bytes()).hexdigest() != HELDOUT_SHA:
        raise ValueError("Heldout fixture changed")
    heldout = load(heldout_path)
    home = directory / "hermes-home"
    bridge = ProjectBridge(home, "evaluation", "catalog-fixture")
    original_receipts = populate(EvidenceFixture(bridge.memory))
    heldout_receipts = {}
    for topic, text in heldout["facts"]:
        bridge.memory.publish("harbour", topic, "OBSOLETE: " + text,
                              source="fixture:obsolete:" + topic, evidence="OBSOLETE: " + text)
        heldout_receipts[topic] = bridge.memory.publish("harbour", topic, text,
            source="fixture:current:" + topic, expected_revision=1, evidence=text)
        bridge.memory.publish("foreign-harbour", topic, text,
                              source="fixture:foreign:" + topic, evidence=text)
    for i in range(20):
        text = "The interface shows the current view and is in the sidebar."
        bridge.memory.publish("harbour", f"ui.{i}", text, source=f"fixture:noise:{i}", evidence=text)
    public, private = [], []
    cases = [("original", q, topic, category, "evaluation", original_receipts)
             for q, topic, category in QUERIES if category != "identifier"]
    cases += [("heldout", q, topic, category, "harbour", heldout_receipts)
              for q, topic, category, split in heldout["queries"] if split == "holdout"]
    for index, (corpus, question, topic, category, project, receipts) in enumerate(cases):
        case_id = f"c{index:04d}"
        public.append(dict(case_id=case_id, question=question))
        record = bridge.memory.get(project, receipts[topic]["id"]) if topic else None
        private.append(dict(case_id=case_id, corpus=corpus, category=category, project=project,
            expected_id=record["id"] if record else None,
            expected_topic=topic, evidence_sha256=record["evidence_sha256"] if record else None))
    dump(directory / "questions.json", dict(cases=public))
    dump(directory / "judge-private.json", dict(cases=private))
    dump(directory / "schemas.json", [s for s in bridge.schemas() if s["name"] in READ_TOOLS])
    dump(directory / "fixture-original-frozen.json", original)
    dump(directory / "fixture-heldout-frozen.json", heldout)
    repository = Path(__file__).resolve().parents[1]
    implementation_sha256 = {name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in ("cmi/project_memory.py", "cmi/project_bridge.py", "bench/project_catalog.py")}
    manifest = dict(format=2, model_calls=0, cases=len(cases), implementation_sha256=implementation_sha256,
        host_request_capture="questions.json original user text captured on every fresh bridge; no judge labels",
        original_cases=sum(c["corpus"] == "original" for c in private),
        heldout_cases=sum(c["corpus"] == "heldout" for c in private),
        original_sha256=ORIGINAL_SHA, heldout_sha256=HELDOUT_SHA,
        questions_sha256=hashlib.sha256(encoded(dict(cases=public))).hexdigest(),
        native_schemas_sha256=hashlib.sha256(encoded(bridge.schemas())).hexdigest(),
        tools=sorted(READ_TOOLS), max_calls_per_case=MAX_CALLS,
        max_tool_result_bytes_per_case=MAX_RESULT_BYTES, catalogue_response_byte_cap=3000,
        catalogue_max_entries=10, token_bound="not measured; provider tokenizer required",
        prompt_policy="Only questions.json, schemas.json and actual tool results; no judge/fixtures/expected IDs.",
        scoring_scope="Source navigation and citation identity, NOT generated answer entailment or real-world accuracy.")
    dump(directory / "manifest.json", manifest)
    return manifest


def traces(directory):
    path = Path(directory) / "tool-trace.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def tool_step(directory, request):
    directory = Path(directory)
    if not isinstance(request, dict) or set(request) != {"case_id", "name", "arguments"}:
        raise ValueError("Request requires case_id, name and arguments only")
    cases = {c["case_id"]: c for c in load(directory / "judge-private.json")["cases"]}
    if request["case_id"] not in cases:
        raise ValueError("Unknown case_id")
    if not isinstance(request["arguments"], dict):
        raise ValueError("Tool arguments must be an object")
    case = cases[request["case_id"]]
    history = [r for r in traces(directory) if r["case_id"] == case["case_id"]]
    remaining = MAX_RESULT_BYTES - sum(r["result_bytes"] for r in history)
    if len(history) >= MAX_CALLS or remaining <= 0:
        raise ValueError("Case budget exhausted")
    start = time.perf_counter()
    if request["name"] not in READ_TOOLS:
        result = dict(error="Only read-only project tools are permitted")
    else:
        bridge = ProjectBridge(directory / "hermes-home", case["project"], "catalog-agent")
        # Fresh bridges must see the same original user text as production capture.
        # Read it ONLY from the public manifest, never judge labels or tool queries.
        questions = {c["case_id"]: c["question"] for c in load(directory / "questions.json")["cases"]}
        bridge.capture([dict(role="user", content=questions[case["case_id"]])])
        result = bridge.handle(request["name"], request["arguments"])
    size = len(encoded(result))
    if size > remaining:
        result = dict(error="Tool result exceeds remaining case byte budget; abstain")
        size = len(encoded(result))
        if size > remaining:
            raise ValueError("Case byte budget exhausted")
    response = dict(case_id=case["case_id"], result=result, result_bytes=size)
    row = dict(response, name=request["name"], arguments=request["arguments"],
               latency_ms=(time.perf_counter() - start) * 1000)
    with (directory / "tool-trace.jsonl").open("a", encoding="utf-8") as file:
        file.write(encoded(row).decode("utf-8") + "\n")
    return response


def evidence_opened(history, event_id, evidence_hash):
    """Require exact complete paginated source, not merely a get without evidence."""
    pages = {}
    for row in history:
        result, args = row["result"], row["arguments"]
        if (row["name"] == "cmi_project_get" and args.get("id") == event_id
                and args.get("include_evidence") is True and result.get("current") is True
                and result.get("evidence_sha256") == evidence_hash and "evidence" in result):
            pages[args.get("offset", 0)] = result
    offset, parts = 0, []
    while offset in pages:
        page = pages[offset]
        parts.append(page["evidence"])
        next_offset = page.get("next_offset")
        if next_offset is None:
            return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest() == evidence_hash
        if not isinstance(next_offset, int) or next_offset <= offset:
            return False
        offset = next_offset
    return False


def score(directory, answers):
    directory = Path(directory)
    private = {c["case_id"]: c for c in load(directory / "judge-private.json")["cases"]}
    history, observations, seen = traces(directory), [], set()
    for answer in answers:
        case_id = answer["case_id"]
        if case_id not in private or case_id in seen:
            raise ValueError("Unknown or duplicate answer case_id")
        seen.add(case_id)
        cited = answer["cited_ids"]
        if not isinstance(cited, list) or any(not isinstance(i, str) for i in cited):
            raise ValueError("cited_ids must be a list of exact strings")
        if len(cited) != len(set(cited)) or type(answer["abstained"]) is not bool or not isinstance(answer["answer"], str):
            raise ValueError("Malformed answer")
        case = private[case_id]
        rows = [r for r in history if r["case_id"] == case_id]
        bridge = ProjectBridge(directory / "hermes-home", case["project"], "catalog-judge")
        grounded = True
        for event_id in cited:
            try:
                record = bridge.memory.get(case["project"], event_id)
                grounded &= record["current"] and evidence_opened(rows, event_id, record["evidence_sha256"])
            except (ValueError, TypeError):
                grounded = False
        expected = case["expected_id"]
        observations.append(dict(case_id=case_id, corpus=case["corpus"], category=case["category"],
            expected_evidence_opened=evidence_opened(rows, expected, case["evidence_sha256"]) if expected else None,
            expected_id_cited=expected in cited if expected else None,
            all_citations_current_and_evidence_opened=bool(cited) and bool(grounded),
            absence_abstention_declared=answer["abstained"] if expected is None else None,
            tool_calls=len(rows), tool_result_bytes=sum(r["result_bytes"] for r in rows),
            answer=answer["answer"], cited_ids=cited))
    return dict(submitted=len(seen), missing=len(private) - len(seen),
        expected_cases=len(private), answer_entailment="not evaluated; parent/human review required",
        model_run="parent-supplied answers only; this harness invoked no model", cases=observations)


def verify(directory):
    """Structural traversal exercises bounds, scopes, exact receipts and evidence."""
    directory = Path(directory)
    results = {}
    for project in ("evaluation", "harbour"):
        bridge = ProjectBridge(directory / "hermes-home", project, "catalog-structure")
        prefixes, leaves, pages, maximum = [""], [], 0, 0
        while prefixes:
            prefix, cursor = prefixes.pop(), None
            while True:
                result = bridge.handle("cmi_project_topics", dict(prefix=prefix, cursor=cursor, limit=10))
                if "error" in result:
                    raise ValueError(result)
                size = max(len(json.dumps(result, ensure_ascii=a).encode("utf-8")) for a in (True, False))
                assert size <= 3000
                maximum = max(size, maximum)
                pages += 1
                for item in result["items"]:
                    if item["type"] == "group":
                        prefixes.append(item["prefix"])
                    else:
                        leaf = bridge.memory.get(project, item["id"], include_evidence=True)
                        assert leaf["current"] and leaf["revision"] == item["revision"]
                        assert hashlib.sha256(leaf["evidence"].encode("utf-8")).hexdigest() == item["evidence_sha256"]
                        leaves.append(item["id"])
                cursor = result["next_cursor"]
                if cursor is None:
                    break
        with bridge.memory._connect() as db:
            expected = {r[0] for r in db.execute("""SELECT e.id FROM events e WHERE e.project=?
                AND e.revision=(SELECT MAX(n.revision) FROM events n WHERE n.project=e.project AND n.topic=e.topic)""", (project,))}
        assert len(leaves) == len(set(leaves)) and set(leaves) == expected
        results[project] = dict(current_leaves=len(leaves), pages=pages, maximum_page_bytes=maximum)
    return dict(model_calls=0, structural_only=True, projects=results,
                semantic_or_answer_quality="not evaluated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "serve", "score", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("answers", type=Path, nargs="?")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.directory)
    elif args.command == "verify":
        result = verify(args.directory)
    elif args.command == "score":
        if args.answers is None:
            parser.error("score requires answers JSON array")
        result = score(args.directory, load(args.answers))
    else:
        for line in sys.stdin:
            try:
                response = tool_step(args.directory, json.loads(line))
            except (ValueError, TypeError, KeyError) as error:
                response = dict(error=str(error))
            print(json.dumps(response, ensure_ascii=False), flush=True)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
