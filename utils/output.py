"""Extract the user-facing answer without printing graph state or metadata."""


def _text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
    return ""


def format_result(response):
    """Handle SQL answers and the router's nested SQL/ETL message states."""
    if isinstance(response, dict):
        answer = _text(response.get("final_answer"))
        if answer:
            return answer
        for message in reversed(response.get("messages", [])):
            answer = format_result(message)
            if answer:
                return answer
        if response.get("type") == "ai" and not response.get("tool_calls"):
            return _text(response.get("content"))
    elif getattr(response, "type", None) == "ai" and not getattr(response, "tool_calls", None):
        return _text(response.content)
    return ""
