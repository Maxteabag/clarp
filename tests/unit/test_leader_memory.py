from lib import leader_memory


def test_decision_log_search_apply_and_user_value_promotion(tmp_path, monkeypatch):
    user_values_path = tmp_path / "user-values.md"
    user_values_path.write_text("# User Values\n\nCurated value model.\n", encoding="utf-8")
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC", user_values_path)

    decision_id = leader_memory.log_decision(
        question="Should leaders run tests themselves?",
        user_answer="No, delegate execution to workers.",
        normalized_answer="Leaders delegate execution to workers.",
        decision_type="workflow",
        tags=["leader", "delegation"],
        source_trace="trace-1",
    )

    matches = leader_memory.search_decisions(
        "Should leaders run tests themselves?",
        tags=["leader"],
    )
    assert matches[0]["id"] == decision_id
    assert matches[0]["tags"] == ["leader", "delegation"]

    application_id = leader_memory.log_application(
        decision_id,
        trace_id="trace-2",
        reason="covered by existing delegation rule",
    )
    assert application_id.startswith("dapp_")

    fact_id = leader_memory.merge_decision_to_user_values(
        decision_id=decision_id,
        statement="Leaders delegate implementation, tests, builds, and commits to workers.",
        category="workflow_rule",
        promote=True,
        tags=["leader", "delegation"],
    )
    assert fact_id.startswith("sf_")
    assert "Leaders delegate implementation" not in user_values_path.read_text()

    compact = leader_memory.compact_user_values()
    assert "Curated value model" in compact
    assert "## Recent Promotions" not in compact
    assert "Leaders delegate implementation" not in compact

    memory = leader_memory.search_memory(
        "Leaders delegate implementation",
        tags=["delegation"],
    )
    assert memory["decisions"][0]["id"] == decision_id
    assert memory["user_value_facts"][0]["fact_id"] == fact_id


def test_standalone_user_value_fact_without_decision_id(tmp_path, monkeypatch):
    user_values_path = tmp_path / "user-values.md"
    user_values_path.write_text("# User Values\n\nCurated value model.\n", encoding="utf-8")
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC", user_values_path)

    fact_id = leader_memory.merge_decision_to_user_values(
        statement="Dreaming runs should use the user's local night window.",
        category="workflow_rule",
        promote=True,
        tags=["dreaming", "schedule"],
    )

    assert fact_id.startswith("sf_")
    assert "Dreaming runs should use the user's local night window" not in user_values_path.read_text()
    assert "Dreaming runs should use the user's local night window" not in leader_memory.compact_user_values()
    assert leader_memory.search_user_value_facts("Dreaming runs", tags=["dreaming"])


def test_decision_id_merge_uses_logged_answer_when_statement_omitted(
        tmp_path, monkeypatch):
    user_values_path = tmp_path / "user-values.md"
    user_values_path.write_text("# User Values\n\nCurated value model.\n", encoding="utf-8")
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC", user_values_path)
    decision_id = leader_memory.log_decision(
        question="When should dreaming run?",
        user_answer="Dreaming runs around 3AM in the user's local timezone.",
        normalized_answer="Run dreaming around 3AM in the user's local timezone.",
        decision_type="workflow",
        tags=["dreaming", "schedule"],
    )

    fact_id = leader_memory.merge_decision_to_user_values(
        decision_id=decision_id,
        category="workflow_rule",
        promote=True,
        tags=["dreaming", "schedule"],
    )

    assert fact_id.startswith("sf_")
    assert "Run dreaming around 3AM" not in user_values_path.read_text()
    assert "Run dreaming around 3AM" not in leader_memory.compact_user_values()


def test_compact_user_values_strips_file_and_db_promotions(tmp_path, monkeypatch):
    user_values_path = tmp_path / "user-values.md"
    user_values_path.write_text(
        "# User Values\n\nCurated value model.\n\n"
        "## Recent Promotions\n"
        "- <!-- user-value-fact:old --> [preference] Old file-only fact.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC", user_values_path)
    fact_id = leader_memory.merge_decision_to_user_values(
        statement="DB-rendered fact.",
        category="preference",
        promote=True,
    )

    compact = leader_memory.compact_user_values()

    assert "Curated value model" in compact
    assert "Old file-only fact" not in compact
    assert f"<!-- user-value-fact:{fact_id} --> [preference] DB-rendered fact." not in compact
    assert leader_memory.search_user_value_facts("DB-rendered fact")


def test_compact_user_values_keeps_only_core_value_sections(tmp_path, monkeypatch):
    user_values_path = tmp_path / "user-values.md"
    user_values_path.write_text(
        "# User Values\n\n"
        "## Current Value Model\n"
        "- Evidence first.\n\n"
        "## Standing Preferences\n"
        "- Do not dump this into every turn.\n\n"
        "## Anti-Goals\n"
        "- No noisy no-ops.\n\n"
        "## Product Taste\n"
        "- Also omitted.\n\n"
        "## Risk Posture\n"
        "- Ask on irreversible action.\n\n"
        "## Recent Promotions\n"
        "- <!-- user-value-fact:old --> [preference] Old file-only fact.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC", user_values_path)

    compact = leader_memory.compact_user_values()

    assert "Evidence first" in compact
    assert "No noisy no-ops" in compact
    assert "Ask on irreversible action" in compact
    assert "Standing Preferences" not in compact
    assert "Do not dump this" not in compact
    assert "Product Taste" not in compact
    assert "Old file-only fact" not in compact


def test_compact_user_values_searches_candidate_paths(tmp_path, monkeypatch):
    installed_root = tmp_path / "installed"
    source_root = tmp_path / "source"
    user_values_path = source_root / "docs" / "user-values.example.md"
    user_values_path.parent.mkdir(parents=True)
    user_values_path.write_text("# User Values\n\n- Loaded from source fallback.\n",
                         encoding="utf-8")
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC",
                        installed_root / "docs" / "user-values.example.md")
    monkeypatch.setattr(leader_memory, "_root_candidates",
                        lambda: [installed_root, source_root])

    assert "Loaded from source fallback" in leader_memory.compact_user_values()


def test_leader_context_includes_standing_orders_and_helper(tmp_path, monkeypatch):
    user_values_path = tmp_path / "user-values.md"
    user_values_path.write_text("# User Values\n\n- Evidence first.\n", encoding="utf-8")
    monkeypatch.setattr(leader_memory, "USER_VALUES_DOC", user_values_path)

    context = leader_memory.leader_context_instruction(leader_session="lena")

    assert "LEADER STANDING ORDERS v2" in context
    assert "Delegate with `--from lena`" in context
    assert "--from lena" in context
    assert "scripts/leader_decision.py search" in context
    assert "Evidence first" in context
