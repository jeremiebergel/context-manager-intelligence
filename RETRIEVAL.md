# Project-memory retrieval: measured, not solved

## Contracts

Default retrieval has no new dependencies, model, API key or generative LLM.
It removes EN/FR/ES/DE question boilerplate, then requires every remaining
literal content term (FTS AND). This is precision-first lexical matching,
**not semantic understanding**. Unknown content terms cause abstention.
Queries over 4096 characters or 32 distinct content terms abstain instead of
silently losing their tail. Operators are quoted as literal user data.

Dotted/underscored/path/hyphenated identifiers and C++/C# preserve punctuation.
A SQLite predicate checks complete identifier tokens before LIMIT. Both paths
filter the project, latest topic revision and text-size budget before limits.
Existing evidence/history/CAS/permissions and context UTF-8 byte caps remain
unchanged. Retrieval returns stored records, not manufactured answers.

## Optional local prototype

`LocalSemanticRetriever` explicitly loads two user-supplied files. It performs
CPU ONNX inference, attention-mask mean pooling and normalized cosine ranking.
No downloads, network calls or remote code occur in this adapter. Initialization
verifies pinned model AND tokenizer SHA-256 hashes before optional imports.
Optional runtime dependencies are numpy, onnxruntime and tokenizers; none was
added to required dependencies. Initialization failure must be caught by the
caller if lexical-only startup is desired; runtime ranking failures log a
warning and retain the lexical result. Normal lexical hits are not mixed with
semantic guesses. Empty/boilerplate/identifier-containing misses never fuzzy-match.

The current implementation is programmatic opt-in only:

```python
from pathlib import Path
from cmi.project_memory import ProjectMemory
from cmi.project_retrieval import LocalSemanticRetriever
adapter = LocalSemanticRetriever(Path.home() / '.hermes/cache/scratch/cmi-retrieval-model')
memory = ProjectMemory(Path('/absolute/non-symlink/memory.db'), semantic=adapter)
```

No engine configuration was changed or added. An embedding cache is bounded
at 1024 documents and keyed by project/event/revision/content; candidate sets
over 4096 current records abstain with a warning. Claims over 16384 text bytes
are excluded from semantic ranking. The model truncates to 128 tokens; facts
at the end of long records may not influence ranking. No persistent vectors
or changes to the SQLite schema are introduced.

## Primary references and artifacts

Model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2,
revision `86741b4e3f5cb7765a600d3a3d55a0f6a6cb443d`.
The original adapter implements the publisher's 384-dimensional mean-pooling
contract, not a competitor implementation. Model/tokenizer: Apache-2.0;
ONNX Runtime: MIT; Hugging Face tokenizers: Apache-2.0; numpy: BSD-3-Clause.
Preserve the respective licenses/notices if redistributing these artifacts.
They are downloaded to scratch, not included in the repository/package.

Primary sources inspected:
- https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/86741b4e3f5cb7765a600d3a3d55a0f6a6cb443d/README.md
- Same revision: `1_Pooling/config.json` and `sentence_bert_config.json`.
- https://github.com/microsoft/onnxruntime/blob/main/LICENSE
- https://github.com/huggingface/tokenizers/blob/main/LICENSE

Pinned files:
- `onnx/model_qint8_arm64.onnx`, saved locally as `model.onnx`: 118412398 bytes;
  SHA-256 `783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672`.
- `tokenizer.json`: 9081518 bytes;
  SHA-256 `2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8`.

The benchmark records actual runtime versions, hashes, source IDs and scores.
Tested on Darwin arm64/Python 3.12.13: numpy 2.5.4, onnxruntime 1.31.0,
tokenizers 0.23.3. Other operating systems were not verified.

## Reproduce

The following downloads are explicit preparation, never adapter side effects:

```sh
MODEL="$HOME/.hermes/cache/scratch/cmi-retrieval-model"
REV=86741b4e3f5cb7765a600d3a3d55a0f6a6cb443d
BASE="https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/resolve/$REV"
mkdir -p "$MODEL"
curl --fail --location "$BASE/onnx/model_qint8_arm64.onnx" -o "$MODEL/model.onnx"
curl --fail --location "$BASE/tokenizer.json" -o "$MODEL/tokenizer.json"
CMI_STANDALONE=1 uv --system-certs run python bench/project_retrieval.py \
  --output bench/results/retrieval-lexical-20261014.json
CMI_STANDALONE=1 uv --system-certs run --with numpy==2.5.4 \
  --with onnxruntime==1.31.0 --with tokenizers==0.23.3 \
  python bench/project_retrieval.py --model-dir "$MODEL" \
  --output bench/results/retrieval-local-20261014.json
CMI_STANDALONE=1 CMI_TEST_RETRIEVAL_MODEL="$MODEL" \
  uv --system-certs run --with pytest --with numpy==2.5.4 \
  --with onnxruntime==1.31.0 --with tokenizers==0.23.3 \
  python -m pytest -q tests/test_project_retrieval.py tests/test_project_memory.py \
  --basetemp "$HOME/.hermes/cache/scratch/cmi-retrieval-optional-tests"
```

## Evaluation and release limits

The fixed synthetic suite has 49 questions: 12 identifiers, 12 literal
questions, 11 paraphrases, 6 cross-language questions and 8 absence cases.
The database has 487 events: 12 target topics, 262 current in-project rows,
200 foreign-project rows and 25 obsolete rows. Distractors include common-word
UI noise and punctuation-colliding identifiers. SQL-derived counts and corpus/
query fingerprints are verified. All methods return at most 3 records and use
the same database. `legacy_search` freezes the original quoted-whitespace OR
query behavior rather than silently comparing against the new implementation.

Precision is relevant retrieved records / retrieved records; recall is relevant
retrieved / expected records across the 41 answerable queries. Irrelevant bytes
count UTF-8 claim text only, excluding receipt overhead. Empty precision is
undefined, not falsely 100%. Per-question receipts contain expected and returned
IDs, source, project, topic, revision, full claim text and optional cosine score.
They are source-identity judgements, not guessed LLM answers. Latencies measure
one search per question, including cold embedding population but not model load.

From `bench/results/retrieval-local-20261014.json`:

| Method | Precision | Recall | Irrelevant bytes | Absence correct | Median / p95 ms |
|---|---:|---:|---:|---:|---:|
| Original OR | 31.0% | 65.9% | 4890 | 2/8 | 0.77 / 1.38 |
| Lexical AND | 100% | 53.7% | 0 | 8/8 | 0.49 / 0.71 |
| Optional local fallback, cosine >= .65 | 100% | 61.0% | 0 | 8/8 | 1.12 / 7.42 |

Local model load took 696 ms; maximum/cold population search took 1034 ms.
Do not represent the median or p95 as cold-start latency. Timing is noisy,
particularly under concurrent test runs. Lexical precision improves at the
cost of recall. At the unchanged .65 default, semantic fallback added three
source-verified hits, but **still recalled 0/11 paraphrases and only 1/6
cross-language questions**. Generic semantic recall is not solved.

A clearly exploratory run at .50 is recorded separately in
`retrieval-local-exploratory-t050-20261014.json`: precision 100%, recall 68.3%,
8/8 absence abstentions, but still 0/11 paraphrases; multilingual recall 4/6.
This is not held-out calibration and did not change the default threshold.
Zero observed false positives in this small fixture is not a safety guarantee.

Observed TDD RED: common-word UI contamination; identifier phrase collision;
missing local adapter; missing store opt-in; missing runtime-failure fallback;
unpinned artifact acceptance; C++/C# collision; oversized-query acceptance.
Then GREEN: 45 targeted tests including real local inference. Broader standalone
run: 261 passed, 4 skipped, 2 failed. Both failures are existing binding tests
`test_env_compression_child_preserves_matching_parent_home` in the concurrently
edited engine/binding area; no disallowed files were changed to fix them.

Before public release: improve semantic recall on independent real-session
and unseen negative corpora, assess a query/document-trained encoder or safe
hybrid ranking, calibrate thresholds without fitting this suite, handle long
records/candidate sets, add explicit engine opt-in configuration, measure
cross-platform runtime/resource behavior and clear the binding regressions.
Do not claim a complete retrieval release, broad competitor superiority or
end-to-end assistant accuracy from these measurements.

## October 14 follow-up: real query/document encoder, bounded improvement

The sections above describe the prior implementation/results. Current measured
follow-up is `bench/results/retrieval-followup-20261014.md`, rendered by
`python3 bench/retrieval_report.py` from real JSON inference and source receipts.
The full standalone suite with both real encoders now passes: **293 passed,
3 skipped**. No engine/config bridge, live installation, publication, commit,
mandatory dependency, extraction model or generative model was added here.

### Diagnosis before architecture changes

`retrieval-diagnosis-full-20261014.json` probes the original fixture's **262
latest in-project candidates**, not just three example records. MiniLM ranks
correct sources first for 6/11 paraphrases and 5/6 multilingual questions before
thresholds. Billing scores .44059 and storage .48239: both fail .65 AND .50.
The French incomplete-release query ranks its expected source twentieth behind
literal release-scope distractors in that full corpus; a three-record top-one
result does not establish full-corpus retrieval. Topic/text header ablations are
stored, not used to tune heldout gates.

The inspected pinned MiniLM configs specify 128 tokens, masked mean pooling,
no retrieval prefixes. The implementation follows that protocol with normalized
cosine. No pooling bug was found. French grammatical inversion (`Peut-on`,
`gardent-ils`) was incorrectly classified as code punctuation and is now
normalized only in natural sentences outside quoted code. A bounded compound
rule handles `hors-ligne` in such sentences. Bare/quoted identifiers, dots,
underscores, paths, C++/C# retain the existing exact contract. This is not a
complete multilingual grammar parser. Lexical hits still block semantic mixing.

A multi-term lexical query whose returned claims are identical across different
topics now abstains rather than emitting arbitrary repeated boilerplate sources.
It probes at least two rows even for limit one. Exact identifiers and existing
single-term broad retrieval retain their behavior. This guard was TDD-driven
AFTER the first heldout report exposed noise; final selector numbers are
post-inspection, not pristine unseen confirmation. Model gates were NOT retuned.

### Optional E5 adapter and primary protocol

`LocalQueryDocumentRetriever` uses explicitly provisioned
`intfloat/multilingual-e5-small`, revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`. Both weights and tokenizer are
SHA256-pinned in code; initialization never downloads anything. The pinned
quantized artifact is named `onnx/model_qint8_avx512_vnni.onnx`; it actually ran
on arm64 with ONNX Runtime CPUExecutionProvider, without claiming AVX512 use.
The inspected model card specifies `query: ` / `passage: ` even for non-English
retrieval, masked mean pooling, normalization and 512-token truncation. Its cosine
scores concentrate near .7–1 and are not calibrated probabilities or directly
comparable with MiniLM. The adapter returns at most one semantic source only when
cosine >= .85 and its margin over the next-best candidate is >= .01. The margin
is calculated BEFORE threshold filtering. No engine/default switch was made.

Primary references inspected directly (the generic extraction backend failed,
so pinned raw documents were retrieved with urllib):

- MiniLM sequence config: `https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/raw/86741b4e3f5cb7765a600d3a3d55a0f6a6cb443d/sentence_bert_config.json`
- MiniLM pooling config: `https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/raw/86741b4e3f5cb7765a600d3a3d55a0f6a6cb443d/1_Pooling/config.json`
- E5 pinned model card/protocol/training: `https://huggingface.co/intfloat/multilingual-e5-small/raw/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md`
- E5 paper, linked by its author model card: `https://arxiv.org/abs/2402.05672`

### Independent corpus, calibration and measured limits

`bench/retrieval_holdout.json` was frozen before inspecting its rankings:
SHA256 `81337fbc36f99f250eb3eb9f2b09e62fbd4939858a42a57cae5219c4d3609dc7`.
It has twelve new fictional operational claims, twelve calibration queries
(six positives/six negatives), and 36 heldout queries (24 positives/12 negatives).
Actual SQL counts are 56 events: 32 current project rows, twelve obsolete rows,
and twelve foreign rows. Initial report metadata incorrectly said 68; that
count was corrected after SQL verification, without changing observations.

Calibration-only gate search found MiniLM .70/.01: 1/6 positive hits, no errors;
E5 .85/.01: 3/6 positive hits, no errors. Full calibration grids/raw rankings are
retained. No lower threshold was selected on the old fixture or heldout set.
Header ablation on the heldout positives gives raw top-one MiniLM 21/24 with
headers versus 20/24 without, E5 16/24 versus 18/24. Header removal was not adopted;
E5 is not uniformly a better ranker, even though its accepted retrieval improves.

Final heldout source-identity metrics (`retrieval-heldout-final-20261014.json`):

| Method | Precision | Recall | Correct absence | Irrelevant claim bytes |
|---|---:|---:|---:|---:|
| Frozen baseline OR | 17.2% | 62.5% | 1/12 | 6759 |
| Frozen current MiniLM .65 | 55.6% | 20.8% | 10/12 | 280 |
| Fixed selector, MiniLM .65 | 83.3% | 20.8% | 11/12 | 103 |
| Fixed selector, calibrated MiniLM .70/.01 | 83.3% | 20.8% | 11/12 | 103 |
| Fixed selector, E5 .85/.01 | 90.0% | 37.5% | 11/12 | 103 |

E5 recalls 3/9 English paraphrases versus current MiniLM 0/9, but remains at
0/2 French paraphrases, 0/2 hyphenated French positives and 0/5 cross-language
positives. The original 49-query fixture gets 1/11 paraphrases and 2/6 multilingual
with E5; that fixture is not heldout calibration or broad release evidence.
The remaining negative hit asks about password recovery after permanent erasure;
the source only documents a deletion grace period. E5 assigns .88006/margin
.05677. Embedding confidence and a margin are NOT an answerability proof.

Per-category precision/recall/absence/irrelevant bytes/median/p95/max latency,
all receipts and raw header ablations are in the JSON and rendered report.
Cold fresh-process peaks in the latest run are 675,069,952 bytes for MiniLM
and 746,668,032 bytes for E5, for approximately 118 MB quantized weight files,
including optional runtime imports. Earlier run peaks vary. Reported cold
initialization, first-pair inference and first candidate population are separate
from search median/p95. These single-run arm64 timings are not a latency SLA.
Long records, candidate sets beyond 4096, robust answerability/negative handling,
fresh real-session evaluation and multilingual recall remain unresolved.

### Reproduction (repo root, scratch artifacts, no installation)

Reuse the pinned MiniLM `$MODEL` setup above. Provision E5 explicitly:

```sh
E5="$HOME/.hermes/cache/scratch/cmi-retrieval-e5"
REV=614241f622f53c4eeff9890bdc4f31cfecc418b3
BASE="https://huggingface.co/intfloat/multilingual-e5-small/resolve/$REV"
mkdir -p "$E5"
curl --fail --location "$BASE/onnx/model_qint8_avx512_vnni.onnx" -o "$E5/model.onnx"
curl --fail --location "$BASE/tokenizer.json" -o "$E5/tokenizer.json"

CMI_STANDALONE=1 uv --system-certs run --with numpy==2.5.4 \
  --with onnxruntime==1.31.0 --with tokenizers==0.23.3 \
  python bench/retrieval_diagnose.py "$MODEL" \
  bench/results/retrieval-diagnosis-full-20261014.json
CMI_STANDALONE=1 uv --system-certs run --with numpy==2.5.4 \
  --with onnxruntime==1.31.0 --with tokenizers==0.23.3 \
  python bench/retrieval_e5_calibration.py "$E5"
python3 bench/retrieval_calibrate.py
CMI_STANDALONE=1 uv --system-certs run --with numpy==2.5.4 \
  --with onnxruntime==1.31.0 --with tokenizers==0.23.3 \
  python bench/retrieval_heldout.py --minilm-dir "$MODEL" --e5-dir "$E5" \
  --output bench/results/retrieval-heldout-final-20261014.json
python3 bench/retrieval_report.py
CMI_STANDALONE=1 CMI_TEST_RETRIEVAL_MODEL="$MODEL" CMI_TEST_RETRIEVAL_E5="$E5" \
  uv --system-certs run --with pytest --with numpy==2.5.4 \
  --with onnxruntime==1.31.0 --with tokenizers==0.23.3 python -m pytest -q \
  --basetemp "$HOME/.hermes/cache/scratch/cmi-retrieval-reproduction-tests"
```


## October 15: active-model catalogue gate (current recommended path)

Passive selectors above remain limited. The measured path is the already-running
main model navigating `cmi_project_topics` → `cmi_project_get(include_evidence)`
and answering only from exact opened sources. No auxiliary model is added; the
cost is ordinary tool calls, tokens and latency.

| Run (gpt-6.1-sol / openai-codex) | Expected sources opened+cited | Correct abstentions |
|---|---:|---:|
| Heldout 36, before request-fidelity guidance | 24/24 | 10/12 |
| Prospective 16 after guidance (frozen before run) | 8/8 | 8/8 |
| Retrospective rerun of the 2 prior failures | — | 2/2 |

Reports: `bench/results/project-catalog-live-heldout-complete-20261015.{json,md}`,
`bench/results/project-request-fidelity-live-20261015.json`,
`bench/results/project-request-fidelity-handoff-20261015.md`. The prospective set
was frozen by its author (unseen by the evaluated model, not author-blind); answers
were parent-inspected against opened evidence, not blind human graded. Single
model/provider; Claude-side and native Desktop end-to-end runs are still pending.
