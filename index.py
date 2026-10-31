"""
Agentic QA Engineer — AI agent jo browser control karke website test karta hai.
Usage: python index.py "<url>" "<requirement>"
"""

import asyncio
import base64
import json
import os
import re
import sys
import time

from dotenv import load_dotenv
from solari_browser import Solari
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

load_dotenv()

MAX_STEPS = 12
EVIDENCE_DIR = "evidence"

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-3.5-flash-lite")


async def get_next_action(requirement, history, screenshot_bytes):
    """Gemini ko screenshot + task dekar agla action poochta hai."""
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
  "reason": "agar done/fail hai toh kyun (task complete hua ya nahi)"
}}

Rules:
- Agar task complete ho gaya lagta hai, "done" bhejo.
- Agar 5+ tries ke baad bhi element nahi mil raha, "fail" bhejo.
- Selector simple rakho: "text=Sauces" ya "text=Add to cart" jaisa.
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
            wait_time = 45
            print(f"⏳ Rate limit hit, {wait_time}s wait kar rahe hain (attempt {attempt+1}/{max_retries})...")
            time.sleep(wait_time)

    raise Exception("Max retries exceeded — rate limit se bahar nahi nikal paye")


async def run_agent(url: str, requirement: str):
    os.makedirs(EVIDENCE_DIR, exist_ok=True)

    print(f"\n🚀 Starting QA test on: {url}")
    print(f"📋 Requirement: {requirement}\n")

    solari = Solari(api_key=os.environ["SOLARI_API_KEY"])
    browser = await solari.launch(recording=True)

    history = []
    report = {"url": url, "requirement": requirement, "steps": [], "result": None}

    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        for step in range(1, MAX_STEPS + 1):
            screenshot_bytes = await page.screenshot()
            screenshot_path = f"{EVIDENCE_DIR}/step-{step}.png"
            with open(screenshot_path, "wb") as f:
                f.write(screenshot_bytes)

            print(f"\n--- Step {step} ---")
            decision = await get_next_action(requirement, history, screenshot_bytes)
            print(f"🤔 Thought: {decision.get('thought')}")
            print(f"⚡ Action: {decision.get('action')} {decision.get('selector', '')}")

            report["steps"].append({"step": step, "screenshot": screenshot_path, **decision})

            action = decision.get("action")

            if action == "done":
                report["result"] = {"status": "PASS", "reason": decision.get("reason")}
                print(f"\n✅ PASS: {decision.get('reason')}")
                break

            if action == "fail":
                report["result"] = {"status": "FAIL", "reason": decision.get("reason")}
                print(f"\n❌ FAIL: {decision.get('reason')}")
                break

            try:
                if action == "click":
                    await page.locator(decision["selector"]).first.click(timeout=8000)
                    history.append(f"Clicked \"{decision['selector']}\"")
                elif action == "fill":
                    await page.locator(decision["selector"]).first.fill(
                        decision.get("value", ""), timeout=8000
                    )
                    history.append(f"Filled \"{decision['selector']}\" with \"{decision.get('value')}\"")
                elif action == "goto":
                    await page.goto(decision["value"], wait_until="domcontentloaded")
                    history.append(f"Navigated to \"{decision['value']}\"")
            except Exception as e:
                history.append(f"FAILED to {action} \"{decision.get('selector')}\": {e}")
                print(f"⚠️ Action failed: {e}")

            if step == MAX_STEPS:
                report["result"] = {"status": "FAIL", "reason": "Max steps reached without completion"}

    finally:
        with open(f"{EVIDENCE_DIR}/report.json", "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n📄 Report saved to {EVIDENCE_DIR}/report.json")
        await browser.close()
async def generate_summary(report) -> str:
    """Poore run ka ek chhota human-readable summary banata hai."""
    steps = report.get("steps", [])
    result = report.get("result", {}) or {}

    steps_text = "\n".join(
        f"{s['step']}. {s.get('action')} — {s.get('thought', '')[:100]}"
        for s in steps
    )

    prompt = f"""
Neeche ek QA test run ka poora log hai. Ek chhota, natural, professional summary likho
(1-2 sentences, max 40 words) jo bataye ki kya hua — kitne steps liye, koi retry/struggle
hua ya smooth chala, aur final outcome kya raha. Third-person, past tense mein likho,
jaise "The agent..." se shuru ho.

REQUIREMENT: {report.get('requirement')}
RESULT: {result.get('status')} — {result.get('reason')}

STEPS:
{steps_text}

Sirf summary text return karo, koi JSON ya extra formatting nahi.
"""
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception:
        return ""

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print('Usage: python index.py "<url>" "<requirement>"')
        sys.exit(1)

    asyncio.run(run_agent(sys.argv[1], sys.argv[2]))