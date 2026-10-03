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
You are a family care document extraction agent.

Extract ONLY information explicitly present in the source document.
Never infer, guess, add medical knowledge, or invent missing information.

Return exactly these fields:

patient_name:
document_date:
doctor:
specialty:
prescribed_medicines:
requested_tests:
follow_up_instruction:

If a value is not present, return null.
Preserve medicine dosage and frequency exactly as written.
""",
)

agent(
    "Read test_data/raj_prescription.txt and extract the required information."
)