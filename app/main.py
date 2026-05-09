import os
import json
import re
import time
import logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from groq import Groq
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Load catalog ──────────────────────────────────────────────────────────────
CATALOG_PATH = Path(__file__).parent.parent / "data" / "shl_catalog.json"

with open(CATALOG_PATH) as f:
    SHL_CATALOG = json.load(f)

CATALOG_BY_NAME = {item["name"]: item for item in SHL_CATALOG}
VALID_URLS = {item["url"] for item in SHL_CATALOG}

logger.info(f"Catalog loaded: {len(SHL_CATALOG)} assessments, {len(VALID_URLS)} unique URLs.")

# ── Groq setup ────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise EnvironmentError("GROQ_API_KEY environment variable not set.")

groq_client = Groq(api_key=GROQ_API_KEY)
logger.info("Groq client configured successfully.")

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="SHL Assessment Recommender",
    description="Conversational AI agent to help HR managers choose SHL assessments.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class Message(BaseModel):
    role: str
    content: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")
        return v


class ChatRequest(BaseModel):
    messages: list[Message]

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, msgs: list[Message]) -> list[Message]:
        if not msgs:
            raise ValueError("messages cannot be empty")
        if len(msgs) > 16:
            raise ValueError("Too many messages (max 16)")
        return msgs


class Recommendation(BaseModel):
    name: str
    url: str
    test_type: str


class ChatResponse(BaseModel):
    reply: str
    recommendations: list[Recommendation]
    end_of_conversation: bool


# ── Catalog formatting ────────────────────────────────────────────────────────
def format_catalog() -> str:
    lines = []
    for item in SHL_CATALOG:
        types = ", ".join(item["test_type"])
        langs = ", ".join(item.get("languages", [])[:5])
        if len(item.get("languages", [])) > 5:
            langs += f" (+{len(item['languages']) - 5} more)"
        line = (
            f"NAME: {item['name']}\n"
            f"  URL: {item['url']}\n"
            f"  TYPE: {types} | DURATION: {item.get('duration','--')} | LANGUAGES: {langs}\n"
            f"  DESC: {item['description'][:200]}\n"
            f"  KEYWORDS: {', '.join(item.get('keywords', [])[:10])}\n"
        )
        lines.append(line)
    return "\n".join(lines)


CATALOG_TEXT = format_catalog()

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = f"""You are an expert SHL Assessment Recommender. You help HR managers and recruiters choose the right SHL Individual Test Solutions for their hiring needs.

## YOUR IDENTITY AND SCOPE
- You ONLY discuss SHL assessments from the catalog provided below.
- You do NOT give general hiring advice, salary advice, legal advice, regulatory compliance advice, or competitor comparisons.
- You do NOT answer off-topic questions. Politely redirect: "I can only help with SHL assessment selection."
- You do NOT interpret whether an assessment satisfies a legal or regulatory obligation.

## CONVERSATION BEHAVIOURS (follow strictly)

### 1. CLARIFY before recommending
If the query is too vague (no role, no level, no context), ask ONE targeted clarifying question. Do not recommend on the first turn if the query lacks enough information.
- "I need an assessment" - Ask: what role, what level?
- "We need something for leadership" - Ask: is this for selection or development?
- "We are hiring agents" - Ask: what language will calls be in?

### 2. RECOMMEND when you have enough context
Once you have sufficient context (role + level, or JD provided), recommend 1 to 10 assessments.

Key recommendation principles:
- For SENIOR/EXECUTIVE roles: OPQ32r is almost always appropriate. Add OPQ Leadership Report for leadership selection.
- For COGNITIVE assessment: SHL Verify Interactive G+ is the default for professional roles. SHL Verify Interactive Numerical Reasoning for finance/analyst roles.
- For GRADUATE hiring: Graduate Scenarios + Verify G+ + OPQ32r is the standard battery.
- For TECHNICAL roles: include relevant knowledge test(s) + Verify G+ + OPQ32r.
- For CONTACT CENTRE high-volume: SVAR language screen then simulation then personality.
- For SAFETY-CRITICAL industrial: DSI or Safety and Dependability 8.0.
- For OFFICE admin roles: MS Excel/Word knowledge tests. If simulation needed, use the 365 versions.

### 3. REFINE gracefully when user changes constraints
Update the shortlist. Do not start over. Carry forward unchanged items and modify as instructed.

### 4. COMPARE when asked
Draw only from catalog data. Explain what each measures, who it is for, how long it takes.

### 5. ACKNOWLEDGE catalog gaps honestly
If no specific test exists (e.g. Rust, Go), say so and offer closest alternatives. Never invent assessments.

### 6. REFUSE off-topic requests politely
- Legal questions: "Those are legal compliance questions. Your legal team is the right resource."
- General hiring advice: "I can only help with SHL assessment selection."

## OUTPUT FORMAT - CRITICAL - DO NOT DEVIATE
Respond ONLY with a valid JSON object. No markdown fences. No text outside JSON.

{{
  "reply": "<your conversational reply>",
  "recommendations": [
    {{
      "name": "<EXACT name from catalog>",
      "url": "<EXACT url from catalog>",
      "test_type": "<type codes e.g. K or P or A>"
    }}
  ],
  "end_of_conversation": false
}}

RULES:
- recommendations is [] when still clarifying, comparing only, or refusing.
- recommendations has 1 to 10 items when you have committed to a shortlist.
- end_of_conversation is true ONLY when user confirms they are satisfied.
- Every name and URL MUST match catalog exactly. Do not invent or modify them.

## SHL CATALOG
{CATALOG_TEXT}
"""


# ── Validate recommendations ──────────────────────────────────────────────────
def validate_recommendations(recs: list) -> list[Recommendation]:
    valid = []
    for rec in recs:
        name = str(rec.get("name", "")).strip()
        url = str(rec.get("url", "")).strip()
        test_type = str(rec.get("test_type", "")).strip()

        if url not in VALID_URLS:
            logger.warning(f"Invalid URL '{url}' - trying rescue by name '{name}'")
            matched = CATALOG_BY_NAME.get(name)
            if matched:
                url = matched["url"]
                test_type = ", ".join(matched["test_type"])
            else:
                logger.warning(f"Dropping hallucinated entry: {name}")
                continue

        if name not in CATALOG_BY_NAME:
            matched_by_url = next(
                (item for item in SHL_CATALOG if item["url"] == url), None
            )
            if matched_by_url:
                name = matched_by_url["name"]
            else:
                continue

        valid.append(Recommendation(name=name, url=url, test_type=test_type))

    seen_urls = set()
    deduped = []
    for r in valid:
        if r.url not in seen_urls:
            seen_urls.add(r.url)
            deduped.append(r)

    return deduped[:10]


# ── Parse LLM response ────────────────────────────────────────────────────────
def parse_llm_response(raw: str) -> ChatResponse:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in LLM output: {raw[:300]}")

    data = json.loads(match.group())
    reply = data.get("reply", "I could not generate a response. Please try again.")
    raw_recs = data.get("recommendations") or []
    end_flag = bool(data.get("end_of_conversation", False))

    if not isinstance(raw_recs, list):
        raw_recs = []

    validated = validate_recommendations(raw_recs)
    return ChatResponse(
        reply=reply,
        recommendations=validated,
        end_of_conversation=end_flag,
    )


# ── Call Groq ─────────────────────────────────────────────────────────────────
def call_groq(prompt: str) -> str:
    """
    Calls Groq API using llama-3.3-70b model.
    Fast, free, works perfectly in India.
    """
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": "You are an expert SHL Assessment Recommender. Always respond with valid JSON only. No markdown. No extra text outside the JSON object."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.2,
        max_tokens=1024,
    )
    return response.choices[0].message.content


# ── API endpoints ─────────────────────────────────────────────────────────────
@app.get("/health", summary="Health check")
def health_check():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse, summary="Chat with the recommender")
def chat(request: ChatRequest) -> ChatResponse:
    start = time.time()

    try:
        # Build conversation string
        parts = []
        for msg in request.messages:
            role = "USER" if msg.role == "user" else "ASSISTANT"
            parts.append(f"{role}: {msg.content}")
        conversation = "\n\n".join(parts)

        # Build full prompt
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"---\n\n"
            f"CONVERSATION:\n{conversation}\n\n"
            f"ASSISTANT (respond with JSON only):"
        )

        raw_text = call_groq(prompt)
        logger.info(f"Groq response (first 250 chars): {raw_text[:250]}")

        result = parse_llm_response(raw_text)

        elapsed = time.time() - start
        logger.info(
            f"Done in {elapsed:.2f}s | "
            f"Recs: {len(result.recommendations)} | "
            f"End: {result.end_of_conversation}"
        )

        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return ChatResponse(
            reply="I had a formatting issue. Could you please rephrase your request?",
            recommendations=[],
            end_of_conversation=False,
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
