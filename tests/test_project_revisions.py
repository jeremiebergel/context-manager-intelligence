"""A revised decision keeps its earlier revisions readable by the model (cmi_project_get)."""
from cmi.project_bridge import ProjectBridge


def _publish(bridge, text, revision, model):
    bridge.model = model
    bridge.capture([{"role": "user", "content": text}])
    return bridge.handle("cmi_project_publish", dict(topic="pricing/currency", text=text, quote=text,
                                                     source="user:latest", expected_revision=revision))


def test_get_lists_earlier_revisions_of_a_revised_topic(tmp_path):
    claude = ProjectBridge(tmp_path, "p", "s1", "claude")
    first = _publish(claude, "Our prices are in dollars.", 0, "claude")
    second = _publish(claude, "Prices are in euros now.", 1, "claude")
    gpt = ProjectBridge(tmp_path, "p", "s2", "gpt")
    current = gpt.handle("cmi_project_get", dict(id=second["id"]))
    assert current["current"] is True
    revs = current["revisions"]
    assert [r["revision"] for r in revs] == [1, 2]
    assert revs[0]["id"] == first["id"] and revs[0]["current"] is False
    assert revs[0]["text"] == "Our prices are in dollars." and revs[0]["model"] == "claude"
    assert revs[0]["date"][:4].isdigit()
    assert revs[1]["current"] is True
    # The earlier revision opens in full, marked as superseded, with its evidence.
    old = gpt.handle("cmi_project_get", dict(id=revs[0]["id"], include_evidence=True))
    assert old["current"] is False and old["text"] == "Our prices are in dollars."
    assert old["evidence"] == "Our prices are in dollars."
    assert [r["revision"] for r in old["revisions"]] == [1, 2]


def test_single_revision_adds_no_history_field(tmp_path):
    b = ProjectBridge(tmp_path, "p", "s", "m")
    only = _publish(b, "Our prices are in dollars.", 0, "m")
    assert "revisions" not in b.handle("cmi_project_get", dict(id=only["id"]))


def test_revision_list_is_bounded(tmp_path):
    b = ProjectBridge(tmp_path, "p", "s", "m")
    last = {}
    for n in range(14):
        last = _publish(b, f"Price list version {n}. " + "x" * 600, n, "m")
    got = b.handle("cmi_project_get", dict(id=last["id"]))
    revs = got["revisions"]
    assert len(revs) == 10 and revs[-1]["revision"] == 14 and revs[0]["revision"] == 5
    assert got["revisions_omitted"] == 4
    assert all(len(r["text"]) <= 201 for r in revs)
    # Opening an old revision shows the revisions around it, plus the current one.
    second = next(r for r in b.memory.history("p", "pricing/currency") if r["revision"] == 2)
    old = b.handle("cmi_project_get", dict(id=second["id"]))["revisions"]
    assert 2 in [r["revision"] for r in old] and old[-1]["revision"] == 14 and old[-1]["current"] is True
    assert len(old) == 10


def test_revisions_never_cross_projects(tmp_path):
    mine = ProjectBridge(tmp_path, "mine", "s1", "m")
    other = ProjectBridge(tmp_path, "other", "s2", "m")
    _publish(other, "Other project: prices in yen.", 0, "m")
    _publish(other, "Other project: prices in won.", 1, "m")
    only = _publish(mine, "Our prices are in dollars.", 0, "m")
    assert "revisions" not in mine.handle("cmi_project_get", dict(id=only["id"]))


def test_search_points_to_revisions():
    from cmi.project_bridge import ProjectBridge as B
    get = next(s for s in B.schemas() if s["name"] == "cmi_project_get")
    assert "earlier revisions" in get["description"]
