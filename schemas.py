from pydantic import BaseModel
class RetrievalResult(BaseModel):
    question: str
    sql: str | None
    rows: list[dict]
    status: str