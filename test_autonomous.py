from strands import Agent, tool
from strands.models import BedrockModel


@tool
def get_upcoming_appointments() -> str:
    """Return upcoming family appointments from the care records."""
    return """
    Raj Sharma | Cardiology | 10 days away
    Meera Sharma | General Medicine | 30 days away
    """


@tool
def prepare_appointment(person: str) -> str:
    """Prepare an appointment packet for a family member."""
    return f"Appointment preparation started for {person}."


model = BedrockModel(
    model_id="amazon.nova-pro-v1:0",
    region_name="us-east-1",
)

agent = Agent(
    model=model,
    tools=[get_upcoming_appointments, prepare_appointment],
    system_prompt="""
You are a proactive Family Care Coordinator.

Check upcoming appointments.

RULE:
- If an appointment is exactly 10 days away, prepare it using
  prepare_appointment.
- If it is more than 10 days away, take no action.
- Never invent appointments or dates.
""",
)

agent(
    "Perform today's scheduled family care check. "
    "Take action only when required."
)