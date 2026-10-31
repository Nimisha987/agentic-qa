"""
app.py — Flask web server.
"""

import asyncio
import threading

from flask import Flask, render_template, request, jsonify, send_from_directory
from dotenv import load_dotenv

from agent import (
    run_agent, run_batch, run_checklist, suggest_tests,
    get_history, generate_pdf_report, EVIDENCE_DIR
)

load_dotenv()

app = Flask(__name__)

current_status = {"running": False, "steps": [], "result": None}


def start_thread(coro_factory):
    def runner():
        asyncio.run(coro_factory())
    thread = threading.Thread(target=runner)
    thread.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run_test():
    data = request.json
    url = data.get("url")
    requirement = data.get("requirement")

    if not url or not requirement:
        return jsonify({"error": "URL aur requirement dono zaroori hain"}), 400

    current_status.clear()
    current_status.update({"running": True, "steps": [], "result": None})

    def update_status(report):
        current_status.update(report)

    start_thread(lambda: run_agent(url, requirement, update_status))
    return jsonify({"started": True})


@app.route("/run_batch", methods=["POST"])
def run_batch_test():
    data = request.json
    url = data.get("url")
    requirements_raw = data.get("requirements", "")
    reqs = [r.strip() for r in requirements_raw.split("\n") if r.strip()]

    if not url or not reqs:
        return jsonify({"error": "URL aur kam se kam ek requirement zaroori hai"}), 400

    current_status.clear()
    current_status.update({"running": True, "batch": True, "runs": []})

    def update_status(report):
        current_status.clear()
        current_status.update(report)

    start_thread(lambda: run_batch(url, reqs, update_status))
    return jsonify({"started": True})


@app.route("/run_checklist", methods=["POST"])
def run_checklist_route():
    data = request.json
    url = data.get("url")
    checklist_type = data.get("checklist_type")

    if not url or not checklist_type:
        return jsonify({"error": "URL aur checklist type zaroori hai"}), 400

    current_status.clear()
    current_status.update({"running": True, "batch": True, "runs": []})

    def update_status(report):
        current_status.clear()
        current_status.update(report)

    start_thread(lambda: run_checklist(url, checklist_type, update_status))
    return jsonify({"started": True})


@app.route("/suggest", methods=["POST"])
def suggest():
    data = request.json
    url = data.get("url")
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        suggestions = asyncio.run(suggest_tests(url))
        return jsonify({"suggestions": suggestions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/status")
def status():
    return jsonify(current_status)


@app.route("/history")
def history():
    return jsonify(get_history())


@app.route("/export/<run_id>")
def export_pdf(run_id):
    history_data = get_history()
    report = next((r for r in history_data if r.get("run_id") == run_id), None)
    if not report:
        return "Report not found", 404

    output_path = f"{EVIDENCE_DIR}/{run_id}-report.pdf"
    generate_pdf_report(report, output_path)
    return send_from_directory(EVIDENCE_DIR, f"{run_id}-report.pdf", as_attachment=True)


@app.route("/evidence/<path:filename>")
def evidence(filename):
    return send_from_directory(EVIDENCE_DIR, filename)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5000)