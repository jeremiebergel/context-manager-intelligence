"""Unit-only controlled vectors exercise gate boundaries, not model quality."""
import threading
from collections import OrderedDict
import pytest
from cmi.project_retrieval import LocalQueryDocumentRetriever


@pytest.mark.parametrize('scores,expected', [((.855,.848), []),((.86,.849), ['a']),((.84,.2), [])])
def test_margin_uses_runner_up_even_below_threshold(scores,expected):
    np=pytest.importorskip('numpy')
    adapter=LocalQueryDocumentRetriever.__new__(LocalQueryDocumentRetriever)
    adapter.threshold=.85;adapter.cache_size=1024;adapter._np=np
    adapter._lock=threading.RLock();adapter._cache=OrderedDict();adapter.model_sha256='unit-only'
    adapter._encode=lambda texts: np.array([[1.]]) if len(texts)==1 else np.array([[s] for s in scores])
    rows=[dict(project='p',id=t,revision=1,topic=t,text=t) for t in ['a','b']]
    assert [r['id'] for r in adapter.rank('query',rows,limit=3)]==expected
