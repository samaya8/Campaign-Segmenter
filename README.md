# Campaign Segmenter

Project 1 of the AI Engineer sprint: Python, SQL, K-Means and a first LLM app for marketing campaigns.

## Status

- [x] **Day 1** — Set up and make the first LLM call (ad headlines as validated JSON)
- [ ] Day 2 — SQL on real campaign data
- [ ] Day 3 — Split customers into groups
- [ ] Day 4 — Write an email for each group
- [ ] Day 5 — Finish Project 1 (FastAPI + Docker)

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env            # then paste your Gemini API key
```

## Day 1: ad headline generator

```bash
python headlines.py "Noise-cancelling wireless earbuds with 30-hour battery life"
```

Output (validated by Pydantic before it is printed):

```json
{
  "product": "Wireless Earbuds",
  "headlines": [
    { "text": "...", "angle": "benefit" },
    { "text": "...", "angle": "urgency" },
    { "text": "...", "angle": "social proof" }
  ]
}
```

How it stays valid every time:

1. Gemini is called with `response_mime_type="application/json"` and the Pydantic model as `response_schema`.
2. The raw text is re-checked with `HeadlineSet.model_validate_json` (exactly 3 headlines, each ≤ 90 chars).
3. If validation fails, the call is retried (up to 3 attempts).

## Marketing terms

| Term | Meaning | Formula |
|------|---------|---------|
| **CTR** — Click-through rate | Share of people who saw the ad and clicked it | clicks ÷ impressions |
| **CVR** — Conversion rate | Share of clicks that turned into a sale or sign-up | conversions ÷ clicks |
| **CPC** — Cost per click | What each click cost | spend ÷ clicks |
| **CPA** — Cost per acquisition | What each conversion cost | spend ÷ conversions |
| **ROAS** — Return on ad spend | Revenue earned per unit of spend | revenue ÷ spend |

Example: 10,000 impressions, 200 clicks, 10 sales, $100 spend, $500 revenue →
CTR 2%, CVR 5%, CPC $0.50, CPA $10, ROAS 5.0×.
