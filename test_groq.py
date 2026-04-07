#!/usr/bin/env python3
"""Test rapide validation prix Groq / fallback local."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.ai_supervisor import AISupervisor

if __name__ == "__main__":
    supervisor = AISupervisor()
    cs = supervisor.control_status()
    print("groq_client_ready:", cs.get("groq_client_ready"))
    print("GROQ_API_KEY set:", bool(os.getenv("GROQ_API_KEY")))

    result = supervisor.validate_prices("Air Force 1", "Nike", [89.99, 95.0, 92.5, 88.0])
    print("Test 1 (prix normaux):", result)

    result = supervisor.validate_prices("Yeezy 350", "Adidas", [15.0, 180.0, 9999.0, 175.0])
    print("Test 2 (prix aberrants):", result)

    result = supervisor.validate_prices("NB 550", "New Balance", [])
    print("Test 3 (vide):", result)
