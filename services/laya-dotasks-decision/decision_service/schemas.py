from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Candidate(StrictModel):
    id: Annotated[str, Field(min_length=1, max_length=128)]
    text: Text


class HistoryRequest(StrictModel):
    query: Text
    candidates: Annotated[list[Candidate], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({item.id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate ids must be unique")
        return self


class FailureRequest(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=6000)]


class AssignmentRequest(HistoryRequest):
    task_revision: Annotated[int, Field(ge=1)]
    candidate_version: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class Metadata(StrictModel):
    schema_version: Literal[1] = 1
    policy_version: str
    model_id: str
    model_revision: str
    runtime_version: str
    request_id: str
    latency_ms: Annotated[int, Field(ge=0)]
    advisory_only: Literal[True] = True
    truncated: Literal[False] = False


class RankedCandidate(StrictModel):
    id: str
    relevance: Probability


class HistoryResponse(Metadata):
    score_kind: Literal["ordinal_0_1"] = "ordinal_0_1"
    candidates: list[RankedCandidate]


class AssignmentResponse(HistoryResponse):
    task_revision: int
    candidate_version: str


Category = Literal["environment", "project", "implementation", "unknown"]


class FailureResponse(Metadata):
    category: Category
    probabilities: dict[Category, Probability]
