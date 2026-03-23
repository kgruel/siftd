from siftd.tool_query import ToolQueryTerm


def test_tool_query_term_is_fielded_property_true_for_fielded_term():
    assert ToolQueryTerm(raw="tool:grep", field="tool", value="grep").is_fielded
