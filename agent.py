"""
agent.py — Core agent logic. Web app isse import karke use karega.
"""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from solari_browser import Solari
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

MAX_STEPS = 12
EVIDENCE_DIR = "evidence"
HISTORY_FILE = f"{EVIDENCE_DIR}/history.json"

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-3.5-flash-lite")


# ---------------------------------------------------------------------------
# Core decision-making
# ---------------------------------------------------------------------------

async def get_next_action(requirement, history, screenshot_bytes):
    history_text = (
        "\n".join(f"{i+1}. {h}" for i, h in enumerate(history))
        if history else "Koi nahi (pehla step hai)"
    )

    prompt = f"""
Tum ek QA testing agent ho jo browser control karta hai.

TASK: "{requirement}"

Ab tak kiye gaye actions:
{history_text}

Attached screenshot dekh kar decide karo agla action kya hona chahiye.

Sirf JSON format mein jawab do, kuch aur text nahi:
{{
  "thought": "kya soch rahe ho is screenshot ko dekh kar",
  "action": "click" | "fill" | "goto" | "done" | "fail",
  "selector": "playwright selector jaise text=Add to Cart ya CSS selector",
  "value": "agar fill hai toh kya type karna hai (warna empty string)",
  "reason": "agar done/fail hai toh kyun (task complete hua ya nahi)",
  "impact_score": "SIRF agar action 'done' ya 'fail' hai: 1-10 ke beech number, kitna business-critical hai ye flow (jaise checkout/login/payment = 8-10, minor UI/cosmetic = 1-3, normal features = 4-7). Warna 0.",
  "impact_label": "SIRF agar action 'done' ya 'fail' hai: 2-4 words mein label, jaise 'Revenue-critical flow' ya 'Minor cosmetic issue' ya 'Core functionality'. Warna empty string."
}}

Rules:
- Agar task complete ho gaya lagta hai, "done" bhejo.
- Agar 5+ tries ke baad bhi element nahi mil raha, "fail" bhejo.
- Selector simple rakho: "text=Sauces" ya "text=Add to cart" jaisa.
- impact_score dete waqt socho: ye feature agar real production mein break ho, business ko kitna nuksaan hoga?
"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                [prompt, {"mime_type": "image/png", "data": screenshot_bytes}]
            )
            raw = response.text.strip()
            cleaned = re.sub(r"```json|```", "", raw).strip()
            return json.loads(cleaned)
        except ResourceExhausted:
            print("⏳ Rate limit hit, 45s wait kar rahe hain...")
            time.sleep(45)

    raise Exception("Max retries exceeded — rate limit se bahar nahi nikal paye")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def save_to_history(report):
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            history = []

    history.insert(0, report)
    history = history[:50]

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def get_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


# ---------------------------------------------------------------------------
# Single test run
# ---------------------------------------------------------------------------

async def run_agent(url: str, requirement: str, on_update):
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = f"{EVIDENCE_DIR}/{run_id}"
    os.makedirs(run_dir, exist_ok=True)

    history_actions = []
    report = {
        "run_id": run_id,
        "url": url,
        "requirement": requirement,
        "timestamp": datetime.now().isoformat(),
        "steps": [],
        "result": None,
        "running": True,
        "session_id": None,
    }
    on_update(report)

    browser = None
    try:
        solari = Solari(api_key=os.environ["SOLARI_API_KEY"])
        browser = await solari.launch(recording=True)
        report["session_id"] = browser.id
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        for step in range(1, MAX_STEPS + 1):
            screenshot_bytes = await page.screenshot()
            screenshot_path = f"{run_dir}/step-{step}.png"
            with open(screenshot_path, "wb") as f:
                f.write(screenshot_bytes)

            decision = await get_next_action(requirement, history_actions, screenshot_bytes)

            step_data = {"step": step, "screenshot": f"{run_id}/step-{step}.png", **decision}
            report["steps"].append(step_data)
            on_update(report)

            action = decision.get("action")

            if action == "done":
                report["result"] = {
                    "status": "PASS",
                    "reason": decision.get("reason"),
                    "impact_score": decision.get("impact_score", 0),
                    "impact_label": decision.get("impact_label", ""),
                }
                break

            if action == "fail":
                report["result"] = {
                    "status": "FAIL",
                    "reason": decision.get("reason"),
                    "impact_score": decision.get("impact_score", 0),
                    "impact_label": decision.get("impact_label", ""),
                }
                break

            try:
                if action == "click":
                    await page.locator(decision["selector"]).first.click(timeout=8000)
                    history_actions.append(f"Clicked \"{decision['selector']}\"")
                elif action == "fill":
                    await page.locator(decision["selector"]).first.fill(
                        decision.get("value", ""), timeout=8000
                    )
                    history_actions.append(f"Filled \"{decision['selector']}\" with \"{decision.get('value')}\"")
                elif action == "goto":
                    await page.goto(decision["value"], wait_until="domcontentloaded")
                    history_actions.append(f"Navigated to \"{decision['value']}\"")
            except Exception as e:
                history_actions.append(f"FAILED to {action} \"{decision.get('selector')}\": {e}")

            if step == MAX_STEPS and report["result"] is None:
                report["result"] = {"status": "FAIL", "reason": "Max steps reached without completion", "impact_score": 0, "impact_label": ""}

    except Exception as e:
        report["result"] = {
            "status": "FAIL",
            "reason": f"Agent crashed: {e}",
            "impact_score": 0,
            "impact_label": "",
        }

    finally:
        if report.get("result") and report.get("steps"):
            try:
                report["summary"] = await generate_summary(report)
            except Exception:
                report["summary"] = ""
        report["running"] = False
        save_to_history(report)
        on_update(report)
        if browser:
            await browser.close()


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------

async def run_batch(url: str, requirements: list, on_update):
    batch_report = {
        "batch": True,
        "url": url,
        "running": True,
        "runs": [],
    }
    on_update(batch_report)

    for req in requirements:
        slot_index = len(batch_report["runs"])
        batch_report["runs"].append({
            "requirement": req,
            "steps": [],
            "result": None,
            "running": True,
        })
        on_update(batch_report)

        def sub_update(single_report, idx=slot_index):
            batch_report["runs"][idx] = single_report
            on_update(batch_report)

        await run_agent(url, req, sub_update)

    batch_report["running"] = False
    on_update(batch_report)


# ---------------------------------------------------------------------------
# Coverage checklist mode
# ---------------------------------------------------------------------------

CHECKLISTS = {
    "ecommerce": [
        "search for a product using the search bar",
        "open a product and view its details",
        "add an item to the cart",
        "open the cart and verify the item appears there",
        "proceed towards checkout",
    ],
    "auth": [
        "locate and open the login form",
        "attempt to log in with any visible demo or test credentials",
        "locate the sign up or register link",
        "locate the forgot password link",
    ],
    "saas": [
        "log in using any visible demo credentials",
        "navigate to the main dashboard after login",
        "open the settings or profile page",
        "open the main navigation menu and check it works",
    ],
    "content": [
        "open the main navigation menu",
        "click into the first visible article or post",
        "use the search bar if one exists",
        "scroll to the footer and check its links are visible",
    ],
}


async def run_checklist(url: str, checklist_type: str, on_update):
    reqs = CHECKLISTS.get(checklist_type, [])
    batch_report = {
        "batch": True,
        "checklist": True,
        "site_type": checklist_type,
        "url": url,
        "running": True,
        "runs": [],
        "health_score": None,
    }
    on_update(batch_report)

    for req in reqs:
        idx = len(batch_report["runs"])
        batch_report["runs"].append({"requirement": req, "steps": [], "result": None, "running": True})
        on_update(batch_report)

        def sub_update(single_report, idx=idx):
            batch_report["runs"][idx] = single_report
            on_update(batch_report)

        await run_agent(url, req, sub_update)

    total = len(batch_report["runs"])
    passed = sum(1 for r in batch_report["runs"] if r["result"] and r["result"]["status"] == "PASS")
    batch_report["health_score"] = round(passed / total * 100) if total else 0
    batch_report["running"] = False
    on_update(batch_report)


# ---------------------------------------------------------------------------
# Auto-suggested test cases
# ---------------------------------------------------------------------------

async def suggest_tests(url: str) -> list:
    solari = Solari(api_key=os.environ["SOLARI_API_KEY"])
    browser = await solari.launch()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        screenshot_bytes = await page.screenshot()
    finally:
        await browser.close()

    prompt = """
Tum ek QA test planner ho. Neeche diye gaye screenshot dekh kar is website ke liye
3 se 5 sensible, concrete test cases suggest karo jo ek user commonly perform karega.

Sirf JSON array format mein jawab do, kuch aur text nahi:
["test requirement 1", "test requirement 2", "test requirement 3"]

Rules:
- Har suggestion plain English mein ek clear, actionable instruction ho (jaise "log in and add an item to the cart"), 15 words se kam.
- Website ke visible elements (buttons, forms, nav links) ke basis par realistic suggestions do.
- Generic suggestions mat do jaise "test the website" — specific raho.
"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                [prompt, {"mime_type": "image/png", "data": screenshot_bytes}]
            )
            raw = response.text.strip()
            cleaned = re.sub(r"```json|```", "", raw).strip()
            return json.loads(cleaned)
        except ResourceExhausted:
            time.sleep(45)
    return []


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------

def generate_pdf_report(report, output_path):
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib import colors

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Title'], fontSize=20)
    h2 = ParagraphStyle('H2', parent=styles['Heading2'])
    h4 = ParagraphStyle('H4', parent=styles['Heading4'], fontSize=11, spaceBefore=6)
    body = styles['BodyText']
    small = ParagraphStyle('Small', parent=styles['BodyText'], fontSize=9, textColor=colors.grey)

    doc = SimpleDocTemplate(output_path, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    elements = []

    elements.append(Paragraph("QA Test Report", title_style))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(f"<b>URL:</b> {report.get('url', '')}", body))
    elements.append(Paragraph(f"<b>Requirement:</b> {report.get('requirement', '')}", body))
    elements.append(Paragraph(f"<b>Run at:</b> {report.get('timestamp', '')}", small))
    elements.append(Spacer(1, 12))

    result = report.get('result') or {}
    status = result.get('status', 'UNKNOWN')
    color = colors.HexColor('#0F8A5F') if status == 'PASS' else colors.HexColor('#C4362E')
    verdict_style = ParagraphStyle('Verdict', parent=styles['Heading2'], textColor=color)
    elements.append(Paragraph(f"Result: {status}", verdict_style))
    elements.append(Paragraph(result.get('reason', ''), body))
    if result.get('impact_score'):
        elements.append(Paragraph(
            f"<b>Impact:</b> {result['impact_score']}/10 — {result.get('impact_label', '')}", body
        ))
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Execution Log", h2))
    for step in report.get('steps', []):
        elements.append(Spacer(1, 10))
        elements.append(Paragraph(
            f"Step {step['step']}: {step.get('action', '')} {step.get('selector', '')}", h4
        ))
        elements.append(Paragraph(step.get('thought', ''), body))
        img_path = os.path.join(EVIDENCE_DIR, step['screenshot'])
        if os.path.exists(img_path):
            try:
                elements.append(RLImage(img_path, width=5.5 * inch, height=3.1 * inch))
            except Exception:
                pass

    doc.build(elements)