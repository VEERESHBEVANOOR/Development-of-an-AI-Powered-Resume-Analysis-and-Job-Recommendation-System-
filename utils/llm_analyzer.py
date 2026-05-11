import json
import re
import requests
import time

from config import (
    USE_LLM,
    OPENAI_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OLLAMA_RETRIES,
)


def _default_payload():
    return {
        "summary": "Profile Summary:\n- Could not generate summary",
        "insights": {
            "strengths": [],
            "weaknesses": [],
            "gaps": [],
            "recommendations": [],
            "target_roles": [],
        },
    }


def _extract_json(text: str):
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None


def _extract_list_block(text: str, key: str):
    pattern = rf'"{re.escape(key)}"\s*:\s*\[(.*?)\]'
    m = re.search(pattern, text, flags=re.DOTALL)
    if not m:
        return []
    block = m.group(1)
    items = re.findall(r'"([^"]+)"', block)
    if not items:
        items = [x.strip(" \n\r\t,-") for x in block.split("\n") if x.strip()]
    clean = [i.strip() for i in items if i.strip()]
    return clean[:6]


def _extract_fallback_from_text(text: str):
    def _clean_summary(raw: str) -> str:
        s = (raw or "").replace("\\n", "\n").replace('\\"', '"').strip()
        s = s.strip("` ").strip()
        if s.startswith("{"):
            s = s[1:].strip()
        s = re.sub(r'^"summary"\s*:\s*"?', "", s, flags=re.IGNORECASE).strip()
        cut_markers = [
            "Key strengths include:",
            "Key areas for improvement:",
            "Gaps in the resume:",
            "Recommendations:",
            "Target roles:",
            '"insights"',
        ]
        for marker in cut_markers:
            pos = s.find(marker)
            if pos != -1:
                s = s[:pos].strip()
        s = s.strip('", ')
        return s

    summary = ""
    m = re.search(r'"summary"\s*:\s*"(.+?)"\s*,\s*"insights"', text, flags=re.DOTALL)
    if m:
        summary = m.group(1)
    elif '"summary"' in text:
        m2 = re.search(r'"summary"\s*:\s*"(.+)$', text, flags=re.DOTALL)
        if m2:
            summary = m2.group(1)
    else:
        # Try fenced block or plain section extraction
        txt = text.replace("```json", "").replace("```", "").strip()
        if "Profile Summary" in txt or "Technical Skills" in txt:
            summary = txt
        else:
            summary = "\n".join(line.strip() for line in txt.splitlines()[:12] if line.strip())

    summary = _clean_summary(summary)
    insights = {
        "strengths": _extract_list_block(text, "strengths"),
        "weaknesses": _extract_list_block(text, "weaknesses"),
        "gaps": _extract_list_block(text, "gaps"),
        "recommendations": _extract_list_block(text, "recommendations"),
        "target_roles": _extract_list_block(text, "target_roles"),
    }
    return {"summary": summary, "insights": insights}


def _build_summary(data: dict) -> str:
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()

    profile = data.get("profile_summary", "")
    skills = data.get("skills", [])
    projects = data.get("projects", [])

    lines = ["Profile Summary:"]
    if profile:
        lines.append(f"- {profile}")
    if skills:
        lines.append("Technical Skills:")
        for s in skills[:3]:
            lines.append(f"- {s}")
    if projects:
        lines.append("Projects / Experience:")
        for p in projects[:2]:
            lines.append(f"- {p}")
    return "\n".join(lines)


def _normalize(data: dict):
    payload = _default_payload()
    payload["summary"] = _build_summary(data)

    insights = data.get("insights", {})
    if not isinstance(insights, dict):
        insights = {}
    normalized_insights = {}
    for key in ["strengths", "weaknesses", "gaps", "recommendations", "target_roles"]:
        value = insights.get(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []
        normalized_insights[key] = [str(x).strip() for x in value if str(x).strip()][:6]
    payload["insights"] = normalized_insights
    return payload


def _prompt(resume_text: str) -> str:
    return f"""You are an expert resume analyzer.
Use only the provided resume text. Do not invent facts.
Return STRICT JSON only with this schema:
{{
  "summary": "string (max 3 concise sentences, plain text only)",
  "insights": {{
    "strengths": ["max 4 bullets"],
    "weaknesses": ["max 4 bullets"],
    "gaps": ["max 4 bullets"],
    "recommendations": ["max 4 bullets"],
    "target_roles": ["max 4 bullets"]
  }}
}}

Resume text:
{resume_text}
"""


def analyze_resume_llm(resume_text):
    if not USE_LLM:
        raise RuntimeError("LLM analysis is disabled in config.py")

    prompt = _prompt(resume_text)
    try:
        if LLM_PROVIDER == "openai":
            if not OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY is not set")
            from openai import OpenAI  # type: ignore

            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            content = (response.choices[0].message.content or "").strip()

        elif LLM_PROVIDER == "ollama":
            url = f"{OLLAMA_HOST}/api/generate"
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 220,
                    "num_ctx": 2048,
                },
            }
            last_exc = None
            content = ""
            for attempt in range(OLLAMA_RETRIES + 1):
                try:
                    resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
                    resp.raise_for_status()
                    content = (resp.json().get("response") or "").strip()
                    if content:
                        break
                except Exception as exc:
                    last_exc = exc
                    if attempt < OLLAMA_RETRIES:
                        time.sleep(1.2 * (attempt + 1))
                    continue
            if not content and last_exc:
                raise last_exc
        else:
            raise RuntimeError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER}")

        parsed = _extract_json(content)
        if not parsed:
            extracted = _extract_fallback_from_text(content)
            summary = extracted.get("summary") or _default_payload()["summary"]
            return {
                "summary": summary,
                "insights": extracted.get("insights", _default_payload()["insights"]),
            }
        return _normalize(parsed)
    except Exception as e:
        raise RuntimeError(f"LLM analysis failed: {e}") from e
