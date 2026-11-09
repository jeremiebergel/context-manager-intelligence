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
    # Preserve quoted code tokens; expand only grammatical inversion and closed
    # French compound prefixes in a sentence containing that grammar. A bare
    # `missing-module` (or even bare `hors-ligne`) remains a literal identifier.
    verb = r"(?:[^\W\d_]+(?:ent|ait|aient|era|eront|ez|ons)|peut|doit|faut|est|sont|ont|a|va)"
    inversion = rf"\b({verb})-(?:t-)?(on|ils?|elles?|vous|nous|tu|je)\b"
    parts = re.split(r'(`[^`]*`|"[^"]*")', query)
    natural_sentence = (len(query.split()) >= 3 and
                        any(re.search(inversion, part, re.IGNORECASE) for part in parts[::2]))
    for i in range(0, len(parts), 2):
        part = parts[i]
        if natural_sentence:
            part = re.sub(inversion, r"\1 \2", part, flags=re.IGNORECASE)
            part = re.sub(r"\b(hors|non|quasi|demi)-([^\W\d_]+)\b", r"\1 \2",
                          part, flags=re.IGNORECASE)
        parts[i] = part
    query = "".join(parts)
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


class LocalSemanticRetriever:
    """Opt-in, CPU-only ONNX encoder. Never downloads or calls a remote service.

    Uses pinned multilingual MiniLM, mean pooling, normalized cosine similarity.
    A threshold is a heuristic, not an entailment or truth guarantee. Input rows
    MUST already be scoped/current; ProjectMemory enforces this before ranking.
    """
    model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    model_revision = "86741b4e3f5cb7765a600d3a3d55a0f6a6cb443d"
    max_records = 4096
    expected_model_sha256 = "783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672"
    expected_tokenizer_sha256 = "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8"
    max_tokens = 128
    query_prefix = ""
    document_prefix = ""
    top_only = False
    minimum_margin = 0.0

    def __init__(self, model_dir, *, threshold=0.65, cache_size=1024):
        import hashlib
        import math
        import threading
        from collections import OrderedDict
        from pathlib import Path
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("threshold must be finite and between zero and one")
        if not isinstance(cache_size, int) or not 1 <= cache_size <= self.max_records:
            raise ValueError("cache_size must be between 1 and 4096")
        path = Path(model_dir)
        model_path = path / "model.onnx"
        tokenizer_path = path / "tokenizer.json"
        def file_hash(filename):
            digest = hashlib.sha256()
            with filename.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        self.model_sha256 = file_hash(model_path)
        self.tokenizer_sha256 = file_hash(tokenizer_path)
        if (self.model_sha256 != self.expected_model_sha256
                or self.tokenizer_sha256 != self.expected_tokenizer_sha256):
            raise ValueError("local model/tokenizer must match the pinned public encoder hashes")
        # Optional imports only after validation: base CMI is dependency-free.
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self._np = np
        self.threshold = threshold
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self._tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=self.max_tokens)
        self._tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(model_path), sess_options=options,
                                             providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._session.get_inputs()}

    def _encode(self, texts):
        np = self._np
        encoded = self._tokenizer.encode_batch(texts)
        feeds = {
            "input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
            "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64),
        }
        hidden = self._session.run(["last_hidden_state"],
                                   {k: v for k, v in feeds.items() if k in self._inputs})[0]
        mask = feeds["attention_mask"][..., None].astype(np.float32)
        pooled = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
        return pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)

    def rank(self, query, rows, *, limit=5):
        import hashlib
        if not rows or limit <= 0:
            return []
        if len(rows) > self.max_records:
            raise ValueError("local semantic scan exceeds the 4096-current-record bound")
        with self._lock:
            documents = [self.document_prefix + r["topic"] + "\n" + r["text"] for r in rows]
            keys = [(r["project"], r["id"], r["revision"],
                     hashlib.sha256(doc.encode()).digest()) for r, doc in zip(rows, documents)]
            vectors = [None] * len(rows)
            missing = []
            for i, key in enumerate(keys):
                vector = self._cache.get(key)
                if vector is None:
                    missing.append(i)
                else:
                    self._cache.move_to_end(key)
                    vectors[i] = vector
            for start in range(0, len(missing), 16):
                indices = missing[start:start+16]
                embeddings = self._encode([documents[i] for i in indices])
                for i, vector in zip(indices, embeddings):
                    vectors[i] = vector
                    self._cache[keys[i]] = vector
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
            query_vector = self._encode([self.query_prefix + query[:16384]])[0]
            scores = self._np.stack(vectors) @ query_vector
            order = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), rows[i]["topic"], rows[i]["id"]))
            margin = float(scores[order[0]]) - (float(scores[order[1]]) if len(order) > 1 else 0.0)
            if self.top_only:
                if margin < self.minimum_margin:
                    return []
                order = order[:1]
            return [dict(rows[i], retrieval={"method": "local-semantic", "score": float(scores[i]),
                        "model": self.model_name, "model_sha256": self.model_sha256,
                        **({"margin": margin} if self.top_only else {})})
                    for i in order[:limit] if float(scores[i]) >= self.threshold]


class LocalQueryDocumentRetriever(LocalSemanticRetriever):
    """Pinned multilingual E5 asymmetric retrieval, conservative top-one gate.

    Defaults were selected only on the frozen calibration split, not holdout.
    Cosine/margin gates are relevance heuristics, not answerability guarantees.
    """
    model_name = "intfloat/multilingual-e5-small"
    model_revision = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    expected_model_sha256 = "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"
    expected_tokenizer_sha256 = "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"
    max_tokens = 512
    query_prefix = "query: "
    document_prefix = "passage: "
    top_only = True
    minimum_margin = 0.01

    def __init__(self, model_dir, *, threshold=0.85, cache_size=1024):
        super().__init__(model_dir, threshold=threshold, cache_size=cache_size)
