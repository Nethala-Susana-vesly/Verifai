# Verifai — Autonomous Research & Report Agent

Give it a topic. It plans sub-questions, searches the live web, cross-checks
facts across sources, writes a structured report, reviews its own draft for
gaps (re-searching if needed), and exports a cited PDF — with the whole
process streamed live to the browser.

## How it works (pipeline)

1. **Plan** — breaks the topic into 3–4 focused sub-questions
2. **Search** — queries the web (Tavily) for each sub-question
3. **Extract** — pulls concrete, source-attributed facts from results
4. **Cross-check** — flags any facts that contradict each other
5. **Synthesize** — drafts the full report from gathered facts
6. **Self-review** — the agent critiques its own draft; if it finds real
   gaps, it runs one more targeted search round before finalizing
7. **Render** — outputs a formatted PDF with a source list

Steps 1, 6, and the re-search decision are genuinely agentic — the model
decides how to proceed, not a fixed script.

## Setup

```bash
cd research_agent
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and add two free API keys:
- **Groq** — https://console.groq.com (used for all the LLM reasoning steps)
- **Tavily** — https://tavily.com (used for live web search; free tier covers 1000 searches/month)

Run it:
```bash
python app.py
```
Open http://localhost:5000

## Project structure

```
research_agent/
├── app.py           Flask routes + SSE streaming for real-time progress
├── agent.py         The actual agent pipeline (planning, search, review, PDF)
├── database.py      SQLite storage for report history
├── templates/
│   └── index.html
├── static/
│   ├── style.css
│   └── script.js
├── reports/         Generated PDFs land here
└── requirements.txt
```

## Notes for demos / interviews

- The agent log panel shows every step live via Server-Sent Events (SSE) —
  not a spinner. This is what makes the "agentic" behavior visible rather
  than a black box.
- If the self-review step finds the draft incomplete, you'll see a second
  search round happen live — that's the clearest demonstration that this
  isn't a single fixed prompt.
- Be honest in interviews about what's a genuine model decision (sub-question
  count, re-search trigger) vs. a fixed step (PDF formatting) — see the
  pipeline list above.

## Known limitations (worth mentioning if asked)

- Free-tier Tavily search quality varies by topic; very niche topics may
  return thin results.
- The self-review loop is capped at one extra round to keep runtime and API
  costs predictable.
- No user accounts — report history is local to whoever runs the server.
