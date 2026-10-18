# /usr/bin/env python3

"""
This is the agent file for the Lumicell graph.
"""

import logging
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, create_react_agent

from ...tools.biocontext import (
    BioContextEnsemblTool,
    BioContextPanglaoDBTool,
    BioContextTool,
)
from ..states.state_lumicell import Lumicell
from ..tools.scp_agent.display_studies import display_studies
from ..tools.scp_agent.search_studies import search_studies

# Initialize logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LUMICELL_PROMPT = """\
You are Lumicell, the single-cell transcriptomics specialist in Phronesis. You help researchers find \
relevant single-cell datasets and interpret cell types and marker genes.

<tools>
- search_studies: search the Broad Institute Single Cell Portal for studies matching a disease, tissue, \
or cell type. Use a short, specific search term (for example "pancreatic ductal adenocarcinoma" rather \
than a full sentence) and request only as many studies as the researcher needs.
- display_studies: show the most recent search results to the researcher as a table. Call it after \
search_studies so the researcher can see the studies.
- biocontext_panglaodb: look up canonical cell-type markers for a gene or cell type.
- biocontext_ensembl: resolve gene symbols to Ensembl identifiers and basic annotation.
- biocontext: general biomedical lookups when the specific tools above do not fit.
</tools>

<approach>
1. For dataset discovery, call search_studies, then display_studies, then summarize what the table shows \
(study names, cell and gene counts). Base the summary on the returned rows only.
2. For marker or cell-type questions, use biocontext_panglaodb or biocontext_ensembl and report what they \
return, naming the source.
3. If a search returns no studies, say so and suggest one or two broader or alternative search terms.
4. You cannot run clustering or differential expression here. If the researcher wants an analysis of their \
own data, explain that it requires an uploaded AnnData file (.h5ad), which the Phronesis research workflow \
analyzes with Scanpy.
</approach>

<rules>
- Report study metadata, identifiers, and counts exactly as returned; do not invent studies, cell counts, \
or markers.
- Treat marker genes as correlative evidence of cell identity, not proof of function or disease causation.
- Keep answers short and factual, and state clearly when a tool returned nothing useful.
</rules>
"""


def get_app(uniq_id, llm_model: BaseChatModel = None):
    """
    This function returns the langraph app.
    """

    def agent_scp_node(state: Lumicell):
        """
        This function calls the model.
        """
        logger.log(logging.INFO, "Creating SCP_Agent node with thread_id %s", uniq_id)
        # Get the messages from the state
        # messages = state['messages']
        # Call the model
        # inputs = {'messages': messages}
        response = model.invoke(state, {"configurable": {"thread_id": uniq_id}})
        # The response is a list of messages and may contain `tool calls`
        # We return a list, because this will get added to the existing list
        # return {"messages": [response]}
        return response

    # Define the tools
    tools = ToolNode(
        [
            search_studies,
            display_studies,
            BioContextPanglaoDBTool(),
            BioContextEnsemblTool(),
            BioContextTool(),
        ]
    )

    # Create the LLM
    # And bind the tools to it
    # model = ChatOpenAI(model="gpt-4o-mini", temperature=0).bind_tools(tools)

    if llm_model is None:
        model_name = os.getenv("AIAGENTS4PHARMA_LLM_MODEL", "gpt-4o-mini")
        llm = ChatOpenAI(model=model_name, temperature=0)
    else:
        llm = llm_model

    model = create_react_agent(
        llm,
        tools=tools,
        state_schema=Lumicell,
        state_modifier=LUMICELL_PROMPT,
        checkpointer=MemorySaver(),
    )

    # Define a new graph
    workflow = StateGraph(Lumicell)

    # Define the two nodes we will cycle between
    workflow.add_node("agent_scp", agent_scp_node)

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.add_edge(START, "agent_scp")

    # Initialize memory to persist state between graph runs
    checkpointer = MemorySaver()

    # Finally, we compile it!
    # This compiles it into a LangChain Runnable,
    # meaning you can use it as you would any other runnable.
    # Note that we're (optionally) passing the memory when compiling the graph
    app = workflow.compile(checkpointer=checkpointer, name="Lumicell_Agent")
    logger.log(logging.INFO, "Compiled the graph")

    return app
