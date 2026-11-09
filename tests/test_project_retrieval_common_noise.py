"""Source ambiguity should not arbitrarily inject duplicated boilerplate."""
from cmi.project_memory import ProjectMemory


def test_identical_boilerplate_sources_abstain_even_with_limit_one(tmp_path):
    memory = ProjectMemory(tmp_path / 'memory.db')
    for i in range(5):
        memory.publish('p', f'layout.{i}', 'The header renders a navigation toggle.', source=f'user:{i}')
    assert memory.search('p', 'header navigation toggle', limit=1) == []
    assert memory.context('p', 'header navigation toggle') == ''
    assert len(memory.search('p', 'layout.1', limit=1)) == 1


def test_duplicate_text_in_foreign_or_obsolete_rows_does_not_create_ambiguity(tmp_path):
    memory = ProjectMemory(tmp_path / 'memory.db')
    fact = memory.publish('p', 'layout', 'The header renders a navigation toggle.', source='user:1')
    memory.publish('foreign', 'layout', fact['text'], source='user:foreign')
    memory.publish('p', 'other', fact['text'], source='user:old')
    memory.publish('p', 'other', 'Sidebar colour is blue.', source='user:new', expected_revision=1)
    assert [r['id'] for r in memory.search('p', 'header navigation toggle', limit=1)] == [fact['id']]
