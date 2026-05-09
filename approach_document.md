# Approach Document — SHL Assessment Recommender
**Role:** AI Research Intern | **Date:** May 2026

---

## 1. Problem Decomposition

The core challenge: convert a recruiter's vague natural-language intent into a grounded shortlist from a fixed catalog — without inventing assessments that don't exist, and while handling the full complexity of real hiring conversations (clarification, refinement, comparison, edge cases, off-topic refusals).

I decomposed this into four sub-problems:
1. **Catalog representation** — structure 37 verified SHL Individual Test Solutions for reliable LLM retrieval
2. **Conversation management** — handle clarify / recommend / refine / compare / refuse behaviours
3. **Hallucination prevention** — guarantee every returned URL is real
4. **Schema enforcement** — machine-parseable JSON output on every turn

---

## 2. Catalog

I built the catalog from two sources: (a) the official conversation traces, which provided exact assessment names, URLs, durations, and languages as used by the expected agent, and (b) the live SHL product catalog page. The traces were the primary source — they provided ground truth for 35 assessments with verified URLs, test types, and durations.

Each catalog entry includes name, URL, description, test type codes, duration, languages, job levels, job families, and a keyword list for retrieval context.

---

## 3. Design Choices

### Retrieval: Catalog Embedded in System Prompt

| Approach | Pros | Cons |
|----------|------|------|
| Vector store (FAISS/Chroma) | Scales to millions | Retrieval errors cascade; adds complexity |
| Catalog in system prompt | 100% recall, no retrieval errors, simpler | Token cost (acceptable — ~20K tokens, fits Gemini's 1M window) |

**Decision:** Embed the full catalog. With 37 assessments, the token cost is trivial and the benefit — the model always sees all options — is significant for Recall@10.

### LLM: Gemini 1.5 Flash
- Free tier, 1M context window, fast inference (<2s typical)
- Strong instruction-following for structured JSON output
- Temperature 0.2: grounded and consistent

### API: Stateless
Full conversation history sent on every `POST /chat`. No session state. Matches the spec exactly and enables horizontal scaling.

### Output: Forced JSON + URL Whitelist
The system prompt mandates JSON-only output. After parsing, every URL is checked against a whitelist loaded from the catalog at startup. Invalid URLs are either rescued by name lookup or dropped entirely. This makes hallucination of fake assessments impossible in the final response.

---

## 4. Prompt Engineering (Key Decisions from Traces)

Reading all 10 conversation traces revealed critical agent behaviours I encoded into the system prompt:

- **Do not recommend on Turn 1 if vague** — the traces show the agent always clarifies first for ambiguous queries
- **Ask ONE targeted question** — not multiple questions at once
- **Honest catalog gaps** — C2 (Rust engineer) shows the agent explicitly saying "no Rust test in catalog" and offering alternatives
- **Refuse legal/compliance questions** — C7 (HIPAA) shows the agent explicitly refusing to advise on regulatory obligations
- **OPQ32r is the default personality instrument** — appears in 8 of 10 traces
- **Verify G+ is the default cognitive test** — adaptive, appropriate for all professional levels
- **Graduate Scenarios is the SJT for graduate hiring**
- **Safety roles need personality predictors (DSI/Safety 8.0), not just knowledge tests** — C6 explains this explicitly

---

## 5. Evaluation Coverage

| Hard Eval | How Handled |
|-----------|-------------|
| Schema compliance | Pydantic enforced on every response |
| Catalog-only URLs | URL whitelist validator; hallucinated entries dropped |
| Turn cap (max 8) | Stateless design; client controls history |

| Behaviour Probe | How Handled |
|----------------|-------------|
| Vague query → clarify, no T1 recommendation | Explicit rule in system prompt |
| Refine mid-conversation | Full history sent each call; prompt instructs updating not restarting |
| Compare assessments | Catalog descriptions enable grounded comparisons |
| Off-topic refusal | Prompt enumerates: salary, legal, competitors → redirect |
| Honest catalog gaps | Prompt instructs: acknowledge missing, offer alternatives |
| Hallucination rate | URL whitelist eliminates all hallucinated URLs before response |

---

## 6. What Didn't Work

1. **Temperature 0.5**: Occasionally generated plausible-sounding but invented assessment names. Fixed → temperature 0.2.
2. **Recommending on Turn 1 for any query with a JD**: Without explicit instruction, the model would skip clarification even for ambiguous JDs. Fixed → added explicit rule to clarify backend vs frontend lean before committing.
3. **Markdown code fences in JSON output**: LLM occasionally wrapped JSON in \`\`\`json blocks. Fixed → regex stripping before parse.
4. **OPQ report outputs confusing the model**: The model sometimes treated OPQ UCF Report and OPQ Leadership Report as standalone assessments to administer. Fixed → catalog descriptions clarify these are report outputs from a single OPQ32r administration.

---

## 7. Tools Used

- **Claude (Anthropic)** — Architecture review and test case brainstorming
- **Google AI Studio** — Gemini API key and prompt iteration
- All code written and understood by me; every design choice is defensible in interview

## 8. Deployment

Deployed on **Render** (free tier):
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Env var: `GEMINI_API_KEY`
- Cold start < 60s. Response time < 3s per turn.
