"""Render measured JSON without recomputing or inventing retrieval results."""
import json
from pathlib import Path
p=Path(__file__).parent/'results'
d=json.loads((p/'retrieval-heldout-final-20261014.json').read_text())

def pct(x):return '—' if x is None else f'{x*100:.1f}%'
def row(method,category,s):
    return f"| {method} | {category} | {s['queries']} | {pct(s['precision'])} | {pct(s['recall'])} | {s['correct_abstentions']}/{s['absence_queries']} | {s['irrelevant_text_bytes']} | {s['latency_median_ms']:.2f} / {s['latency_p95_ms']:.2f} / {s['latency_max_ms']:.2f} |"
lines=['# Retrieval follow-up: measured improvement, not release validation','',
'## Frozen protocol','',
f"Corpus SHA256: `{d['corpus_sha256']}`. Twelve independently authored fictional operational claims, not reused original fixture facts. Twelve calibration queries (six positives/six hard negatives), then 36 heldout queries (24 positives/12 negatives). Database: {d['corpus']['all_events']} SQL-counted events, 32 latest in-project rows, 12 obsolete rows, 12 foreign rows. Each expected receipt is its exact current source/event identity. Limit three; no answers, extraction model, generated proxy inference, API, or live installation.",
'',
'Parameters were selected ONLY from calibration (zero source errors, maximize recall; ties higher cosine then smaller margin): MiniLM .70/.01 gave 1/6 positive recall; E5 .85/.01 gave 3/6. Both abstained on 6/6 calibration negatives. Thresholds and topic headers were kept unchanged on holdout. The gate considers the next-best score before filtering, returns at most one semantic source, and lexical/identifier contracts stay separate.',
'',
'**Holdout caveat:** the first report `retrieval-heldout-20261014.json` exposed duplicated common-word lexical noise. A TDD multi-term duplicate-source ambiguity guard was then added, without recalibrating the model. `retrieval-heldout-noise-guard-20261014.json` records that iteration, and the final report below includes the bare-code hyphen regression fix and calibrated MiniLM comparison. Final selector numbers are therefore post-inspection, not pristine unseen acceptance evidence. No fixture source labels were changed. A genuinely fresh real-session evaluation remains necessary.',
'',
'## Overall source-identity measurements','',
'Precision/recall with empty denominators is undefined (—), not 100%. Irrelevant bytes count only UTF-8 claim text. Search timings include first candidate-embedding population but exclude model initialization; hardware was concurrently loaded. Single run, not a stable latency SLA. Cold model measurements are isolated subprocesses below.',
'',
'| Method | Category | Queries | Precision | Recall | Correct absence | Irrelevant bytes | Median / p95 / max ms |',
'|---|---|---:|---:|---:|---:|---:|---:|']
for name,result in d['results'].items():lines.append(row(name,'all',result['overall']))
lines += ['', '## Per-category measurements', '',
'| Method | Category | Queries | Precision | Recall | Correct absence | Irrelevant bytes | Median / p95 / max ms |',
'|---|---|---:|---:|---:|---:|---:|---:|']
for name in ('baseline_or','current_minilm','candidate_e5'):
    for cat,s in d['results'][name]['categories'].items():lines.append(row(name,cat,s))
lines += ['', '## Cold model resources (shared across categories)', '',
'RSS is fresh-process peak after one real query/document inference, in macOS bytes; it includes Python, NumPy, ONNX Runtime and tokenizer imports. It is not the weight file size, incremental weight memory, full 4096-candidate cache usage, or total concurrently loaded evaluation memory.', '',
'| Encoder | Weight bytes | Tokenizer bytes | Load ms | First pair ms | Peak RSS bytes | Provider |',
'|---|---:|---:|---:|---:|---:|---|']
for name,r in d['cold_resources'].items():
    lines.append(f"| {name} | {r['model_bytes']} | {r['tokenizer_bytes']} | {r['load_ms']:.2f} | {r['first_single_record_inference_ms']:.2f} | {r['max_rss_bytes']} | {', '.join(r['providers'])} |")
lines += ['', f"Runtime: {d['platform']}; {d['runtime_versions']}.", '',
'## Diagnosis and model protocol','',
'The earlier zero-paraphrase result did not mean zero useful ranking. The full original 262-current-candidate raw probe (`retrieval-diagnosis-full-20261014.json`) ranks the correct source first for 6/11 paraphrases and 5/6 multilingual queries, before selector gates. Billing is top at .44059; storage at .48239. Both fail .65 and .50. The full corpus puts the French incomplete-release question at rank 20 behind literal release-scope distractors, unlike the small three-record probe: the candidate corpus matters. MiniLM pooling is masked mean, unprefixed, 128 tokens and L2-normalized cosine, matching the pinned SentenceTransformer configs; no pooling bug was found. French `Peut-on` had independently been misclassified as a code identifier. Any lexical hit still blocks semantic mixing, intentionally preserving that conservative boundary.',
'',
'E5 was evaluated because it is query/document trained, not because MiniLM lacked all signal. Its inspected pinned model card specifies `query: ` / `passage: ` (including non-English queries), masked mean pooling, L2 normalization, and 512-token truncation. Its scores concentrate around .7–1; they are not probabilities and cannot be compared directly to MiniLM scores. The exact pinned quantized ONNX artifact has an AVX512-VNNI filename, but was actually exercised on arm64 through CPUExecutionProvider, not claimed accelerated by AVX512.',
'',
'Header ablation is reporting-only, never a holdout tuning pass: top-one source recall before thresholds on the 24 heldout positives is MiniLM 21/24 with topics vs 20/24 without; E5 16/24 with topics vs 18/24 without. E5 improves accepted English retrieval at the fixed calibrated policy; it is not universally a better ranker. Topic removal was NOT adopted.',
'',
'## Unresolved precision and recall limits','',
'Candidate E5 gets 3/9 heldout English paraphrases, versus 0/9 current MiniLM, but still 0/2 French paraphrases, 0/2 hyphenated French positives and 0/5 cross-language positives. Fixing the hyphen selector gate therefore does not claim semantic success on those queries. The original 49-query regression fixture gets 1/11 paraphrases and 2/6 multilingual with E5, 100% precision and 63.4% overall recall; that old fixture is not independent calibration or release evidence.',
'',
'The remaining heldout false positive is the question about password recovery after permanent account erasure: both encoders return the deletion grace-period record, which omits password recovery. E5 gives it .88006 and margin .05677. Topic similarity plus a margin cannot prove all requested details are supported, detect unsupported premises, or replace an answerability/entailment check. Do not lower thresholds or claim this is solved. Broader multilingual/negative recall, calibrated answerability or a validated non-generative reranker, long-record chunking, >4096-candidate coverage, cross-platform resources and independent real-session data remain open. No engine/config bridge or default E5 switch was implemented.',
'',
'## Verification','',
'TDD observed real failures for French clitics/compound hyphens, bare `add-on` code normalization, missing pinned E5 adapter, and repeated boilerplate source ambiguity. Real model-enabled retrieval/storage tests passed. The full standalone suite with both real encoders passed: 293 passed, 3 skipped. A discovered full-suite repeated single-term `cold` retrieval regression was fixed by keeping the duplicate guard restricted to multi-term queries; the concurrent engine/binding fixes were not changed by this work.',
'',
'Primary protocol references and exact reproduction commands are in `RETRIEVAL.md`. Raw calibration scores, the entire calibration gate grid, source receipts, per-query latency, scores/margins, topic-header ablation and pinned artifacts are retained in the JSON reports.']
(p/'retrieval-followup-20261014.md').write_text('\n'.join(lines)+'\n')
print(p/'retrieval-followup-20261014.md')
