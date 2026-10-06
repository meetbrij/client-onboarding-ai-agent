import os

# Checkpoints are deserialised only for known types (MIA D-24). Set before LangGraph is imported anywhere.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
