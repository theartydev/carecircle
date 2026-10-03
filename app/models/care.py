from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Medicine:
    name: str
    dosage: Optional[str]
    frequency: Optional[str]
    source_document: str


@dataclass
class FollowUp:
    instruction: str
    appointment_date: Optional[str] = None
    requires_human_clarification: bool = False


@dataclass
class MedicalDocument:
    document_id: str
    patient_id: str
    document_type: str
    document_date: Optional[str]
    doctor: Optional[str]
    specialty: Optional[str]
    medicines: list[Medicine] = field(default_factory=list)
    requested_tests: list[str] = field(default_factory=list)
    follow_up: Optional[FollowUp] = None
    storage_path: Optional[str] = None


@dataclass
class FamilyMember:
    patient_id: str
    name: str
    relationship: str
    care_circle: list[str] = field(default_factory=list)