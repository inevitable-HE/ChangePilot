from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from changepilot.planning.application.nodes import PlanningNodes
from changepilot.planning.application.state import PlanningState


def build_planning_graph(nodes: PlanningNodes) -> object:
    builder = StateGraph(PlanningState)
    builder.add_node("normalize_request", nodes.normalize_request)
    builder.add_node("check_required_context", nodes.check_required_context)
    builder.add_node("retrieve_knowledge", nodes.retrieve_knowledge)
    builder.add_node("generate_plan", nodes.generate_plan)
    builder.add_node("validate_plan", nodes.validate_plan)
    builder.add_node("repair_plan", nodes.repair_plan)
    builder.add_node("validate_repaired_plan", nodes.validate_repaired_plan)

    builder.add_edge(START, "normalize_request")
    builder.add_edge("normalize_request", "check_required_context")
    builder.add_conditional_edges(
        "check_required_context",
        _route_result_or,
        {"end": END, "continue": "retrieve_knowledge"},
    )
    builder.add_conditional_edges(
        "retrieve_knowledge",
        _route_result_or,
        {"end": END, "continue": "generate_plan"},
    )
    builder.add_conditional_edges(
        "generate_plan",
        _route_result_or,
        {"end": END, "continue": "validate_plan"},
    )
    builder.add_conditional_edges(
        "validate_plan",
        _route_after_validation,
        {
            "end": END,
            "repair": "repair_plan",
        },
    )
    builder.add_conditional_edges(
        "repair_plan",
        _route_result_or,
        {"end": END, "continue": "validate_repaired_plan"},
    )
    builder.add_edge("validate_repaired_plan", END)
    return builder.compile()


def _route_result_or(state: PlanningState) -> str:
    return "end" if "result" in state else "continue"


def _route_after_validation(state: PlanningState) -> str:
    return "end" if "result" in state else "repair"
