"""Prepare an independent prospective request-fidelity gate; never calls a model.

Run: python -m bench.project_request_fidelity RUN_DIR
Then use bench.project_catalog serve/score or the unchanged live-model harness.
Only questions.json, schemas.json and actual native tool results enter prompts.
Facts and expected sources are frozen separately before any parent model run.
This small gate is NOT calibration data and does not change earlier corpora.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from bench.project_catalog import dump, encoded, READ_TOOLS, MAX_CALLS, MAX_RESULT_BYTES
from cmi.project_bridge import ProjectBridge


# Entirely new operational source reports, unrelated to the earlier frozen sets.
FACTS = [
    ("certs.rotate", "For service certificates, install the replacement certificate in the inactive slot, "
     "run the handshake probe, then switch the active slot. Keep the previous certificate until the probe passes."),
    ("certs/rotate", "For the warehouse key-ring, rotate the physical selector clockwise until the blue mark "
     "aligns with the notch. This procedure is not about service certificates."),
    ("queue.pause", "Before conveyor maintenance, stop admitting new parcels and let already accepted parcels "
     "clear the belt. Only then engage the maintenance latch."),
    ("queue-pause", "The audio demonstration's queue-pause control freezes the pending playlist while the "
     "currently playing track continues. It does not control parcel conveyors."),
    ("archives.restore", "To restore a cold archive, verify its manifest digest before unpacking, then "
     "write the restored files into a new directory rather than overwriting the working directory."),
    ("atelier.ventilation", "Avant de nettoyer les filtres de l'atelier, couper l'alimentation du ventilateur "
     "et attendre son arrêt complet. Remettre les capots avant de rétablir l'alimentation."),
    ("console.east", "The status tile is shown next to the selected workspace."),
    ("console.west", "The status tile is shown next to the selected workspace."),
]

# Public questions have no labels or expected identifiers/answers attached.
QUESTIONS = [
    ("rf01", "What procedure is recorded under `certs.rotate`?"),
    ("rf02", "Que décrit exactement certs/rotate ?"),
    ("rf03", "How can I replace a service certificate without switching to an untested one?"),
    ("rf04", "Comment préparer le convoyeur de colis pour la maintenance ?"),
    ("rf05", "What does the `queue-pause` audio control do?"),
    ("rf06", "How should I recover files from cold storage without replacing my working copy?"),
    ("rf07", "Quelles précautions avant de nettoyer les filtres de ventilation de l'atelier ?"),
    ("rf08", "What does queue.pause require before engaging the maintenance latch?"),
    ("rf09", "What is the documented procedure for certs:rotate?"),
    ("rf10", "Que prévoit `archives/restore` ?"),
    ("rf11", "Explain the procedure called “queue_pause”."),
    ("rf12", "How do I pause the queue?"),
    ("rf13", "Quelle console faut-il utiliser pour cette tuile de statut de l'espace sélectionné ?"),
    ("rf14", "What is the exact maximum certificate-switch downtime in milliseconds?"),
    ("rf15", "Quel fournisseur et quelle durée de conservation utilise archives.restore ?"),
    ("rf16", "For `atelier.ventilation`, what protective glove model is mandatory?"),
]

# Judge-only mapping, separate from public questions. None means withhold the
# requested factual answer (clarification/explicit absence/unsupported detail).
EXPECTED = {
    "rf01": ("certs.rotate", "literal-positive"),
    "rf02": ("certs/rotate", "literal-positive-fr"),
    "rf03": ("certs.rotate", "paraphrase-positive"),
    "rf04": ("queue.pause", "paraphrase-positive-fr"),
    "rf05": ("queue-pause", "literal-positive"),
    "rf06": ("archives.restore", "paraphrase-positive"),
    "rf07": ("atelier.ventilation", "paraphrase-positive-fr"),
    "rf08": ("queue.pause", "bare-literal-positive"),
    "rf09": (None, "bare-punctuation-absent"),
    "rf10": (None, "quoted-punctuation-absent-fr"),
    "rf11": (None, "unicode-quoted-punctuation-absent"),
    "rf12": (None, "ambiguous-domain"),
    "rf13": (None, "indistinguishable-candidates-fr"),
    "rf14": (None, "unsupported-precision"),
    "rf15": (None, "unsupported-details-fr"),
    "rf16": (None, "unsupported-detail-with-exact-id"),
}


def prepare(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    bridge = ProjectBridge(directory / "hermes-home", "prospective-ops", "fidelity-fixture")
    records = {}
    for topic, text in FACTS:
        receipt = bridge.memory.publish("prospective-ops", topic, text,
                                        source="fixture:prospective:" + topic, evidence=text)
        records[topic] = bridge.memory.get("prospective-ops", receipt["id"])
    public = dict(cases=[dict(case_id=i, question=q) for i, q in QUESTIONS])
    private = dict(cases=[])
    for case_id, _ in QUESTIONS:
        topic, category = EXPECTED[case_id]
        record = records[topic] if topic else None
        private["cases"].append(dict(case_id=case_id, corpus="request-fidelity-prospective",
            category=category, project="prospective-ops", expected_topic=topic,
            expected_id=record["id"] if record else None,
            evidence_sha256=record["evidence_sha256"] if record else None))
    frozen = dict(facts=FACTS)
    schemas = [s for s in bridge.schemas() if s["name"] in READ_TOOLS]
    repository = Path(__file__).resolve().parents[1]
    implementation = {name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in ("cmi/project_memory.py", "cmi/project_bridge.py", "bench/project_catalog.py",
                     "bench/project_request_fidelity.py", "bench/project_catalog_live.py")}
    manifest = dict(format=2, corpus="request-fidelity-prospective", cases=len(QUESTIONS), model_calls=0,
        implementation_sha256=implementation,
        host_request_capture="questions.json original user text captured on every fresh bridge; no judge labels",
        fixture_sha256=hashlib.sha256(encoded(frozen)).hexdigest(),
        judge_sha256=hashlib.sha256(encoded(private)).hexdigest(),
        questions_sha256=hashlib.sha256(encoded(public)).hexdigest(),
        native_schemas_sha256=hashlib.sha256(encoded(schemas)).hexdigest(),
        tools=sorted(READ_TOOLS), max_calls_per_case=MAX_CALLS,
        max_tool_result_bytes_per_case=MAX_RESULT_BYTES,
        prompt_policy="Only questions.json, schemas.json and actual tool results; no judge/fixtures/expected IDs.",
        scoring_scope="Source opening/citation only; request fidelity and answer entailment require parent review.")
    dump(directory / "questions.json", public)
    dump(directory / "judge-private.json", private)
    dump(directory / "fixture-request-fidelity-frozen.json", frozen)
    dump(directory / "schemas.json", schemas)
    dump(directory / "manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory), ensure_ascii=False, indent=2))
