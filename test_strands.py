from strands import Agent, tool
from strands.models import BedrockModel


@tool
def get_upcoming_appointment(person: str) -> str:
    """Return the upcoming appointment for a family member."""
    if person.lower() == "raj":
        return "Raj has a cardiology follow-up in 10 days."

    return f"No upcoming appointment found for {person}."


model = BedrockModel(
    model_id="amazon.nova-pro-v1:0",
    region_name="us-east-1",
)

agent = Agent(
    model=model,
    tools=[get_upcoming_appointment],
)

agent(
    "Does Raj have an upcoming appointment? "
    "Use the available tool to find out."
)