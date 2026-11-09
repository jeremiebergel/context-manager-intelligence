from cmi.project_memory import ProjectMemory
from cmi.project_retrieval import lexical_terms


def test_french_question_clitic_does_not_disable_semantic_retrieval(tmp_path):
    class Recorder:
        max_records = 4096
        def __init__(self):
            self.calls = []
        def rank(self, query, rows, *, limit):
            self.calls.append(query)
            return rows[:limit]
    adapter = Recorder()
    memory = ProjectMemory(tmp_path / 'memory.db', semantic=adapter)
    expected = memory.publish('p', 'release.scope', 'Complete product required.', source='user:release')
    assert [r['id'] for r in memory.search('p', 'Peut-on sortir une mouture incomplète ?')] == [expected['id']]
    assert lexical_terms('Peut-on sortir une mouture incomplète ?') == ['sortir', 'mouture', 'incomplète']
    assert adapter.calls == ['Peut-on sortir une mouture incomplète ?']
    assert memory.search('p', 'Peut-on travailler hors-ligne ?')
    assert len(adapter.calls) == 2
    assert memory.search('p', 'missing-module') == []
    assert memory.search('p', 'What about `missing-module`?') == []
    assert memory.search('p', 'Peut-on') == []
    assert memory.search('p', 'add-on') == []
    assert memory.search('p', 'Peut-on modifier `missing-module` ?') == []
    assert lexical_terms('Peut-on') == ['peut-on']
    assert len(adapter.calls) == 2
