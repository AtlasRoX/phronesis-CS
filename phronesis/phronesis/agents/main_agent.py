# /usr/bin/env python3

"""
This is the main agent file for the Phronesis.
"""

import logging

import hydra
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph_supervisor import create_supervisor

from ...vivarix.agents.t2b_agent import get_app as get_app_t2b
from ...lumicell.agents.scp_agent import LUMICELL_PROMPT, get_app as get_app_scp
from ...synapta.agents.t2kg_agent import get_app as get_app_t2kg
from ...lithoscribe.agents.s2_agent import get_app as get_app_s2
from ..states.state_phronesis import Phronesis

# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_app(uniq_id, llm_model: BaseChatModel):
    """
    This function returns the multi-agent langgraph supervisor app across all 4 domain agents.
    """
    if hasattr(llm_model, "model_name") and getattr(llm_model, "model_name", None) == "gpt-4o-mini":
        if hasattr(llm_model, "model_kwargs") and isinstance(llm_model.model_kwargs, dict):
            llm_model.model_kwargs["parallel_tool_calls"] = False
    # Load hydra configuration
    logger.log(logging.INFO, "Launching Phronesis_Agent with thread_id %s", uniq_id)
    with hydra.initialize(version_base=None, config_path="../configs"):
        cfg = hydra.compose(config_name="config", overrides=["agents/main_agent=default"])
        cfg = cfg.agents.main_agent
    logger.log(logging.INFO, "System_prompt of T2AA4P: %s", cfg.system_prompt)
    with hydra.initialize(version_base=None, config_path="../../vivarix/configs"):
        cfg_t2b = hydra.compose(config_name="config", overrides=["agents/t2b_agent=default"])
        cfg_t2b = cfg_t2b.agents.t2b_agent
    with hydra.initialize(version_base=None, config_path="../../synapta/configs"):
        cfg_t2kg = hydra.compose(config_name="config", overrides=["agents/t2kg_agent=default"])
        cfg_t2kg = cfg_t2kg.agents.t2kg_agent
    with hydra.initialize(version_base=None, config_path="../../lithoscribe/configs"):
        cfg_s2 = hydra.compose(
            config_name="config", overrides=["agents/s2_agent=default"]
        )
        cfg_s2 = cfg_s2.agents.s2_agent

    system_prompt = cfg.system_prompt
    sections = [
        ("Vivarix_Agent", cfg_t2b.state_modifier),
        ("Synapta_Agent", cfg_t2kg.state_modifier),
        ("Lumicell_Agent", LUMICELL_PROMPT),
        ("s2_agent", cfg_s2.s2_agent),
    ]
    for agent_name, agent_prompt in sections:
        system_prompt += f'\n\n<specialist_reference name="{agent_name}">\n{agent_prompt}\n</specialist_reference>'
    system_prompt += (
        "\n\n<shared_lookups>\n"
        "Specialists can call BioContext lookup tools (OpenTargets, KEGG, UniProt, EuropePMC, Ensembl, PanglaoDB) "
        "for live entity lookups. Results from these tools are evidence; anything not returned by a tool is not.\n"
        "</shared_lookups>"
    )
    # Create supervisor workflow orchestrating all 4 clinical domain agents
    workflow = create_supervisor(
        [
            get_app_t2b(uniq_id, llm_model),   # Vivarix
            get_app_t2kg(uniq_id, llm_model),  # Synapta
            get_app_scp(uniq_id, llm_model),   # Lumicell
            get_app_s2(uniq_id, llm_model),    # Lithoscribe
        ],
        model=llm_model,
        state_schema=Phronesis,
        # Full history is needed to extract
        # the tool artifacts
        output_mode="full_history",
        add_handoff_back_messages=True,
        prompt=system_prompt,
    )

    # Compile and run
    app = workflow.compile(checkpointer=MemorySaver(), name="Phronesis_Agent")

    return app
