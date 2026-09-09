from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from prompt import SYSTEM_PROMPT
from google import genai
from google.genai import types

class Retriever:
    def __init__(self) -> None:
        self.client = genai.Client()
        self.engine: Engine = create_engine(
            os.environ["DB_URL"],
            pool_pre_ping=True,
        )
    def retrieve(self, question: str) -> dict:
        if not question or not question.strip():
            raise ValueError("Question cannot be empty")
        chat = self.client.chats.create(
                model="gemini-3.6-flash",
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                )
        )    
        response = chat.send_message(question.strip())
        sql = response.text.strip()
        if sql == "INVALID_REQUEST":
            return {
                "question": question,
                "sql": None,
                "rows": [],
                "status": "invalid_request",
            }
        if not sql.upper().startswith("SELECT"):
            raise ValueError("Generated query is not a SELECT statement")
        if ";" in sql:
            raise ValueError("Multiple SQL statements are not allowed")
        with self.engine.connect() as connection:
            result = connection.execute(text(sql))
            rows = [dict(row) for row in result.mappings().all()]
        return {
            "question": question,
            "sql": sql,
            "rows": rows,
            "status": "success",
        }
if __name__ == "__main__":
    retriever = Retriever()
    result = retriever.retrieve(
        input(":> ")
    )
    print("SQL:")
    print(result["sql"])
    #print("\nRows:")
    #print(result["rows"])
