from strands import Agent, tool
from strands.models import BedrockModel


@tool
def read_document(file_path: str) -> str:
    """Read a local text document and return its exact contents."""
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


model = BedrockModel(
    model_id="amazon.nova-pro-v1:0",
    region_name="us-east-1",
)

agent = Agent(
    model=model,
    tools=[read_document],
    system_prompt="""
You are a family care coordination agent.

Extract ONLY information explicitly present in the source document.
Never infer, guess, add medical knowledge, or invent missing information.

For follow-up instructions:
- If an exact date is explicitly provided, extract it.
- If a deterministic duration is provided, such as "after 4 weeks",
  preserve that instruction.
- If timing is vague, such as "next month", "later", or "soon",
  do NOT calculate or invent a date.
- Instead return:
  requires_human_clarification: true
  clarification_reason: <brief reason>

Otherwise return:
  requires_human_clarification: false

Return patient, medicines, follow-up information, and clarification status.
""",
)

agent(
    "Read test_data/ambiguous_prescription.txt and extract the follow-up information."
)