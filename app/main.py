"""FastAPI application entry point."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import FhirError
from app.routers import bundle, reassign, resources

app = FastAPI(
    title="Laboratory FHIR Research Ingest API",
    version="1.0.0",
    description=(
        "Ingests FHIR R4 transaction Bundles (Patient / Observation / "
        "DiagnosticReport) into the research library with idempotent, "
        "atomic, patient-scoped semantics. Try POST /fhir from this page. "
        "Misfiled reports can be moved to the correct patient via "
        "POST /diagnostic-reports/{report_id}/reassign."
    ),
)

app.include_router(bundle.router)
app.include_router(reassign.router)
app.include_router(resources.router)


@app.exception_handler(FhirError)
async def fhir_error_handler(request: Request, exc: FhirError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_outcome(),
        media_type="application/fhir+json",
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Render request-shape errors as OperationOutcome like every other error.
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", []))
    diagnostics = f"{location}: {first.get('msg', 'invalid request')}"
    return JSONResponse(
        status_code=422,
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "invalid",
                    "details": {"text": "request-validation"},
                    "diagnostics": diagnostics[:300],
                }
            ],
        },
        media_type="application/fhir+json",
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak a stack trace; the transaction is rolled back by the session
    # dependency, so no partial writes survive an unexpected failure.
    return JSONResponse(
        status_code=500,
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "exception",
                    "details": {"text": "internal-error"},
                    "diagnostics": str(exc)[:300],
                }
            ],
        },
        media_type="application/fhir+json",
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "lab-fhir-research-ingest",
        "docs": "/docs",
        "submit": "POST /fhir (header Idempotency-Key required)",
    }
