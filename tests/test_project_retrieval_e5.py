import os
import pytest
from cmi import project_retrieval


def test_e5_rejects_unpinned_artifacts_before_import(tmp_path):
    (tmp_path / 'model.onnx').write_bytes(b'wrong')
    (tmp_path / 'tokenizer.json').write_text('{}')
    with pytest.raises(ValueError, match='pinned'):
        project_retrieval.LocalQueryDocumentRetriever(tmp_path)


def test_real_query_document_encoder_recalls_translation_and_abstains(tmp_path):
    model = os.environ.get('CMI_TEST_RETRIEVAL_E5')
    if not model:
        pytest.skip('explicit local E5 artifacts required; never downloads')
    from cmi.project_memory import ProjectMemory
    adapter = project_retrieval.LocalQueryDocumentRetriever(model)
    memory = ProjectMemory(tmp_path / 'memory.db', semantic=adapter)
    invoice = memory.publish('p', 'invoices.delivery', 'Les factures finalisées sont envoyées en pièce jointe au contact comptable chaque vendredi.', source='user:invoice')
    memory.publish('p', 'exports.encoding', 'Les exports CSV utilisent UTF-8 avec un indicateur BOM pour éviter les accents corrompus dans Excel.', source='user:csv')
    memory.publish('p', 'webhooks.delivery', 'Webhook deliveries include a unique event identifier; recipients must deduplicate repeated deliveries.', source='user:webhook')
    memory.publish('foreign', 'invoices.delivery', invoice['text'], source='foreign:invoice')
    hits = memory.search('p', 'Comment sont transmis les documents de facturation ?', limit=3)
    assert [r['id'] for r in hits] == [invoice['id']]
    assert hits[0]['retrieval']['margin'] >= .01
    assert memory.search('p', 'Quelle est la taille maximale des fichiers CSV ?') == []
    assert adapter._tokenizer.truncation['max_length'] == 512
    assert adapter.query_prefix == 'query: '
    assert adapter.document_prefix == 'passage: '
