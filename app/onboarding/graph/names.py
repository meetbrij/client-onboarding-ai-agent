"""Node names, shared by the graph, the audit trajectory and (later) the eval expectations and Langfuse spans."""

INTAKE = "intake"
EXTRACT = "extract"
SCREEN = "screen"
ASSESS = "assess"
APPROVE = "approve"
EXECUTE = "execute"
AWAIT_DOCS = "await_docs"
EXECUTED = "executed"

# Audit event types that mark progress through the workflow
NODE_COMPLETED = "node_completed"
APPROVAL_REQUESTED = "approval_requested"  # the graph paused at `approve`
DOCUMENTS_REQUESTED = "documents_requested"  # the graph paused at `await_docs`
