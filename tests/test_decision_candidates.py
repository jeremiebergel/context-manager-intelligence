from cmi.decision_candidates import find_candidates

LONG = "x" * 220


def _conv(proposal, reply):
    return [{"role": "user", "content": "q"}, {"role": "assistant", "content": LONG + proposal},
            {"role": "user", "content": reply}]


def test_choice_then_approval_is_candidate():
    c = find_candidates(_conv(" Mon avis : on garde A et on écarte B.", "go"))
    assert [x["index"] for x in c] == [2]


def test_action_go_ahead_is_rejected():
    assert find_candidates(_conv(" Je lance l'inspect maintenant ?", "go")) == []


def test_never_pairs_with_older_exchange():
    msgs = _conv(" on garde A au lieu de B.", "go") + [{"role": "assistant", "content": "ok"},
                                                    {"role": "user", "content": "oui"}]
    assert [x["index"] for x in find_candidates(msgs)] == [2]
