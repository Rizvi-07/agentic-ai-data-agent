import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents import sql_analyst
from utils.llm_pick import pick_llm
from utils.output import format_result
from utils.etl_tools import ETLTools
from Models.schema import RouterSchema, DataAgentSchema
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langchain.tools import tool
from langchain_anthropic import ChatAnthropic
from agents.etl_analyst import etl_analyst
from agents.sql_analyst import sql_analyst


llm = pick_llm("claude")

llm_router = llm.with_structured_output(RouterSchema)

ROUTER_INSTRUCTIONS = """Classify the user's data task.
Use etl for any request to extract, export or save data as a file (CSV, JSON,
Parquet), including data from a PostgreSQL table. For example, 'Extract top 5
data from ratings table and save it as CSV in data/extract' is etl.
The ETL agent has FastAPI tools for the connected PostgreSQL database.
Use sql for database questions, joins, filters, counts and summaries when no
file is requested. A named table refers to the connected PostgreSQL database
unless the user explicitly provides an external URL or local input file.
"""

# ---------------------------- DATA AGENT GRAPH ---------------------------- #


def router_node(state:DataAgentSchema):

    message = state.messages[-1].content

    route_response_dict = llm_router.invoke([
        SystemMessage(content=ROUTER_INSTRUCTIONS), HumanMessage(content=message)
    ]).model_dump()

    route_response = route_response_dict['answer']

    return {"route_response": route_response}

def etl_node(state:DataAgentSchema):

    message = state.messages[-1].content

    response = etl_analyst.invoke(
             {"messages": [HumanMessage(content=message)]},
             config={"recursion_limit": 16}
        ) 
    return {"messages": [response]}

def sql_node(state:DataAgentSchema):

    message = state.messages[-1].content

    input_schema = {
        "messages": [],
        "user_question": f"{message}",
        "curated_ques": "",
        "prompt_query_context": "",
        "generated_sql_query": "",
        "is_safe": "No",
        "comments": "",
        "sql_query_execution_result": "",
        "final_answer": ""
    }

    response = sql_analyst.invoke(input_schema)

    return {"messages": [response]}




data_agent_graph = StateGraph(DataAgentSchema)

data_agent_graph.add_node("router_node", router_node)
data_agent_graph.add_node("etl_node", etl_node)
data_agent_graph.add_node("sql_node", sql_node)

data_agent_graph.add_edge(START, "router_node")

def route_edge(state: DataAgentSchema) -> str:
    if state.route_response == "sql":
        return "sql_node"
    elif state.route_response == "etl":
        return "etl_node"
    else:
        raise ValueError(f"Invalid route response: {state.route_response}")


data_agent_graph.add_conditional_edges("router_node", route_edge,
                                      {
                                          "sql_node": "sql_node",
                                          "etl_node": "etl_node"
                                      })

data_agent = data_agent_graph.compile()

# Generate a graph image only when explicitly requested from the CLI.
# Importing this module for the website must not contact the diagram service.
if __name__ == "__main__" and "--graph" in sys.argv:
    from IPython.display import Image
    img = Image(data_agent.get_graph().draw_mermaid_png())
    with open("data_agent_graph.png", "wb") as f:
        f.write(img.data)



if __name__ == "__main__":

    # response = data_agent.invoke(
    #     {"messages":[HumanMessage(content="I want to extract the data from the API endpoint 'https://pokeapi.co/api/v2/pokemon' and save it to data/extract folder in the csv folder")],
    #      "route_response": ""}
    # )
    # response = data_agent.invoke(
    #         {"messages":[HumanMessage(content="Show me the average rating for each vehicle type")],
    #          "route_response": ""}
    #     )
    # response = data_agent.invoke(
    #             {"messages":[HumanMessage(content="What are the different kind of payment methods available")],
    #              "route_response": ""}
    #         )
    # response = data_agent.invoke(
    #             {"messages":[HumanMessage(content="Show completed payment total, grouped by payment method.")],
    #              "route_response": ""}
    #         )
    response = data_agent.invoke(
                    {"messages":[HumanMessage(content="Show completed payment total, grouped by payment method and make a csv file and save it to data/extract folder in the csv folder")],
                     "route_response": ""}
                )
    print(format_result(response))




