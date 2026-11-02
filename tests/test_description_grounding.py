"""
Descriptions say only what the model supports. An assistant asked to improve
the descriptions of a real model added agreements, causes and meanings that no
file states ("above the allowance agreed with the customer", "colour
calibration"), dropped a nuance of the old text and claimed to use no
acronyms. The server cannot read what the assistant writes, so it puts the
rule where the assistant reads: the full guidelines, the short text of the
tools, the preview, and the files in which the user keeps a glossary.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_data_platform_toolkit.semantic_model import description_rules, reader_tools, writer_tools
from mcp_data_platform_toolkit.semantic_model.description_guidelines import (SHORT_GUIDELINES, load_full_guidelines,
                                                                             with_short_guidelines)
from tests.self_test import create_pbip_project

ELEMENT = {"table_name": "Sales", "element_name": "Total Sales"}


@pytest.fixture
def project(tmp_path):
    return create_pbip_project(str(tmp_path / "pbip"))


def test_the_full_guidelines_say_what_may_be_stated(project):
    text = reader_tools.get_description_guidelines(project)
    assert "GROUNDING (what you may say):" in text
    for rule in ("Do NOT state business agreements, thresholds, reasons or causes",
                 "is explained only if the rules or the user define it",
                 "keep every fact it holds",
                 "say in one line what you inferred rather than read"):
        assert " ".join(rule.split()) in " ".join(text.split()), rule
    assert load_full_guidelines().index("GROUNDING") < load_full_guidelines().index("STYLE:")


def test_the_short_text_of_the_tools_carries_it_too():
    for must in ("no invented agreements", "keep the facts of the", "if unclear, ask"):
        assert must in " ".join(SHORT_GUIDELINES.split())
    assert with_short_guidelines("Writes it.").count(SHORT_GUIDELINES) == 1

    async def descriptions():
        mcp = MCPServer("grounding")
        reader_tools.register_reader_tools(mcp)
        writer_tools.register_writer_tools(mcp)
        return {t.name: t.description for t in await mcp.list_tools()}
    tools = asyncio.run(descriptions())
    for name in ("get_smart_description_preview", "write_smart_description"):
        assert "no invented agreements" in " ".join(tools[name].split()), name


def test_every_preview_with_a_proposal_repeats_the_rule(project):
    text = reader_tools.get_smart_description_preview(
        project, **ELEMENT, base_description="Revenue from all sales.", mode="merge")
    assert "Say only what the existing description, the technical context, the rules" in text
    assert "ask about the agreements, thresholds, causes or company terms you cannot see" in text
    assert "Overwrite replaces the existing description" not in text


def test_overwriting_an_existing_description_warns_not_to_lose_its_facts(project):
    writer_tools.write_smart_description(project, **ELEMENT, base_description="Revenue, tax excluded.",
                                         mode="overwrite")
    overwrite = reader_tools.get_smart_description_preview(
        project, **ELEMENT, base_description="Sales amount.", mode="overwrite")
    assert "Revenue, tax excluded." in overwrite
    assert "Overwrite replaces the existing description: the new text must still hold everything" in overwrite
    # Nothing to lose, or nothing proposed: no warning.
    merge = reader_tools.get_smart_description_preview(
        project, **ELEMENT, base_description="Sales amount.", mode="merge")
    first_look = reader_tools.get_smart_description_preview(project, **ELEMENT, mode="overwrite")
    assert "Overwrite replaces" not in merge and "Overwrite replaces" not in first_look


@pytest.mark.parametrize("template", [description_rules.GENERAL_TEMPLATE, description_rules.PROJECT_TEMPLATE])
def test_the_files_of_the_user_invite_a_glossary(template):
    assert "Glossary" in template and "defined here" in template
    # Inside the comment marks: it is an example, not a rule applied as it stands.
    assert template.index("Glossary") > template.index("<!--")
    assert template.index("Glossary") < template.index("-->")
