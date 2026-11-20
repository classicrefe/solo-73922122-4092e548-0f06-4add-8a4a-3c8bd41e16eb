"""Reassignment (转挂) endpoint: move a misfiled report to the right patient."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_session
from app.errors import bad_request
from app.fhir_utils import parse_etag
from app.services.reassign import reassign_report

router = APIRouter(tags=["reassign"])


class ReassignRequest(BaseModel):
    expectedVersion: int | str = Field(
        ...,
        description=(
            "Report version seen while verifying the mistake, as an integer "
            '(2) or an ETag string (W/"2").'
        ),
        examples=[2],
    )
    targetPatientId: str = Field(
        ...,
        min_length=1,
        description="Id of the patient the report actually belongs to.",
        examples=["b7f1c2d4-0000-4000-8000-000000000002"],
    )


@router.post("/diagnostic-reports/{report_id}/reassign")
def reassign_diagnostic_report(
    report_id: str,
    body: ReassignRequest,
    session: Session = Depends(get_session),
):
    """Reassign a DiagnosticReport (and its observations) to another patient.

    * Reports referencing more than 8 observations are rejected (422).
    * Observations referenced only by this report keep their ids and move
      along; observations shared with other reports are copied for the
      target patient and this report is re-pointed at the copies.
    * Report body, links and new resource versions commit together; the
      receipt lists every observation's before/after ids.
    * A stale ``expectedVersion`` is rejected (412); on any failure nothing
      is left behind, and a concurrency conflict returns 409 so the clerk
      can retry.
    """
    version = body.expectedVersion
    if isinstance(version, str):
        version = parse_etag(version)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise bad_request(
            'expectedVersion must be a positive integer version or an ETag like W/"2".'
        )
    target = body.targetPatientId.strip()
    if not target:
        raise bad_request("targetPatientId must be a non-empty patient id.")
    return reassign_report(session, report_id, version, target)
