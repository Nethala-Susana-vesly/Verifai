"""
agent.py — core agentic pipeline for the Research & Report Agent.

Pipeline stages:
  1. plan_subquestions   -> LLM decomposes the topic into sub-questions
  2. search_subquestion  -> Tavily web search per sub-question
  3. extract_facts       -> LLM pulls concrete facts (with source) from search results
  4. cross_check_facts   -> LLM flags contradictions across facts
  5. synthesize_report   -> LLM drafts a structured report from all facts
  6. self_review         -> LLM checks its own draft for gaps; may trigger one extra
                            round of search on missing sub-topics
  7. render_pdf          -> ReportLab renders the final report to a PDF file

Every LLM call goes through `call_groq`, which enforces JSON-only output when
needed and retries once on transient failure. Every network call is wrapped
in try/except so a single failed source never crashes the whole run.
"""

import os
import re
import json
import time
import html
import requests

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TAVILY_URL = "https://api.tavily.com/search"
MODEL = "openai/gpt-oss-120b"


class AgentError(Exception):
    """Raised for unrecoverable pipeline errors (e.g. missing API keys)."""
    pass


def _check_keys():
    if not GROQ_API_KEY:
        raise AgentError("GROQ_API_KEY is not set. Add it to your .env file.")
    if not TAVILY_API_KEY:
        raise AgentError("TAVILY_API_KEY is not set. Add it to your .env file.")


def call_groq(messages, temperature=0.3, max_tokens=1500, retries=4):
    """Call the Groq chat completion endpoint. Returns the assistant's text.

    Handles 429 rate-limit responses specially: Groq tells us exactly how
    long to wait in the error body, so we parse and respect that instead of
    guessing with a fixed backoff (free-tier TPM limits are hit often enough
    in a multi-call pipeline like this one that this matters in practice).
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]

            if resp.status_code == 429:
                wait_seconds = _parse_retry_after(resp.text) or (2 * (attempt + 1))
                last_err = f"Groq rate limit hit: {resp.text[:200]}"
                if attempt < retries:
                    time.sleep(wait_seconds + 0.5)  # small buffer over the stated wait
                    continue
            else:
                last_err = f"Groq API returned {resp.status_code}: {resp.text[:300]}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise AgentError(f"Groq API call failed after retries: {last_err}")


def _parse_retry_after(error_text):
    """Extract the wait time Groq suggests from a 429 error body, if present."""
    match = re.search(r"try again in ([\d.]+)s", error_text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _extract_json(text):
    """Best-effort extraction of a JSON object/array from an LLM response."""
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: grab the first {...} or [...] block
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    raise AgentError("Could not parse JSON from model response.")


def plan_subquestions(topic, constraints="", max_questions=4):
    """Ask the LLM to break the topic into a small set of research sub-questions."""
    prompt = (
        f'Topic: "{topic}"\n'
        f'Constraints/audience: "{constraints or "none"}"\n\n'
        f"Break this into at most {max_questions} focused sub-questions whose answers, "
        "combined, would give a well-rounded understanding of the topic. "
        'Respond with ONLY a JSON array of strings, nothing else. Example: '
        '["question 1", "question 2"]'
    )
    text = call_groq(
        [
            {"role": "system", "content": "You are a meticulous research planner."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_tokens=300,
    )
    questions = _extract_json(text)
    if not isinstance(questions, list) or not questions:
        raise AgentError("Planner did not return a valid list of sub-questions.")
    return [str(q) for q in questions[:max_questions]]


def search_subquestion(subquestion, max_results=4):
    """Search the web for a sub-question via Tavily. Returns a list of result dicts."""
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": subquestion,
        "search_depth": "advanced",
        "include_raw_content": False,
        "max_results": max_results,
    }
    try:
        resp = requests.post(TAVILY_URL, json=payload, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title", "Untitled"),
                    "url": item.get("url", ""),
                    "content": item.get("content", "")[:2000],
                }
            )
        return results
    except requests.RequestException:
        return []


def extract_facts(subquestion, search_results):
    """Ask the LLM to pull concrete, attributable facts out of raw search results."""
    if not search_results:
        return {"subquestion": subquestion, "facts": [], "sources": []}

    sources_block = "\n\n".join(
        f"[Source {i+1}] {r['title']} ({r['url']})\n{r['content']}"
        for i, r in enumerate(search_results)
    )
    prompt = (
        f'Sub-question: "{subquestion}"\n\n'
        f"Sources:\n{sources_block}\n\n"
        "Extract the 3-6 most relevant, concrete facts that answer the sub-question. "
        "For each fact, note which source number(s) support it. "
        'Respond with ONLY JSON in this shape: '
        '{"facts": [{"statement": "...", "source_indices": [1,2]}]}'
    )
    text = call_groq(
        [
            {"role": "system", "content": "You are a precise research assistant. Only state facts actually present in the sources. Respond with ONLY the JSON object requested \u2014 no preamble, no explanation, no markdown fences."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=800,
    )
    try:
        parsed = _extract_json(text)
        facts = parsed.get("facts", []) if isinstance(parsed, dict) else []
    except AgentError:
        # The model didn't return parseable JSON for this sub-question.
        # Don't kill the whole pipeline over one bad extraction \u2014 continue
        # with no facts for this sub-question rather than failing the run.
        facts = []
    return {"subquestion": subquestion, "facts": facts, "sources": search_results}


def cross_check_facts(extracted_sections):
    """Ask the LLM to flag any facts that contradict each other across sections."""
    all_facts = []
    for section in extracted_sections:
        for f in section.get("facts", []):
            all_facts.append(f.get("statement", ""))
    if len(all_facts) < 2:
        return []

    numbered = "\n".join(f"{i+1}. {f}" for i, f in enumerate(all_facts) if f)
    prompt = (
        f"Here are facts gathered from research:\n{numbered}\n\n"
        "Identify any facts above that directly contradict each other. "
        'Respond with ONLY JSON: {"conflicts": [{"description": "...", "fact_numbers": [1,3]}]}. '
        'If there are no contradictions, respond with {"conflicts": []}.'
    )
    text = call_groq(
        [
            {"role": "system", "content": "You are a careful fact-checker."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=500,
    )
    try:
        parsed = _extract_json(text)
        return parsed.get("conflicts", []) if isinstance(parsed, dict) else []
    except AgentError:
        return []


def synthesize_report(topic, extracted_sections, conflicts):
    """Draft the full report body from all gathered facts."""
    sections_block = ""
    for section in extracted_sections:
        facts_text = "\n".join(f"- {f.get('statement', '')}" for f in section.get("facts", []))
        sections_block += f"\n### {section['subquestion']}\n{facts_text}\n"

    conflicts_block = ""
    if conflicts:
        conflicts_block = "\n\nNoted conflicting information:\n" + "\n".join(
            f"- {c.get('description', '')}" for c in conflicts
        )

    prompt = (
        f'Write a clear, well-organized research report on: "{topic}"\n\n'
        f"Use only the facts below — do not invent information.\n{sections_block}{conflicts_block}\n\n"
        "Structure the report with a short introduction, one section per sub-topic above "
        "(use the sub-question as a section heading, phrased as a statement), and a brief "
        "conclusion. Write in plain prose, no markdown symbols, sentence case headings."
    )
    return call_groq(
        [
            {"role": "system", "content": "You are a skilled technical writer producing a factual report."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
        max_tokens=1800,
    )


def self_review(topic, draft):
    """Ask the LLM to critique its own draft and flag any missing angles."""
    prompt = (
        f'Topic: "{topic}"\n\nDraft report:\n{draft[:3000]}\n\n'
        "Does this draft cover the topic adequately for a general reader? "
        'Respond with ONLY JSON: {"complete": true/false, "missing_topics": ["..."]}. '
        "List at most 2 missing_topics, only if genuinely important gaps exist."
    )
    text = call_groq(
        [
            {"role": "system", "content": "You are a strict editor reviewing a report for completeness."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=300,
    )
    try:
        parsed = _extract_json(text)
        return {
            "complete": bool(parsed.get("complete", True)),
            "missing_topics": parsed.get("missing_topics", [])[:2],
        }
    except AgentError:
        return {"complete": True, "missing_topics": []}


def render_pdf(filepath, topic, report_text, extracted_sections):
    """Render the final report + source list to a PDF file at `filepath`."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontSize=18, spaceAfter=14
    )
    heading_style = ParagraphStyle(
        "ReportHeading", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6
    )
    body_style = ParagraphStyle(
        "ReportBody", parent=styles["BodyText"], fontSize=10.5, leading=15
    )
    source_style = ParagraphStyle(
        "SourceStyle", parent=styles["BodyText"], fontSize=8.5, textColor="#555555"
    )

    doc = SimpleDocTemplate(filepath, pagesize=LETTER,
                             topMargin=0.9 * inch, bottomMargin=0.9 * inch)
    story = [Paragraph(html.escape(topic), title_style), Spacer(1, 10)]

    for para in report_text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        story.append(Paragraph(html.escape(para), body_style))
        story.append(Spacer(1, 8))

    story.append(Paragraph("Sources", heading_style))
    seen_urls = set()
    counter = 1
    for section in extracted_sections:
        for src in section.get("sources", []):
            url = src.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                label = html.escape(f"{counter}. {src.get('title', 'Untitled')} — {url}")
                story.append(Paragraph(label, source_style))
                counter += 1

    doc.build(story)
    return filepath


def run_pipeline(topic, constraints, on_progress):
    """
    Run the full agentic pipeline, calling on_progress(step, message) after
    each stage so the caller (e.g. an SSE stream) can show live progress.
    Returns (report_text, extracted_sections).
    """
    _check_keys()

    on_progress("planning", "Breaking the topic into sub-questions...")
    subquestions = plan_subquestions(topic, constraints)
    on_progress("planning_done", f"Identified {len(subquestions)} sub-questions.")

    extracted_sections = []
    for i, subq in enumerate(subquestions, 1):
        on_progress("searching", f"[{i}/{len(subquestions)}] Searching: {subq}")
        results = search_subquestion(subq)
        on_progress("extracting", f"[{i}/{len(subquestions)}] Extracting facts from {len(results)} sources...")
        section = extract_facts(subq, results)
        extracted_sections.append(section)

    on_progress("cross_checking", "Cross-checking facts for contradictions...")
    conflicts = cross_check_facts(extracted_sections)
    if conflicts:
        on_progress("cross_checking_done", f"Found {len(conflicts)} point(s) needing a conflict note.")
    else:
        on_progress("cross_checking_done", "No contradictions found.")

    on_progress("synthesizing", "Drafting the report from gathered facts...")
    draft = synthesize_report(topic, extracted_sections, conflicts)

    on_progress("reviewing", "Reviewing the draft for gaps...")
    review = self_review(topic, draft)

    if not review["complete"] and review["missing_topics"]:
        on_progress("reviewing_done", "Gaps found — running one more research round...")
        for i, subq in enumerate(review["missing_topics"], 1):
            on_progress("searching", f"Follow-up search: {subq}")
            results = search_subquestion(subq)
            section = extract_facts(subq, results)
            extracted_sections.append(section)
        on_progress("synthesizing", "Re-drafting the report with new information...")
        conflicts = cross_check_facts(extracted_sections)
        draft = synthesize_report(topic, extracted_sections, conflicts)
    else:
        on_progress("reviewing_done", "Draft judged complete.")

    on_progress("done", "Report ready.")
    return draft, extracted_sections
