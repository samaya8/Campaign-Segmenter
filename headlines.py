"""Day 1: give the LLM a product description, get back 3 ad headlines as validated JSON.

Usage:
    python headlines.py "Noise-cancelling wireless earbuds with 30-hour battery life"
    python headlines.py            # uses a built-in sample description
"""

import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_ATTEMPTS = 3
SAMPLE_PRODUCT = "Noise-cancelling wireless earbuds with 30-hour battery life and a pocket-sized charging case."


class Headline(BaseModel):
    text: str = Field(min_length=3, max_length=90, description="The ad headline, at most 90 characters.")
    angle: str = Field(min_length=3, description="The persuasion angle, e.g. benefit, urgency, social proof.")


class HeadlineSet(BaseModel):
    product: str = Field(description="Short product name.")
    headlines: list[Headline] = Field(min_length=3, max_length=3)


PROMPT = """You are a performance marketing copywriter.
Write exactly 3 distinct ad headlines for the product below.
Each headline must be at most 90 characters and use a different persuasion angle.

Product description:
{description}
"""


def generate_headlines(description: str, client: genai.Client | None = None) -> HeadlineSet:
    """Call the LLM and return headlines validated by Pydantic. Retries on invalid output."""
    client = client or genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.models.generate_content(
            model=MODEL,
            contents=PROMPT.format(description=description),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=HeadlineSet,
                temperature=0.9,
            ),
        )
        try:
            # Validate the raw JSON text ourselves rather than trusting the SDK's parse.
            return HeadlineSet.model_validate_json(response.text or "")
        except ValidationError as err:
            last_error = err
            print(f"Attempt {attempt}: invalid JSON from model, retrying...", file=sys.stderr)

    raise RuntimeError(f"Model did not return valid JSON after {MAX_ATTEMPTS} attempts") from last_error


def main() -> None:
    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY is not set. Add it to your .env file (see .env.example).")

    description = " ".join(sys.argv[1:]).strip() or SAMPLE_PRODUCT
    result = generate_headlines(description)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
