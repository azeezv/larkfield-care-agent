"""Request/response models for the Larkfield support-agent API (pydantic v2)."""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

class Attachment(BaseModel):
    file_name: str
    content_type: str
    content_base64: str

class Ticket(BaseModel):
    ticket_id: str
    received_at: str
    channel: Literal["email"]
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    subject: str
    body: str
    attachments: List[Attachment] = []

TicketType = Literal["REFUND", "RETURN", "ORDER_STATUS", "DELIVERY_ISSUE", "OTHER"]
Outcome = Literal["PROCEED", "PROCEED_MODIFIED", "PEND", "ESCALATE", "REJECT", "INVALID_REQUEST", "UNABLE_TO_VERIFY"]
CheckResult = Literal["PASS", "FAIL", "NOT_APPLICABLE", "UNABLE_TO_VERIFY"]
ApproverRole = Optional[Literal["SUPPORT_LEAD", "TRUST_SAFETY", "CARRIER_CLAIMS"]]

class Resolved(BaseModel):
    order_id: Optional[str]
    customer_id: Optional[str]

class Evidence(BaseModel):
    source: str
    ref: str
    value: str

class Check(BaseModel):
    check_id: str
    result: CheckResult
    evidence: List[Evidence] = []
    policy_clause: Optional[str]

class DraftReply(BaseModel):
    to: str
    subject: str
    body: str

class ModelCall(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int

class DocumentUsed(BaseModel):
    document: str
    version: str

class Trace(BaseModel):
    model_calls: List[ModelCall]
    tools_called: List[str]
    documents_used: List[DocumentUsed]
    cost_usd: float

class TicketResponse(BaseModel):
    ticket_id: str
    run_id: str
    ticket_type: TicketType
    resolved: Resolved
    outcome: Outcome
    reason_codes: List[str]
    checks: List[Check]
    human_approval_required: bool
    approver_role: ApproverRole
    draft_reply: DraftReply
    trace: Trace

class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    approver: str

class LedgerEntry(BaseModel):
    run_id: str
    ticket_id: str
    action: Literal["REFUND", "REPLY", "ESCALATION"]
    amount_usd: Optional[float]
    status: Literal["PROPOSED", "APPROVED", "REJECTED"]
    recorded_at: str
