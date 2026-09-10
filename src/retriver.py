from __future__ import annotations
from config import settings
import os
from schemas import RetrievalResult
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from prompt import SYSTEM_PROMPT
from google import genai
from google.genai import types, errors as genai_errors
import time # using it for model eval - speed performance


class Retriever:
    def __init__(self) -> None:
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.engine: Engine = create_engine(
            settings.DB_URL,
            pool_pre_ping=True,
        )
    def retrieve(self, question: str) -> RetrievalResult:
        if not question or not question.strip():
            raise ValueError("Question cannot be empty")
        chat = self.client.chats.create(
                model=settings.MODEL_NAME,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                )
        )    
        try :
            response = chat.send_message(question.strip())
        except genai_errors.ServerError:
            return {
                "question": question,
                "sql": None,
                "rows": [],
                "status": "service_unavailable",
            }
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
    text_input = input(":> ")
    start_time = time.perf_counter()
    result = retriever.retrieve(
        question=text_input
    )
    end_time = time.perf_counter()
    if result["status"] == "service_unavailable":
        print("Aap jis key ka upyog karna chahte hain, woh abhi uplabdh nahi hai, kuch der baad phir prayas karein.")
    elif result["status"] == "invalid_request":
        print("iska jawab mere pass nahi hai")
    else:
        print("SQL:")
        print(result["sql"])
        # time calculation for model's speed
        duration = end_time - start_time
        print(f"Model:{settings.MODEL_NAME}, Code Runtime: {duration:.2f} seconds")    
    #print("\nRows:")
    #print(result["rows"])
