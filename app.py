"""
app.py — Flask backend for the Research & Report Agent.

Routes:
  GET  /                    -> main UI
  GET  /api/research        -> SSE stream that runs the agent pipeline live
  GET  /api/download/<id>   -> download a generated PDF report
  GET  /api/history         -> list past reports
"""

import os
import json
import uuid
import queue
import threading
import traceback
from datetime import datetime

from flask import Flask, Response, render_template, request, jsonify, send_file, stream_with_context
from dotenv import load_dotenv

load_dotenv()

import agent
import database

app = Flask(__name__)
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

database.init_db()


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/history")
def history():
    return jsonify(database.get_history())


@app.route("/api/download/<report_id>")
def download(report_id):
    record = database.get_report(report_id)
    if not record or not os.path.exists(record["filepath"]):
        return jsonify({"error": "Report not found"}), 404
    safe_name = "".join(c for c in record["topic"][:40] if c.isalnum() or c in " _-").strip() or "report"
    return send_file(record["filepath"], as_attachment=True, download_name=f"{safe_name}.pdf")


@app.route("/api/research")
def research():
    """
    Runs the agent pipeline in a background thread and streams progress
    events to the client over Server-Sent Events as they happen in real time.
    """
    topic = (request.args.get("topic") or "").strip()
    constraints = (request.args.get("constraints") or "").strip()

    if not topic:
        return jsonify({"error": "A research topic is required."}), 400
    if len(topic) > 300:
        return jsonify({"error": "Topic is too long (max 300 characters)."}), 400

    def stream():
        q = queue.Queue()
        result_holder = {}

        def progress_to_queue(step, message):
            q.put(("progress", {"step": step, "message": message}))

        def worker():
            try:
                report_text, sections = agent.run_pipeline(topic, constraints, progress_to_queue)
                result_holder["report_text"] = report_text
                result_holder["sections"] = sections
            except agent.AgentError as e:
                result_holder["error"] = str(e)
            except Exception:
                traceback.print_exc()
                result_holder["error"] = "An unexpected error occurred while generating the report."
            finally:
                q.put(("__done__", None))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        while True:
            event_type, payload = q.get()
            if event_type == "__done__":
                break
            yield sse_event(event_type, payload)

        if "error" in result_holder:
            yield sse_event("error", {"message": result_holder["error"]})
            return

        report_text = result_holder["report_text"]
        sections = result_holder["sections"]

        report_id = uuid.uuid4().hex[:12]
        filepath = os.path.join(REPORTS_DIR, f"{report_id}.pdf")
        try:
            agent.render_pdf(filepath, topic, report_text, sections)
        except Exception:
            traceback.print_exc()
            yield sse_event("error", {"message": "Report was drafted but PDF generation failed."})
            return

        database.save_report(report_id, topic, constraints, filepath, datetime.utcnow().isoformat())

        yield sse_event(
            "done",
            {
                "report_id": report_id,
                "topic": topic,
                "download_url": f"/api/download/{report_id}",
                "preview": report_text[:800],
            },
        )

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Content-Type": "text/event-stream",
    }
    return Response(stream_with_context(stream()), headers=headers)


if __name__ == "__main__":
    app.run(debug=False, port=5000, threaded=True)
