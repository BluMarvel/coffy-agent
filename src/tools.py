import logging

from langchain_community.tools import DuckDuckGoSearchResults
from livekit.agents import RunContext, function_tool


@function_tool
async def search_web(context: RunContext, query: str):
    """
    Use this tool to search the web for information related to the given query.
    """

    try:
        results = DuckDuckGoSearchResults().run(tool_input=query)
        logging.info(f"web search for query '{query}': {len(results)} characters")
        return results
    except Exception as e:
        logging.error(
            f"Error occurred while searching the web for query '{query}': {e}"
        )
        raise
