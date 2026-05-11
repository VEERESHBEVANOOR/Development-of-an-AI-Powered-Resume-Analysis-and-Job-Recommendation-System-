import io
import os
import re
import json
import base64
import tempfile
import threading
import time
import traceback
import textwrap
import requests
from urllib.parse import quote
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, send_file, jsonify
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from config import (
    UPLOAD_FOLDER,
    SECRET_KEY,
    MAX_RESUME_CHARS_FOR_LLM,
    ALLOW_LLM_FALLBACK,
    USE_LLM,
    OPENAI_API_KEY,
    LLM_MODEL,
    LLM_PROVIDER,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_TIMEOUT,
)
from utils.auth import create_user, validate_user, init_db
from utils.resume_parser import extract_text
from utils.embedding import get_embedding
from utils.pinecone_db import init_pinecone, store_resume
from utils.llm_analyzer import analyze_resume_llm
from utils.uploads import save_upload, get_uploads_for_user, get_upload_by_id, delete_upload
from utils.scoring import compute_scores
from utils.insight_fallback import enrich_insights
from utils.linkedin_scraper import fetch_linkedin_jobs
from utils.job_recommender import (
    rank_jobs_for_resume,
    extract_keywords_from_resume,
    compute_market_trends,
)

from fpdf import FPDF

app = Flask(__name__)
app.secret_key = SECRET_KEY

init_pinecone()
init_db()

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("database", exist_ok=True)


@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/auth")
def login():
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        create_user(
            request.form["name"],
            request.form["email"],
            request.form["password"]
        )
        return redirect("/auth")
    return render_template("register.html")


@app.route("/login", methods=["POST"])
def login_user():
    if validate_user(request.form["email"], request.form["password"]):
        session["user"] = request.form["email"]
        return redirect("/workspace")
    return "Invalid credentials"


@app.route("/home")
def home():
    if "user" not in session:
        return redirect("/auth")
    return render_template("home.html")


@app.route("/workspace")
def workspace():
    if "user" not in session:
        return redirect("/auth")
    return render_template("workspace.html")


def _extract_markdown_section(markdown_text, heading):
    pattern = rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    m = re.search(pattern, markdown_text, flags=re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip()


def _parse_resume_markdown(markdown_text):
    text = (markdown_text or "").replace("\r\n", "\n").strip()
    parsed = {}
    if not text:
        return parsed

    lines = [ln.strip() for ln in text.split("\n")]
    first_header = next((ln for ln in lines if ln.startswith("# ")), "")
    if first_header:
        parsed["full_name"] = first_header[2:].strip()

    non_empty = [ln for ln in lines if ln and not ln.startswith("#")]
    if non_empty:
        parsed["role_title"] = non_empty[0]

    contact_block = _extract_markdown_section(text, "Contact")
    for raw in contact_block.split("\n"):
        ln = raw.strip().lstrip("-").strip()
        low = ln.lower()
        if low.startswith("phone:"):
            parsed["phone"] = ln.split(":", 1)[1].strip()
        elif low.startswith("email:"):
            parsed["email"] = ln.split(":", 1)[1].strip()
        elif low.startswith("linkedin:"):
            parsed["linkedin"] = ln.split(":", 1)[1].strip()
        elif low.startswith("github:"):
            parsed["github"] = ln.split(":", 1)[1].strip()

    parsed["bio"] = _extract_markdown_section(text, "Summary")

    education_block = _extract_markdown_section(text, "Education")
    edu_lines = [ln.strip().lstrip("-").strip() for ln in education_block.split("\n") if ln.strip()]
    for i, edu in enumerate(edu_lines[:2], start=1):
        parts = [p.strip() for p in edu.split("|")]
        if len(parts) > 0:
            parsed[f"edu_{i}_degree"] = parts[0]
        if len(parts) > 1:
            parsed[f"edu_{i}_college"] = parts[1]
        if len(parts) > 2:
            parsed[f"edu_{i}_date"] = parts[2]
        if len(parts) > 3:
            parsed[f"edu_{i}_score"] = parts[3]

    skills_block = _extract_markdown_section(text, "Technical Skills")
    skill_lines = [ln.strip().lstrip("-").strip() for ln in skills_block.split("\n") if ln.strip()]
    plain_skills = []
    for ln in skill_lines:
        low = ln.lower()
        if low.startswith("languages:"):
            parsed["skill_languages"] = ln.split(":", 1)[1].strip()
        elif low.startswith("frontend & backend:"):
            parsed["skill_frontend_backend"] = ln.split(":", 1)[1].strip()
        elif low.startswith("database:"):
            parsed["skill_database"] = ln.split(":", 1)[1].strip()
        elif low.startswith("others:"):
            parsed["skill_others"] = ln.split(":", 1)[1].strip()
        else:
            plain_skills.append(ln)
    if plain_skills:
        parsed["skills"] = "\n".join(plain_skills)

    projects_block = _extract_markdown_section(text, "Projects")
    project_lines = [ln.strip().lstrip("-").strip() for ln in projects_block.split("\n") if ln.strip()]
    parsed["projects"] = "\n".join(project_lines)
    for i, proj in enumerate(project_lines[:4], start=1):
        if " - " in proj:
            title, desc = proj.split(" - ", 1)
        else:
            title, desc = proj, ""
        parsed[f"project_{i}_title"] = title.strip()
        parsed[f"project_{i}_desc"] = desc.strip()

    cert_block = _extract_markdown_section(text, "Certificates")
    cert_lines = [ln.strip().lstrip("-").strip() for ln in cert_block.split("\n") if ln.strip()]
    if cert_lines:
        parsed["certificates"] = "\n".join(cert_lines)

    additional_block = _extract_markdown_section(text, "Additional Information")
    additional_lines = [ln.strip().lstrip("-").strip() for ln in additional_block.split("\n") if ln.strip()]
    extra_certs = []
    for ln in additional_lines:
        low = ln.lower()
        if low.startswith("soft skills:"):
            parsed["add_soft_skills"] = ln.split(":", 1)[1].strip()
        elif low.startswith("languages:"):
            parsed["add_languages"] = ln.split(":", 1)[1].strip()
        elif low.startswith("linkedin:"):
            parsed["linkedin"] = ln.split(":", 1)[1].strip()
        elif low.startswith("github:"):
            parsed["github"] = ln.split(":", 1)[1].strip()
        elif low.startswith("address:"):
            parsed["address"] = ln.split(":", 1)[1].strip()
        else:
            extra_certs.append(ln)
    if extra_certs:
        parsed["add_certs"] = "\n".join(extra_certs)

    return parsed


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "user" not in session:
        return redirect("/auth")

    if request.method == "POST":
        t0 = time.time()
        file = request.files["resume"]
        if not file or not file.filename.lower().endswith(".pdf"):
            return render_template("error.html", message="Only PDF resumes are allowed"), 400

        filename = secure_filename(file.filename)
        path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(path)

        resume_text = extract_text(path)
        if not resume_text.strip():
            return render_template(
                "error.html",
                message="Could not extract text from the resume PDF"
            ), 400

        fast_resume_text = _trim_for_model(resume_text, MAX_RESUME_CHARS_FOR_LLM)

        embedding = get_embedding(fast_resume_text)
        _store_resume_async(session["user"], embedding, resume_text)
        print(f"[upload] parse+embed: {time.time() - t0:.2f}s")

        try:
            analysis = analyze_resume_llm(fast_resume_text)
        except Exception as exc:
            if ALLOW_LLM_FALLBACK:
                analysis = _fallback_analysis(resume_text)
            else:
                return render_template(
                    "error.html",
                    message=f"Real LLM failed: {exc}. Check Ollama server/model and retry.",
                ), 500
        print(f"[upload] llm done: {time.time() - t0:.2f}s")

        summary_text = clean_summary_text(
            analysis.get("summary", "Profile Summary:\n- Summary not available")
        )
        insights = enrich_insights(resume_text, analysis.get("insights", {}))
        keywords = extract_keywords_from_resume(resume_text)
        li_location = (request.form.get("linkedin_location") or "").strip()
        include_jobs = request.form.get("include_jobs") == "on"
        linkedin_jobs = []
        job_note = ""
        if include_jobs:
            try:
                linkedin_jobs = fetch_linkedin_jobs(
                    keywords,
                    li_location or None,
                    request_timeout_s=30,
                )
                print(f"[upload] linkedin jobs fetched: {len(linkedin_jobs)} in {time.time() - t0:.2f}s")
            except Exception as exc:
                linkedin_jobs = []
                job_note = f"Real LinkedIn scraping unavailable: {exc}"
                print(f"[upload] linkedin error after {time.time() - t0:.2f}s: {exc}")
        strengths = insights.get("strengths", []) if isinstance(insights, dict) else []
        ranked_jobs = rank_jobs_for_resume(
            resume_text,
            linkedin_jobs,
            keywords,
            strengths=strengths,
            top_k=8,
        )
        ranked_jobs = _filter_jobs_for_display(ranked_jobs)
        market_trends = compute_market_trends(ranked_jobs)
        if include_jobs and not ranked_jobs and not job_note:
            job_note = (
                "LinkedIn returned no visible jobs for current keywords/location. "
                "Try a city (e.g., Bengaluru), then retry."
            )
        scores = compute_scores(resume_text)
        upload_id = save_upload(
            session["user"],
            filename,
            summary_text,
            resume_text,
            insights=insights,
            jobs=ranked_jobs,
        )
        print(f"[upload] complete in {time.time() - t0:.2f}s")

        return render_template(
            "result.html",
            summary=summary_text,
            summary_html=format_summary_html(summary_text),
            insights=insights,
            jobs=ranked_jobs,
            market_trends=market_trends,
            job_note=job_note,
            resume_text=resume_text,
            upload_id=upload_id,
            resume_score=scores["resume_score"],
            ats_score=scores["ats_score"],
            ats_checks=scores["ats_checks"],
            tone_score=scores["tone_score"],
            content_score=scores["content_score"],
            structure_score=scores["structure_score"],
            skills_score=scores["skills_score"],
        )

    return render_template("upload.html")


@app.route("/resume-builder", methods=["GET", "POST"])
def resume_builder():
    if "user" not in session:
        return redirect("/auth")

    if request.method == "POST":
        data = {
            "full_name": (request.form.get("full_name") or "").strip(),
            "dob": (request.form.get("dob") or "").strip(),
            "phone": (request.form.get("phone") or "").strip(),
            "email": (request.form.get("email") or "").strip(),
            "github": (request.form.get("github") or "").strip(),
            "linkedin": (request.form.get("linkedin") or "").strip(),
            "college": (request.form.get("college") or "").strip(),
            "degree": (request.form.get("degree") or "").strip(),
            "address": (request.form.get("address") or "").strip(),
            "bio": (request.form.get("bio") or "").strip(),
            "experience": (request.form.get("experience") or "").strip(),
            "skills": (request.form.get("skills") or "").strip(),
            "projects": (request.form.get("projects") or "").strip(),
            "certificates": (request.form.get("certificates") or "").strip(),
            "template": (request.form.get("template") or "classic").strip().lower(),
            "role_title": (request.form.get("role_title") or "").strip(),
            "project_1_title": (request.form.get("project_1_title") or "").strip(),
            "project_1_desc": (request.form.get("project_1_desc") or "").strip(),
            "project_2_title": (request.form.get("project_2_title") or "").strip(),
            "project_2_desc": (request.form.get("project_2_desc") or "").strip(),
            "project_3_title": (request.form.get("project_3_title") or "").strip(),
            "project_3_desc": (request.form.get("project_3_desc") or "").strip(),
            "project_4_title": (request.form.get("project_4_title") or "").strip(),
            "project_4_desc": (request.form.get("project_4_desc") or "").strip(),
            "edu_1_date": (request.form.get("edu_1_date") or "").strip(),
            "edu_1_degree": (request.form.get("edu_1_degree") or "").strip(),
            "edu_1_college": (request.form.get("edu_1_college") or "").strip(),
            "edu_1_score": (request.form.get("edu_1_score") or "").strip(),
            "edu_2_date": (request.form.get("edu_2_date") or "").strip(),
            "edu_2_degree": (request.form.get("edu_2_degree") or "").strip(),
            "edu_2_college": (request.form.get("edu_2_college") or "").strip(),
            "edu_2_score": (request.form.get("edu_2_score") or "").strip(),
            "skill_languages": (request.form.get("skill_languages") or "").strip(),
            "skill_frontend_backend": (request.form.get("skill_frontend_backend") or "").strip(),
            "skill_database": (request.form.get("skill_database") or "").strip(),
            "skill_others": (request.form.get("skill_others") or "").strip(),
            "add_soft_skills": (request.form.get("add_soft_skills") or "").strip(),
            "add_languages": (request.form.get("add_languages") or "").strip(),
            "add_certs": (request.form.get("add_certs") or "").strip(),
        }
        markdown_text = (request.form.get("resume_markdown") or "").strip()
        if markdown_text:
            parsed_md = _parse_resume_markdown(markdown_text)
            for key, value in parsed_md.items():
                if key in data and value:
                    data[key] = value
        if not data["full_name"] or not data["email"]:
            return render_template("error.html", message="Name and email are required"), 400
        try:
            return _generate_resume_pdf(data)
        except Exception as exc:
            print("[resume-builder] error:", exc)
            print(traceback.format_exc())
            return render_template("error.html", message=f"Resume generation failed: {exc}"), 500

    return render_template("resume_builder.html")


@app.route("/interview-questions", methods=["GET", "POST"])
def interview_questions():
    if "user" not in session:
        return redirect("/auth")
    if request.method == "POST":
        file = request.files.get("resume")
        if not file or not file.filename.lower().endswith(".pdf"):
            return render_template("error.html", message="Only PDF resumes are allowed"), 400

        filename = secure_filename(file.filename)
        path = os.path.join(UPLOAD_FOLDER, f"iq_{int(time.time())}_{filename}")
        file.save(path)

        resume_text = extract_text(path)
        if not resume_text.strip():
            return render_template("error.html", message="Could not extract text from the resume PDF"), 400

        fast_resume_text = _trim_for_model(resume_text, MAX_RESUME_CHARS_FOR_LLM)
        count = max(5, min(20, int(request.form.get("count", "8") or "8")))
        qa_items = _generate_interview_qa(fast_resume_text, count=count)
        return render_template(
            "interview_questions.html",
            qa_items=qa_items,
            requested_count=count,
            source_file=filename,
        )

    return render_template("interview_questions.html", qa_items=[], requested_count=8, source_file="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


def _get_resume_row_for_context(email, selected_upload_id=None):
    if not email:
        return None
    if selected_upload_id:
        try:
            row = get_upload_by_id(int(selected_upload_id), email)
            if row:
                return row
        except Exception:
            pass
    uploads = get_uploads_for_user(email)
    if not uploads:
        return None
    latest_id = uploads[0][0]
    return get_upload_by_id(latest_id, email)


def _get_recent_upload_choices(email, limit=2):
    uploads = get_uploads_for_user(email) if email else []
    out = []
    for u in uploads[:limit]:
        out.append({"id": u[0], "filename": u[1], "created_at": u[2]})
    return out


def _get_latest_resume_context(email, selected_upload_id=None):
    try:
        row = _get_resume_row_for_context(email, selected_upload_id=selected_upload_id)
        if not row:
            return ""
        resume_text = (row[3] or "").strip()
        if not resume_text:
            return ""
        return resume_text[:2200]
    except Exception:
        return ""


def _get_latest_analysis_context(email, selected_upload_id=None):
    def _json_safe(value, default):
        try:
            return json.loads(value) if value else default
        except Exception:
            return default

    try:
        row = _get_resume_row_for_context(email, selected_upload_id=selected_upload_id)
        if not row:
            return {}

        # row: id, filename, summary, resume_text, created_at, insights_json, jobs_json
        summary = (row[2] or "").strip()
        resume_text = (row[3] or "").strip()
        insights = _json_safe(row[5], {})
        jobs = _json_safe(row[6], [])
        scores = compute_scores(resume_text) if resume_text else {}

        top_jobs = []
        for job in jobs[:5]:
            if isinstance(job, dict):
                top_jobs.append(
                    {
                        "title": job.get("title", ""),
                        "company": job.get("company", ""),
                        "location": job.get("location", ""),
                        "match_score": job.get("match_score", 0),
                    }
                )

        ctx = {
            "summary": summary,
            "skills": extract_keywords_from_resume(resume_text, max_keywords=10),
            "ats_score": scores.get("ats_score", 0),
            "resume_score": scores.get("resume_score", 0),
            "skill_gaps": (insights.get("gaps", []) if isinstance(insights, dict) else [])[:6],
            "strengths": (insights.get("strengths", []) if isinstance(insights, dict) else [])[:6],
            "weaknesses": (insights.get("weaknesses", []) if isinstance(insights, dict) else [])[:6],
            "recommendations": (insights.get("recommendations", []) if isinstance(insights, dict) else [])[:6],
            "job_matches": top_jobs,
        }
        return ctx
    except Exception:
        return {}


def _build_resume_context_text(analysis_context):
    if not isinstance(analysis_context, dict) or not analysis_context:
        return "No resume data available."
    score = analysis_context.get("ats_score", "N/A")
    summary = analysis_context.get("summary", "")
    strengths = ", ".join(analysis_context.get("strengths", []) or [])
    weaknesses = ", ".join(analysis_context.get("weaknesses", []) or [])
    gaps = ", ".join(analysis_context.get("skill_gaps", []) or [])
    recs = ", ".join(analysis_context.get("recommendations", []) or [])
    roles = []
    for j in (analysis_context.get("job_matches", []) or [])[:5]:
        if isinstance(j, dict):
            t = j.get("title", "")
            c = j.get("company", "")
            m = j.get("match_score", 0)
            if t:
                roles.append(f"{t} at {c} ({m}%)")
    roles_text = ", ".join(roles)
    return (
        "CANDIDATE RESUME ANALYSIS:\n"
        f"- ATS Score: {score}/100\n"
        f"- Professional Summary: {summary}\n"
        f"- Key Strengths: {strengths}\n"
        f"- Weaknesses: {weaknesses}\n"
        f"- Skill Gaps: {gaps}\n"
        f"- Recommendations: {recs}\n"
        f"- Job Matches: {roles_text}"
    )


def _sanitize_chat_history(history):
    out = []
    if not isinstance(history, list):
        return out
    for msg in history[-10:]:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).strip().lower()
        content = str(msg.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            out.append({"role": role, "content": content[:2000]})
    return out


def _raya_llm_answer(question, analysis_context, chat_history, use_resume_context=True):
    q = (question or "").strip()
    if not q or not USE_LLM:
        return ""

    resume_ctx = _build_resume_context_text(analysis_context) if use_resume_context else "Not required for this question."
    if use_resume_context:
        system_prompt = (
            "You are Raya, an expert AI Career Mentor. "
            "Use resume analysis context for personalized guidance. "
            "Answer clearly, concisely, and actionably in short bullet points."
        )
        user_prompt = (
            f"Resume Context:\n{resume_ctx}\n\n"
            f"User Question:\n{q}\n\n"
            "Instructions: Personalize answer using context. "
            "If question is general, still answer directly and practically."
        )
    else:
        system_prompt = (
            "You are Raya, a smart general assistant. "
            "Answer any question accurately in simple concise style (4-8 lines)."
        )
        user_prompt = q

    try:
        if LLM_PROVIDER == "openai" and OPENAI_API_KEY:
            from openai import OpenAI  # type: ignore

            client = OpenAI(api_key=OPENAI_API_KEY)
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(_sanitize_chat_history(chat_history))
            messages.append({"role": "user", "content": user_prompt})
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                temperature=0.3,
                max_tokens=320,
            )
            return (response.choices[0].message.content or "").strip()

        if LLM_PROVIDER == "ollama":
            history_text = ""
            for h in _sanitize_chat_history(chat_history):
                role = "User" if h["role"] == "user" else "Assistant"
                history_text += f"{role}: {h['content']}\n"
            prompt = f"{system_prompt}\n\nConversation History:\n{history_text}\nUser: {user_prompt}\nAssistant:"
            resp = requests.post(
                f"{OLLAMA_HOST}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.25, "num_predict": 300, "num_ctx": 3072},
                },
                timeout=min(18, OLLAMA_TIMEOUT),
            )
            resp.raise_for_status()
            return (resp.json().get("response") or "").strip()
    except Exception:
        return ""
    return ""


def _is_resume_related_question(question):
    q = (question or "").lower()
    # Ask resume-selection only for analysis-specific questions,
    # not for general navigation/how-to questions.
    general_flow = [
        "how do i upload",
        "how to upload",
        "upload resume",
        "login",
        "sign in",
        "create resume",
        "resume builder",
        "interview questions",
        "where am i",
        "which page",
    ]
    if any(k in q for k in general_flow):
        return False

    analysis_keys = [
        "strength",
        "weakness",
        "gap",
        "ats score",
        "resume score",
        "job match",
        "jobs match",
        "recommend jobs",
        "recommended job",
        "recommended jobs",
        "skills should i improve",
        "improve my ats",
        "improve my resume",
        "improve resume",
        "summary of my resume",
        "my resume summary",
        "project ideas based on my profile",
        "software engineering roles",
        "job roles match",
        "match my skills",
        "suggest improvements",
    ]
    return any(k in q for k in analysis_keys)


def _is_public_auth_page(path):
    p = (path or "").strip().lower()
    return p in {"", "/", "/auth", "/register"}


def _raya_navigation_help(question, current_path):
    q = (question or "").lower()
    path = (current_path or "").lower()

    upload_keys = ["upload resume", "how to upload", "how do i upload", "upload pdf", "analyze resume", "analyse resume"]
    create_keys = ["create resume", "build resume", "resume builder"]
    interview_keys = ["interview question", "interview questions", "qa", "q&a"]
    history_keys = ["upload history", "history", "previous reports", "past reports"]
    login_keys = ["login", "sign in"]

    if any(k in q for k in upload_keys):
        return (
            "To upload resume: Login -> Choose 'Upload Resume' -> Select PDF -> Click 'Analyze Resume'. "
            "If needed, tick job recommendations and set location."
        )
    if any(k in q for k in create_keys):
        return (
            "To create resume: Login -> Workspace -> 'Create Resume' -> Fill Form or switch to Markdown -> "
            "Click 'Download PDF'."
        )
    if any(k in q for k in interview_keys):
        return (
            "To get interview Q&A: Login -> Workspace -> 'Get Interview Questions' -> Upload resume PDF -> "
            "Set count -> Generate Questions."
        )
    if any(k in q for k in history_keys):
        return "Go to Resume Review page and click 'View Upload History' to see previous uploads and downloads."
    if any(k in q for k in login_keys):
        return "From landing page click 'Start Your Journey Today', then enter email and password to login."

    if "where am i" in q or "which page" in q:
        page = path or "/"
        return f"You are currently on: {page}"
    return ""


def _raya_project_help(question):
    q = (question or "").lower()

    if any(k in q for k in ["what frontend", "frontend used", "front end used", "frontend technology", "frontend tech stack", "ui stack"]):
        return "Frontend is built with HTML templates + CSS + JavaScript (Flask Jinja templates in /templates and styling in /static/style.css)."
    if any(k in q for k in ["what backend", "backend used", "backend technology", "backend tech stack", "server stack", "flask used"]):
        return "Backend is Python Flask in app.py with utility modules under /utils for auth, parsing, LLM, scoring, embeddings, and LinkedIn jobs."
    if any(k in q for k in ["what database", "database used", "data stored", "where data", "where is data stored"]):
        return "Data is stored in SQLite (database/users.db): users, upload history, summaries, resume text, insights, and job results."
    if any(k in q for k in ["pinecone used", "use pinecone", "vector db", "vector database", "embedding used", "how embedding works", "how pinecone works"]):
        return "Yes, Pinecone is used. Resume text is embedded and stored for vector retrieval workflows (init/store via utils/pinecone_db.py)."
    if any(k in q for k in ["llm used", "which llm", "what llm", "model used", "ollama used", "openai used"]):
        return "LLM is integrated via utils/llm_analyzer.py. Current default is Ollama local model; OpenAI is also supported by config."
    if any(k in q for k in ["workflow", "how it works", "pipeline"]):
        return (
            "Workflow: Login -> Upload PDF -> Extract text -> LLM summary/insights -> ATS & section scores -> "
            "optional LinkedIn job fetch -> ranking -> result page -> PDF/report history."
        )
    if any(k in q for k in ["job recommendation", "linkedin", "scraping"]):
        return (
            "Job recommendations are generated from LinkedIn jobs (Selenium/API strategy), then ranked against resume skills/keywords and strengths."
        )
    if any(k in q for k in ["interview question", "interview qa", "q&a"]):
        return "Interview Q&A module analyzes uploaded resume and generates role-relevant questions with model answers."
    if any(k in q for k in ["create resume", "resume builder", "markdown"]):
        return "Create Resume supports Form and Markdown modes. Form data auto-generates markdown; markdown can be edited and downloaded as PDF."
    if any(k in q for k in ["error 500", "fpdf", "not enough horizontal space"]):
        return "That error is from PDF rendering width constraints. The app includes safe text wrapping and section rendering to avoid it."
    return ""


def _raya_general_fallback(question):
    q = (question or "").strip()
    ql = q.lower()

    if ("frontend" in ql or "front end" in ql or "ui" in ql) and any(k in ql for k in ["improve", "learn", "be better", "master"]):
        return (
            "To improve frontend:\n"
            "1) Strengthen HTML, CSS, JavaScript fundamentals\n"
            "2) Learn responsive design, Flexbox, Grid, and accessibility\n"
            "3) Practice DOM, APIs, forms, and state handling\n"
            "4) Build 3 real UI projects and clone 1 production-style page\n"
            "5) Then move to a framework like React and focus on clean component design"
        )

    if "important topics in python" in ql or ("python" in ql and "topic" in ql):
        return (
            "Important Python topics:\n"
            "1) Data types, control flow, functions, OOP\n"
            "2) Lists/dicts/sets/tuples and comprehensions\n"
            "3) Exception handling, file handling, modules/packages\n"
            "4) Iterators/generators, decorators, context managers\n"
            "5) NumPy/Pandas basics, APIs, and testing (pytest)"
        )

    if "java" in ql and "important topic" in ql:
        return (
            "Important Java topics:\n"
            "1) OOP, collections, exceptions, generics\n"
            "2) Multithreading and concurrency basics\n"
            "3) Streams, lambda expressions, Java 8+\n"
            "4) JDBC, REST APIs, Spring Boot basics\n"
            "5) Testing (JUnit), design patterns, JVM basics"
        )

    if "sql" in ql and ("important" in ql or "topic" in ql):
        return (
            "Important SQL topics:\n"
            "1) Joins, GROUP BY, HAVING, subqueries\n"
            "2) Indexes, normalization, transactions (ACID)\n"
            "3) Window functions and CTEs\n"
            "4) Query optimization and execution plans\n"
            "5) Constraints, views, stored procedures"
        )
    if ql in {"define python", "what is python", "explain python"}:
        return (
            "Python is a high-level, interpreted programming language known for simple syntax and readability. "
            "It is widely used in web development, automation, data science, AI/ML, scripting, and backend APIs."
        )
    if ql in {"define machine learning", "what is machine learning", "explain machine learning"}:
        return (
            "Machine Learning is a branch of AI where models learn patterns from data to make predictions or decisions "
            "without being explicitly programmed for every rule."
        )
    if ("difference" in ql or "diff" in ql) and "python" in ql and "java" in ql:
        return (
            "Python vs Java (beginner view):\n"
            "1) Python is simpler and faster to write; Java is more verbose but strongly structured.\n"
            "2) Python is popular in data science/AI/automation; Java is strong in enterprise/backend/Android.\n"
            "3) Python is dynamically typed; Java is statically typed.\n"
            "4) Java usually gives better runtime performance; Python gives faster development speed."
        )
    if "improve my knowledge in data science" in ql or ("data science" in ql and "improve" in ql):
        return (
            "To improve in Data Science:\n"
            "1) Strengthen Python + SQL + statistics\n"
            "2) Learn Pandas, NumPy, Matplotlib, scikit-learn\n"
            "3) Build 3 end-to-end projects with real datasets\n"
            "4) Practice EDA, feature engineering, model evaluation\n"
            "5) Publish work on GitHub with clear README and results"
        )
    if (
        ("course" in ql or "courses" in ql) and
        ("data analytics" in ql or "data analysis" in ql or "data analyst" in ql)
    ):
        return (
            "Best courses for Data Analytics (practical track):\n"
            "1) Google Data Analytics Professional Certificate (Coursera)\n"
            "2) IBM Data Analyst Professional Certificate (Coursera)\n"
            "3) Microsoft Power BI Data Analyst (PL-300) path\n"
            "4) SQL + Excel + Python (Pandas) specialization track\n"
            "5) Tableau Data Analytics path (official learning)\n"
            "Tip: pick one end-to-end track and build 2 portfolio projects."
        )
    if ("course" in ql or "courses" in ql) and "python" in ql:
        return (
            "Best Python learning path:\n"
            "1) Python for Everybody (Coursera)\n"
            "2) Automate the Boring Stuff with Python\n"
            "3) Python + SQL + APIs mini-project track\n"
            "4) Practice on LeetCode/HackerRank for fundamentals\n"
            "5) Build 3 projects and publish on GitHub."
        )
    return ""


def _raya_gemini_search_answer(question):
    q = (question or "").strip()
    if not q or not GEMINI_API_KEY:
        return ""

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "Answer the user question accurately and briefly (4-8 lines). "
        "If it is educational/technical, give practical points. "
        "Question: " + q
    )

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 280},
        "tools": [{"google_search": {}}],
    }
    try:
        resp = requests.post(endpoint, json=payload, timeout=GEMINI_TIMEOUT)
        if not resp.ok:
            return ""
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            return ""
        parts = (((cands[0].get("content") or {}).get("parts")) or [])
        text = "\n".join((p.get("text") or "").strip() for p in parts if isinstance(p, dict)).strip()
        return text
    except Exception:
        return ""


def _raya_search_engine_answer(question):
    q = (question or "").strip()
    if len(q) < 3:
        return ""
    ql = q.lower()
    # Avoid poor search snippets for short follow-ups like "beginner"/"advanced".
    if ql in {"beginner", "advanced", "interview", "interview-focused", "simple"}:
        return ""

    headers = {"User-Agent": "Mozilla/5.0 (RayaBot/1.0)"}

    # 1) DuckDuckGo instant answer
    try:
        ddg_url = "https://api.duckduckgo.com/"
        resp = requests.get(
            ddg_url,
            params={"q": q, "format": "json", "no_redirect": 1, "no_html": 1},
            headers=headers,
            timeout=6,
        )
        if resp.ok:
            data = resp.json()
            answer = (data.get("Answer") or "").strip()
            abstract = (data.get("AbstractText") or "").strip()
            if answer:
                return answer
            if abstract:
                return abstract
            related = data.get("RelatedTopics") or []
            for item in related:
                if isinstance(item, dict):
                    txt = (item.get("Text") or "").strip()
                    if txt:
                        return txt
                    nested = item.get("Topics") or []
                    for n in nested:
                        if isinstance(n, dict):
                            t = (n.get("Text") or "").strip()
                            if t:
                                return t
    except Exception:
        pass

    # 1b) DuckDuckGo HTML snippet fallback
    try:
        html_resp = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": q},
            headers=headers,
            timeout=6,
        )
        if html_resp.ok and html_resp.text:
            html = html_resp.text
            html = re.sub(r"\s+", " ", html)
            m = re.search(r'result__snippet[^>]*>(.*?)</a>', html, flags=re.IGNORECASE)
            if not m:
                m = re.search(r'class="result__snippet"[^>]*>(.*?)</[^>]+>', html, flags=re.IGNORECASE)
            if m:
                snippet = re.sub(r"<.*?>", "", m.group(1)).strip()
                if snippet:
                    return snippet
    except Exception:
        pass

    # 2) Wikipedia quick summary
    try:
        search_url = "https://en.wikipedia.org/w/api.php"
        s_resp = requests.get(
            search_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": q,
                "format": "json",
                "srlimit": 1,
            },
            headers=headers,
            timeout=6,
        )
        if s_resp.ok:
            s_data = s_resp.json()
            items = (((s_data.get("query") or {}).get("search")) or [])
            if items:
                title = items[0].get("title", "").strip()
                if title:
                    summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}"
                    p_resp = requests.get(summary_url, headers=headers, timeout=6)
                    if p_resp.ok:
                        p_data = p_resp.json()
                        extract = (p_data.get("extract") or "").strip()
                        if extract:
                            return extract
    except Exception:
        pass

    return ""


def _raya_skill_priority_response(analysis_context, resume_context):
    gaps = (analysis_context.get("skill_gaps", []) if isinstance(analysis_context, dict) else []) or []
    weaknesses = (analysis_context.get("weaknesses", []) if isinstance(analysis_context, dict) else []) or []
    skills = (analysis_context.get("skills", []) if isinstance(analysis_context, dict) else []) or []
    ats = analysis_context.get("ats_score", 0) if isinstance(analysis_context, dict) else 0

    if gaps:
        top = gaps[:3]
        return (
            f"Based on your latest analysis (ATS {ats}/100), improve these first:\n"
            f"1) {top[0]}\n"
            f"2) {top[1] if len(top) > 1 else 'Strengthen project depth with measurable outcomes'}\n"
            f"3) {top[2] if len(top) > 2 else 'Add role-specific tools and frameworks'}"
        )

    if weaknesses:
        top = weaknesses[:3]
        return "Start with these improvement priorities:\n" + "\n".join(
            [f"{i+1}) {w}" for i, w in enumerate(top)]
        )

    if skills:
        top = skills[:4]
        return (
            f"Your resume shows these core skills: {', '.join(top)}.\n"
            "To improve faster, add one advanced project, one certification, and one interview-ready explanation for each top skill."
        )

    if resume_context:
        kw = extract_keywords_from_resume(resume_context, max_keywords=4)
        if kw:
            return (
                f"From your resume text, prioritize: {', '.join(kw)}.\n"
                "Then strengthen ATS format, quantified project results, and role-specific keywords."
            )

    return (
        "I need a recent analyzed resume to prioritize skills accurately. "
        "Please upload and analyze your resume first, then ask this again."
    )


def _raya_suggested_answer(question_lower, analysis_context, resume_context):
    q = (question_lower or "").strip()

    if q in {"how do i upload my resume?", "how to upload my resume?"}:
        return (
            "Steps:\n"
            "1) Login from the Auth page.\n"
            "2) Open Workspace -> Upload Resume.\n"
            "3) Select your PDF.\n"
            "4) Click Analyze Resume.\n"
            "5) View summary, ATS score, insights, and job recommendations."
        )

    if q in {"which skills should i improve first?", "which skills should i improve first"}:
        return _raya_skill_priority_response(analysis_context, resume_context)

    if q in {"give me 3 project ideas based on my profile", "give me 3 project ideas based on my profile."}:
        skills = (analysis_context.get("skills", []) if isinstance(analysis_context, dict) else [])[:4]
        if not skills and resume_context:
            skills = extract_keywords_from_resume(resume_context, max_keywords=4)
        base = ", ".join(skills) if skills else "your current profile"
        return (
            f"3 project ideas based on {base}:\n"
            "1) Resume + Job Match Dashboard: parse resume, score ATS, and rank jobs.\n"
            "2) Interview Practice Assistant: generate domain Q&A and track weak topics.\n"
            "3) Skill Gap Tracker: compare resume skills vs job descriptions and recommend weekly plan."
        )

    if q in {"how can i improve my ats score?", "how can i improve my ats score"}:
        ats = analysis_context.get("ats_score", 0) if isinstance(analysis_context, dict) else 0
        gaps = (analysis_context.get("skill_gaps", []) if isinstance(analysis_context, dict) else [])[:2]
        gap_line = f"Priority gaps: {', '.join(gaps)}." if gaps else "Add role-specific keywords from 3 target job descriptions."
        return (
            f"ATS improvement plan (current ATS: {ats}/100):\n"
            "1) Use clear headings: Summary, Skills, Projects, Education.\n"
            "2) Add exact role keywords naturally in skills/projects.\n"
            "3) Quantify achievements (%, numbers, impact).\n"
            f"4) {gap_line}\n"
            "5) Keep formatting simple (no tables/images in resume PDF)."
        )

    if q in {"create a 7-day interview preparation plan", "create a 7-day interview preparation plan."}:
        return (
            "7-day plan:\n"
            "Day 1: Resume deep review + 2-minute self-introduction.\n"
            "Day 2: Core technical revision + 20 questions.\n"
            "Day 3: Project explanation practice (problem -> action -> result).\n"
            "Day 4: DSA/role-specific coding + mock round.\n"
            "Day 5: System/design basics + debugging scenarios.\n"
            "Day 6: HR/behavioral answers with STAR method.\n"
            "Day 7: Full mock interview + final refinement."
        )

    return ""


def _raya_analysis_answer(question_lower, analysis_context, resume_context):
    q = (question_lower or "").strip()
    strengths = (analysis_context.get("strengths", []) if isinstance(analysis_context, dict) else []) or []
    weaknesses = (analysis_context.get("weaknesses", []) if isinstance(analysis_context, dict) else []) or []
    gaps = (analysis_context.get("skill_gaps", []) if isinstance(analysis_context, dict) else []) or []
    recs = (analysis_context.get("recommendations", []) if isinstance(analysis_context, dict) else []) or []
    jobs = (analysis_context.get("job_matches", []) if isinstance(analysis_context, dict) else []) or []
    ats = analysis_context.get("ats_score", 0) if isinstance(analysis_context, dict) else 0
    rs = analysis_context.get("resume_score", 0) if isinstance(analysis_context, dict) else 0
    skills = (analysis_context.get("skills", []) if isinstance(analysis_context, dict) else []) or []

    if any(k in q for k in ["strength", "strengths of my resume", "my strengths"]):
        if strengths:
            return "Top strengths from your latest analysis:\n- " + "\n- ".join(strengths[:5])
        if skills:
            return "Likely strengths from your profile:\n- " + "\n- ".join(skills[:5])
        return "I need your latest analyzed resume to identify strengths accurately. Please upload and analyze once."

    if any(k in q for k in ["weakness", "weaknesses"]):
        if weaknesses:
            return "Top weaknesses from your latest analysis:\n- " + "\n- ".join(weaknesses[:5])
        return "No weaknesses were detected in stored analysis. Re-run analysis for updated details."

    if any(k in q for k in ["gap", "skill gap", "gaps"]):
        if gaps:
            return "Skill gaps from your latest analysis:\n- " + "\n- ".join(gaps[:5])
        return "No skill gaps found in stored analysis. Upload latest resume for better gap detection."

    if any(k in q for k in ["recommendation", "suggestion", "suggest improvements", "improve resume", "improve my resume", "software engineering roles", "software engineer role"]):
        if recs:
            return "Personalized recommendations:\n- " + "\n- ".join(recs[:5])
        if skills:
            return (
                "To improve your resume for software engineering roles:\n"
                f"1) Highlight core skills first: {', '.join(skills[:6])}\n"
                "2) Add quantified project outcomes (accuracy, performance, scale).\n"
                "3) Keep role-specific keywords in Summary, Skills, and Projects."
            )
        return "No stored recommendations found. Please analyze your resume to generate personalized suggestions."

    if any(k in q for k in ["ats score", "ats"]):
        score_only = ("what" in q or "current" in q or "show" in q) and not any(
            k in q for k in ["improve", "increase", "better", "optimize"]
        )
        if score_only:
            return f"Your latest ATS score is {ats}/100." if ats else "ATS score not found. Please analyze your resume first."

        gaps_text = ", ".join(gaps[:2]) if gaps else ""
        gap_line = f"Priority gaps: {gaps_text}." if gaps_text else "Add role-specific keywords from target job descriptions."
        if ats:
            return (
                f"Your latest ATS score is {ats}/100.\n"
                "To improve further:\n"
                "1) Keep clear section headings and simple formatting.\n"
                "2) Add exact role keywords in Skills + Projects.\n"
                "3) Quantify achievements (numbers/%/impact).\n"
                f"4) {gap_line}"
            )
        return (
            "ATS score not found yet.\n"
            "Analyze your resume first, then improve headings, keywords, and quantified project bullets."
        )

    if any(k in q for k in ["resume score", "overall score"]):
        return f"Your latest resume score is {rs}/100." if rs else "Resume score not found. Please analyze your resume first."

    if any(k in q for k in ["job match", "jobs match", "job roles match", "job recommendation", "recommended jobs", "which jobs match", "recommend jobs", "based on my skills", "match my skills"]):
        if jobs:
            lines = []
            for j in jobs[:3]:
                lines.append(f"- {j.get('title','Role')} at {j.get('company','Company')} ({j.get('match_score',0)}%)")
            return "Top job matches:\n" + "\n".join(lines)
        if skills:
            return (
                "No stored job matches found for this resume.\n"
                f"Based on your skills ({', '.join(skills[:6])}), target roles:\n"
                "- Software Developer\n"
                "- Python Developer\n"
                "- Data Analyst / ML Associate\n"
                "For real job links, upload resume with 'Include job recommendations' enabled."
            )
        return "No job matches found in latest analysis. Enable job recommendations during upload."

    if any(k in q for k in ["summary of my resume", "profile summary", "summarize my resume"]):
        s = (analysis_context.get("summary", "") if isinstance(analysis_context, dict) else "").strip()
        if s:
            return f"Latest summary:\n{s}"
        if resume_context:
            return "Summary is not stored, but resume text is available. Re-run analysis to get fresh summary."
        return "No summary found. Please upload and analyze your resume first."

    return ""


def _raya_reply(question, user_email=None, current_path="", selected_upload_id=None, chat_history=None):
    q = (question or "").strip()
    if not q:
        return "Please ask a question."
    ql = q.lower()
    history = chat_history if isinstance(chat_history, list) else []
    is_resume_q = _is_resume_related_question(q)

    if ql in {"hi", "hello", "hey", "hii", "hola"}:
        return "Hi, I am Raya. Ask me about upload flow, resume analysis, interview Q&A, or project details."
    if "thank" in ql:
        return "You're welcome. Ask your next question."

    project_help = _raya_project_help(q)
    if project_help:
        return project_help

    nav_help = _raya_navigation_help(q, current_path)
    if nav_help:
        return nav_help

    resume_context = _get_latest_resume_context(user_email, selected_upload_id=selected_upload_id) if user_email else ""
    analysis_context = _get_latest_analysis_context(user_email, selected_upload_id=selected_upload_id) if user_email else {}
    suggested_answer = _raya_suggested_answer(ql, analysis_context, resume_context)
    if suggested_answer:
        return suggested_answer

    if not is_resume_q:
        general_answer = _raya_general_fallback(q)
        if general_answer:
            return general_answer
        gemini_answer = _raya_gemini_search_answer(q)
        if gemini_answer:
            return gemini_answer
        llm_general = _raya_llm_answer(q, {}, history, use_resume_context=False)
        if llm_general:
            return llm_general
        search_answer = _raya_search_engine_answer(q)
        if search_answer:
            return search_answer
        return (
            "I could not find a reliable answer right now. "
            "Please rephrase your question in one line, and I will answer directly."
        )

    analysis_answer = _raya_analysis_answer(ql, analysis_context, resume_context)
    if analysis_answer:
        return analysis_answer

    if any(k in ql for k in ["skill gap", "skills improve", "improve skills", "what should i learn", "which skills should i improve"]):
        return _raya_skill_priority_response(analysis_context, resume_context)
    if analysis_context and any(k in ql for k in ["project idea", "project ideas"]):
        base_skills = analysis_context.get("skills", [])[:4]
        if base_skills:
            return (
                f"Personalized project ideas using your skills ({', '.join(base_skills)}):\n"
                "- Build a portfolio project with measurable outcome and GitHub README.\n"
                "- Build a mini end-to-end app (backend + frontend + DB).\n"
                "- Add one ATS-friendly project with quantified impact."
            )
    if analysis_context and any(k in ql for k in ["interview", "prepare interview", "interview tips"]):
        ats = analysis_context.get("ats_score", 0)
        return (
            f"Interview prep plan based on your profile (ATS {ats}/100):\n"
            "- Prepare 3 project deep-dive stories (problem, action, result).\n"
            "- Practice top technical skills from your resume.\n"
            "- Prepare strengths/weaknesses with real examples."
        )

    if not user_email:
        return "Please login and analyze your resume first. Then I can answer with personalized insights."

    llm_resume = _raya_llm_answer(q, analysis_context, history, use_resume_context=True)
    if llm_resume:
        return llm_resume

    if resume_context:
        return (
            "I found your latest analyzed resume, but could not generate a high-confidence response right now. "
            "Please retry the same question once."
        )
    return "No analyzed resume found yet. Upload a resume and click Analyze, then ask again."


@app.route("/raya-chat", methods=["POST"])
def raya_chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    context_question = (payload.get("context_question") or "").strip()
    chat_history = payload.get("history") or []
    if message.lower() in {"beginner", "advanced", "interview", "interview-focused", "simple"} and context_question:
        message = f"Explain this in {message.lower()} level: {context_question}"
    current_path = (payload.get("path") or "").strip()
    selected_upload_id = payload.get("selected_upload_id")
    user_email = session.get("user")

    if _is_public_auth_page(current_path) and _is_resume_related_question(message):
        return jsonify(
            {
                "reply": (
                    "Please login first. Then upload and analyze your resume, "
                    "and I will give ATS/strengths/gaps based guidance."
                ),
                "needs_selection": False,
            }
        )

    choices = _get_recent_upload_choices(user_email, limit=2)
    if (
        _is_resume_related_question(message)
        and not selected_upload_id
        and len(choices) >= 2
    ):
        return jsonify(
            {
                "reply": "I found two recent resumes. Please select which resume you want me to use.",
                "needs_selection": True,
                "options": choices,
            }
        )

    reply = _raya_reply(
        message,
        user_email=user_email,
        current_path=current_path,
        selected_upload_id=selected_upload_id,
        chat_history=chat_history,
    )
    return jsonify({"reply": reply, "needs_selection": False})

def format_summary_html(text):
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out = []
    in_list = False
    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br>")
            continue

        m = re.match(r"^\*\*(.+?)\*\*:?$", stripped)
        if m:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<strong>{escape(m.group(1))}:</strong><br>")
            continue

        if stripped.endswith(":") and len(stripped) < 40:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<strong>{escape(stripped)}</strong><br>")
            continue

        if stripped.startswith(("-", "•", "*")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = stripped.lstrip("-•* ").strip()
            out.append(f"<li>{escape(item)}</li>")
            continue

        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"{escape(stripped)}<br>")

    if in_list:
        out.append("</ul>")
    return Markup("".join(out))


def clean_summary_text(text):
    if not text:
        return ""
    txt = text.replace("```json", "").replace("```", "").strip()
    txt = re.sub(r"^Here is .*?:\s*", "", txt, flags=re.IGNORECASE)
    if '"summary"' in txt and '"insights"' in txt:
        m = re.search(r'"summary"\s*:\s*"(.+?)"\s*,\s*"insights"', txt, flags=re.DOTALL)
        if m:
            txt = m.group(1).replace("\\n", "\n").replace('\\"', '"')
    # Normalize common unicode punctuation to avoid render issues.
    txt = (
        txt.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2022", "-")
    )
    return txt.strip()


def _pdf_safe_text(text):
    if text is None:
        return ""
    txt = str(text)
    txt = (
        txt.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2022", "-")
    )
    txt = txt.encode("latin-1", "replace").decode("latin-1")
    txt = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _split_multiline(value):
    raw = (value or "").strip()
    if not raw:
        return []
    lines = []
    for ln in raw.splitlines():
        item = ln.strip().lstrip("-* ").strip()
        if item:
            lines.append(item)
    return lines


def _generate_resume_pdf(data):
    template = data.get("template", "classic")
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    if template in {"structured_pro", "praveen_exact", "praveen"}:
        return _generate_resume_pdf_praveen(pdf, data, template)

    if template in {"premium2", "two_column", "two-column", "2col"}:
        return _generate_resume_pdf_two_column(pdf, data, template)

    if template == "modern":
        title_rgb = (10, 52, 130)
        section_rgb = (18, 88, 179)
    elif template == "compact":
        title_rgb = (15, 15, 15)
        section_rgb = (45, 45, 45)
    else:
        title_rgb = (18, 18, 18)
        section_rgb = (26, 26, 26)

    def section(title):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*section_rgb)
        pdf.cell(0, 7, _pdf_safe_text(title.upper()), ln=True)
        pdf.set_draw_color(170, 170, 170)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)
        pdf.set_text_color(0, 0, 0)

    def paragraph(text, size=10):
        if not text:
            return
        pdf.set_font("Helvetica", "", size)
        _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(text)), h=5.5)

    def bullets(items):
        items = items or []
        if not items:
            return
        pdf.set_font("Helvetica", "", 10)
        for item in items:
            _safe_multi(pdf, _pdf_safe_text("- " + _wrap_long_tokens(item)), h=5.5)

    def key_line(label, value):
        if not value:
            return
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(40, 5.5, _pdf_safe_text(label))
        pdf.set_font("Helvetica", "", 10)
        _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(value)), h=5.5)

    # Header block similar to professional ATS resume
    full_name = data.get("full_name") or "Candidate Name"
    role_hint = "Resume Profile"
    if data.get("degree"):
        role_hint = data["degree"]
    elif data.get("bio"):
        role_hint = "Professional Profile"

    pdf.set_text_color(*title_rgb)
    pdf.set_font("Helvetica", "B", 20 if template != "compact" else 18)
    pdf.cell(0, 9, _pdf_safe_text(full_name.upper()), ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, _pdf_safe_text(role_hint), ln=True)
    pdf.set_text_color(0, 0, 0)

    contact_items = []
    if data.get("phone"):
        contact_items.append(data["phone"])
    if data.get("email"):
        contact_items.append(data["email"])
    if data.get("linkedin"):
        contact_items.append("LinkedIn")
    if data.get("github"):
        contact_items.append("GitHub")
    contact = " | ".join(contact_items)
    if contact:
        paragraph(contact, size=10)
    if data.get("address"):
        paragraph(data["address"], size=10)
    if data.get("dob"):
        paragraph(f"DOB: {data['dob']}", size=10)
    pdf.ln(1)

    # SUMMARY
    section("Summary")
    paragraph(data.get("bio") or "Motivated candidate with strong learning ability and project exposure.")

    # PROJECTS
    section("Projects")
    projects = _split_multiline(data.get("projects"))
    if projects:
        bullets(projects)
    else:
        bullets(_split_multiline(data.get("experience")))

    # EDUCATION
    section("Education")
    key_line("College:", data.get("college"))
    key_line("Degree:", data.get("degree"))

    # TECHNICAL SKILLS
    section("Technical Skills")
    bullets(_split_multiline(data.get("skills")))

    # ADDITIONAL INFORMATION
    section("Additional Information")
    bullets(_split_multiline(data.get("certificates")))
    if data.get("experience"):
        paragraph("Experience Highlights:")
        bullets(_split_multiline(data.get("experience")))

    file_name = secure_filename(f"{data.get('full_name', 'resume')}_{template}_resume.pdf")
    if not file_name:
        file_name = "generated_resume.pdf"
    pdf_bytes = pdf.output(dest="S")
    if isinstance(pdf_bytes, str):
        pdf_bytes = pdf_bytes.encode("latin-1", "replace")
    out = io.BytesIO(pdf_bytes)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name=file_name, mimetype="application/pdf")


def _generate_resume_pdf_praveen(pdf, data, template_name):
    def section(title):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 6, _pdf_safe_text(title.upper()), ln=True)
        pdf.set_draw_color(160, 160, 160)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(2)
        pdf.set_text_color(0, 0, 0)

    def bullet_lines(lines):
        pdf.set_font("Helvetica", "", 10)
        for ln in lines:
            _safe_multi(pdf, _pdf_safe_text("- " + _wrap_long_tokens(ln)), h=5.2)

    def centered_contact(text):
        if not text:
            return
        pdf.set_font("Helvetica", "", 10)
        safe_text = _pdf_safe_text(_wrap_long_tokens(text, chunk=28))
        # Avoid multi_cell(0, ...) edge-case width errors in fpdf by using wrapped centered lines.
        for line in textwrap.wrap(safe_text, width=70) or [safe_text]:
            pdf.set_x(pdf.l_margin)
            pdf.cell(0, 5, line, ln=True, align="C")

    full_name = _pdf_safe_text((data.get("full_name") or "CANDIDATE NAME").upper())
    role_hint = _pdf_safe_text(data.get("role_title") or data.get("degree") or "Computer Science Student")
    contact_items = [x for x in [data.get("phone"), data.get("email")] if x]
    if data.get("linkedin"):
        contact_items.append(f"LinkedIn: {data.get('linkedin')}")
    if data.get("github"):
        contact_items.append(f"GitHub: {data.get('github')}")

    # Header (centered, close to sample layout)
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 10, full_name, ln=True, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, role_hint, ln=True, align="C")
    for item in contact_items:
        centered_contact(item)
    pdf.ln(2)

    # SUMMARY
    section("Summary")
    pdf.set_font("Helvetica", "", 10)
    _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(data.get("bio") or "")), h=5.2)

    # EDUCATION
    section("Education")
    structured_edu = []
    for i in (1, 2):
        e_date = data.get(f"edu_{i}_date", "")
        e_degree = data.get(f"edu_{i}_degree", "")
        e_college = data.get(f"edu_{i}_college", "")
        e_score = data.get(f"edu_{i}_score", "")
        if e_degree or e_college:
            structured_edu.append((e_date, e_degree, e_college, e_score))
    if structured_edu:
        for e_date, e_degree, e_college, e_score in structured_edu:
            if e_date:
                pdf.set_font("Helvetica", "", 9.6)
                _safe_multi(pdf, _pdf_safe_text(e_date), h=5.0)
            if e_degree:
                pdf.set_font("Helvetica", "B", 10)
                _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(e_degree)), h=5.2)
            if e_college:
                pdf.set_font("Helvetica", "", 9.8)
                _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(e_college)), h=5.0)
            if e_score:
                pdf.set_font("Helvetica", "", 9.6)
                _safe_multi(pdf, _pdf_safe_text("• " + _wrap_long_tokens(e_score)), h=5.0)
            pdf.ln(1)
    else:
        edu_lines = []
        if data.get("degree"):
            edu_lines.append(data["degree"])
        if data.get("college"):
            edu_lines.append(data["college"])
        if data.get("dob"):
            edu_lines.append(f"DOB: {data['dob']}")
        bullet_lines(edu_lines or ["Education details not provided"])

    # TECHNICAL SKILLS
    section("Technical Skills")
    skill_blocks = []
    if data.get("skill_languages"):
        skill_blocks.append(f"Languages: {data.get('skill_languages')}")
    if data.get("skill_frontend_backend"):
        skill_blocks.append(f"Frontend & Backend: {data.get('skill_frontend_backend')}")
    if data.get("skill_database"):
        skill_blocks.append(f"Database: {data.get('skill_database')}")
    if data.get("skill_others"):
        skill_blocks.append(f"Others: {data.get('skill_others')}")
    if skill_blocks:
        bullet_lines(skill_blocks)
    else:
        skills = _split_multiline(data.get("skills"))
        bullet_lines(skills or ["Skills not provided"])

    # PROJECTS
    section("Projects")
    structured_projects = []
    for i in range(1, 5):
        title = data.get(f"project_{i}_title", "")
        desc = data.get(f"project_{i}_desc", "")
        if title:
            structured_projects.append((title, desc))
    if structured_projects:
        for title, desc in structured_projects:
            pdf.set_font("Helvetica", "B", 10)
            _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(title)), h=5.2)
            pdf.set_font("Helvetica", "", 10)
            if desc:
                _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(desc)), h=5.2)
            pdf.ln(1)
    else:
        projects = _split_multiline(data.get("projects")) or _split_multiline(data.get("experience"))
        if projects:
            bullet_lines(projects)
        else:
            bullet_lines(["Project details not provided"])

    # CERTIFICATES
    section("Certificates")
    cert_lines = _split_multiline(data.get("certificates"))
    cert_lines.extend(_split_multiline(data.get("add_certs")))
    bullet_lines(cert_lines or ["Certificates not provided"])

    # ADDITIONAL INFORMATION
    section("Additional Information")
    extra = []
    if data.get("add_soft_skills"):
        extra.append(("Soft Skills", data.get("add_soft_skills")))
    if data.get("add_languages"):
        extra.append(("Languages", data.get("add_languages")))
    if data.get("linkedin"):
        extra.append(("LinkedIn", data.get("linkedin")))
    if data.get("github"):
        extra.append(("GitHub", data.get("github")))
    if data.get("address"):
        extra.append(("Address", data.get("address")))
    if not extra:
        bullet_lines(["Additional details not provided"])
    else:
        for label, value in extra:
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(34, 5.2, _pdf_safe_text(f"{label}:"))
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(
                max(10, pdf.w - pdf.r_margin - pdf.get_x()),
                5.2,
                _pdf_safe_text(_wrap_long_tokens(value)),
            )

    file_name = secure_filename(f"{data.get('full_name', 'resume')}_{template_name}_resume.pdf")
    if not file_name:
        file_name = "generated_resume.pdf"
    pdf_bytes = pdf.output(dest="S")
    if isinstance(pdf_bytes, str):
        pdf_bytes = pdf_bytes.encode("latin-1", "replace")
    out = io.BytesIO(pdf_bytes)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name=file_name, mimetype="application/pdf")


def _generate_resume_pdf_two_column(pdf, data, template_name):
    page_w = pdf.w - pdf.l_margin - pdf.r_margin
    left_w = 62
    gap = 8
    right_w = page_w - left_w - gap
    left_x = pdf.l_margin
    right_x = left_x + left_w + gap

    def safe_lines(value):
        if not value:
            return []
        if isinstance(value, list):
            return [_pdf_safe_text(_wrap_long_tokens(v)) for v in value if str(v).strip()]
        return [_pdf_safe_text(_wrap_long_tokens(value))]

    def block(x, y, w, title, lines, bullet=False):
        if y > 268:
            pdf.add_page()
            y = 16
        pdf.set_xy(x, y)
        pdf.set_text_color(26, 26, 26)
        pdf.set_font("Helvetica", "B", 10)
        pdf.multi_cell(w, 6, _pdf_safe_text(title.upper()))
        y = pdf.get_y()
        pdf.set_draw_color(176, 176, 176)
        pdf.line(x, y, x + w, y)
        y += 2
        pdf.set_xy(x, y)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9.5)
        if not lines:
            pdf.multi_cell(w, 5, "-")
        else:
            for ln in lines:
                text = f"- {ln}" if bullet else ln
                pdf.set_x(x)
                try:
                    pdf.multi_cell(w, 5, _pdf_safe_text(text))
                except Exception:
                    pdf.multi_cell(w, 5, _pdf_safe_text(_wrap_long_tokens(text, chunk=10)))
        return pdf.get_y() + 2

    # Header
    full_name = _pdf_safe_text((data.get("full_name") or "Candidate Name").upper())
    role_hint = _pdf_safe_text(data.get("degree") or "Professional Resume")
    contact_items = []
    if data.get("phone"):
        contact_items.append(data["phone"])
    if data.get("email"):
        contact_items.append(data["email"])
    if data.get("linkedin"):
        contact_items.append(data["linkedin"])
    if data.get("github"):
        contact_items.append(data["github"])
    contact_line = " | ".join(contact_items)

    pdf.set_text_color(15, 23, 42)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, full_name, ln=True)
    pdf.set_font("Helvetica", "", 10.5)
    pdf.cell(0, 6, role_hint, ln=True)
    if contact_line:
        pdf.set_font("Helvetica", "", 9.5)
        _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(contact_line, chunk=28)), h=5)
    if data.get("address"):
        _safe_multi(pdf, _pdf_safe_text(_wrap_long_tokens(data.get("address"), chunk=28)), h=5)
    if data.get("dob"):
        pdf.cell(0, 5, _pdf_safe_text(f"DOB: {data['dob']}"), ln=True)
    pdf.ln(2)

    # Column content
    left_y = pdf.get_y()
    right_y = left_y

    left_y = block(left_x, left_y, left_w, "Technical Skills", _split_multiline(data.get("skills")), bullet=True)
    left_y = block(left_x, left_y, left_w, "Certificates", _split_multiline(data.get("certificates")), bullet=True)
    left_y = block(left_x, left_y, left_w, "Contact", safe_lines(data.get("phone")) + safe_lines(data.get("email")), bullet=False)
    left_y = block(left_x, left_y, left_w, "Links", safe_lines(data.get("linkedin")) + safe_lines(data.get("github")), bullet=False)

    right_y = block(right_x, right_y, right_w, "Summary", safe_lines(data.get("bio") or "Motivated candidate with project-based learning."), bullet=False)
    right_y = block(right_x, right_y, right_w, "Projects", _split_multiline(data.get("projects")) or _split_multiline(data.get("experience")), bullet=True)
    edu_lines = []
    if data.get("college"):
        edu_lines.append(f"College: {data.get('college')}")
    if data.get("degree"):
        edu_lines.append(f"Degree: {data.get('degree')}")
    right_y = block(right_x, right_y, right_w, "Education", edu_lines, bullet=False)
    if data.get("experience"):
        right_y = block(right_x, right_y, right_w, "Experience", _split_multiline(data.get("experience")), bullet=True)

    file_name = secure_filename(f"{data.get('full_name', 'resume')}_{template_name}_resume.pdf")
    if not file_name:
        file_name = "generated_resume.pdf"
    pdf_bytes = pdf.output(dest="S")
    if isinstance(pdf_bytes, str):
        pdf_bytes = pdf_bytes.encode("latin-1", "replace")
    out = io.BytesIO(pdf_bytes)
    out.seek(0)
    return send_file(out, as_attachment=True, download_name=file_name, mimetype="application/pdf")


def _wrap_long_tokens(text, chunk=35):
    s = str(text or "")
    parts = s.split()
    out = []
    for p in parts:
        if len(p) <= chunk:
            out.append(p)
            continue
        out.append(" ".join(p[i:i + chunk] for i in range(0, len(p), chunk)))
    return " ".join(out)


def _safe_multi(pdf, text, h=6):
    full_width = max(10, pdf.w - pdf.l_margin - pdf.r_margin)
    pdf.set_x(pdf.l_margin)
    safe = _pdf_safe_text(text)
    try:
        pdf.multi_cell(full_width, h, safe)
    except Exception:
        fallback = _pdf_safe_text(_wrap_long_tokens(str(safe), chunk=12))
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(full_width, h, fallback)


def _safe_json_load(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _filter_jobs_for_display(jobs):
    # Keep previous behavior: show both real and fallback jobs
    # so recommendations are always visible.
    return jobs or []


def _fallback_analysis(resume_text):
    keywords = extract_keywords_from_resume(resume_text, max_keywords=8)
    bullets = []
    for line in resume_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("-", "*", "•")):
            bullets.append(line.lstrip("-*• ").strip())
        if len(bullets) >= 3:
            break
    lines = [
        "Profile Summary:",
        "- Fallback summary generated because LLM timed out.",
    ]
    if keywords:
        lines.append("Technical Skills:")
        lines.append(f"- {', '.join(keywords[:6])}")
    if bullets:
        lines.append("Projects / Experience:")
        for b in bullets[:2]:
            lines.append(f"- {b}")
    return {"summary": "\n".join(lines), "insights": {}}


def _fallback_interview_qa(resume_text, count=8):
    keywords = extract_keywords_from_resume(resume_text, max_keywords=12)
    tops = keywords[: max(4, min(8, len(keywords)))]
    qa = []
    for i, kw in enumerate(tops, start=1):
        qa.append(
            {
                "question": f"Explain your practical experience with {kw}.",
                "answer": (
                    f"My resume highlights hands-on work related to {kw}. "
                    "I would explain one project scenario, my exact contribution, "
                    "the tools used, and the measurable outcome."
                ),
            }
        )
    while len(qa) < count:
        idx = len(qa) + 1
        qa.append(
            {
                "question": f"Question {idx}: Describe a challenge from one project and how you solved it.",
                "answer": (
                    "I describe the problem context, alternatives considered, "
                    "why I chose the final approach, and the final impact."
                ),
            }
        )
    return qa[:count]


def _normalize_interview_qa(items, count=8):
    out = []
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, dict):
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            if q and a:
                out.append({"question": q, "answer": a})
        if len(out) >= count:
            break
    return out


def _generate_interview_qa(resume_text, count=8):
    if not USE_LLM:
        return _fallback_interview_qa(resume_text, count=count)

    prompt = f"""You are an interview coach.
Based only on the resume text, generate exactly {count} interview questions with concise model answers.
Return STRICT JSON only in this format:
{{
  "qa": [
    {{"question": "string", "answer": "string"}},
    {{"question": "string", "answer": "string"}}
  ]
}}

Rules:
- Questions must be relevant to projects/skills in the resume.
- Answers must be practical, 2-4 lines each, first-person style.
- No markdown, no extra text outside JSON.

Resume:
{resume_text}
"""

    try:
        content = ""
        if LLM_PROVIDER == "openai":
            if not OPENAI_API_KEY:
                return _fallback_interview_qa(resume_text, count=count)
            from openai import OpenAI  # type: ignore

            client = OpenAI(api_key=OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            content = (response.choices[0].message.content or "").strip()
        else:
            url = f"{OLLAMA_HOST}/api/generate"
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 700, "num_ctx": 2048},
            }
            resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
            content = (resp.json().get("response") or "").strip()

        data = {}
        try:
            data = json.loads(content)
        except Exception:
            m = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if m:
                data = json.loads(m.group(0))
        qa = _normalize_interview_qa(data.get("qa", []), count=count)
        if qa:
            return qa
        return _fallback_interview_qa(resume_text, count=count)
    except Exception:
        return _fallback_interview_qa(resume_text, count=count)


def _trim_for_model(text, max_chars=6500):
    txt = (text or "").strip()
    if len(txt) <= max_chars:
        return txt
    return txt[:max_chars]


def _store_resume_async(user_email, embedding, resume_text):
    def _task():
        try:
            store_resume(user_email, embedding, resume_text)
        except Exception:
            pass

    threading.Thread(target=_task, daemon=True).start()


def _linkedin_login_redirect(job_url: str) -> str:
    target = (job_url or "").strip()
    if not target:
        target = "https://www.linkedin.com/jobs/"
    if "linkedin.com" not in target:
        return target
    return f"https://www.linkedin.com/login?session_redirect={quote(target, safe='')}"


@app.route("/history")
def history():
    if "user" not in session:
        return redirect("/auth")
    uploads = get_uploads_for_user(session["user"])
    back_url = "/upload"
    back_to = request.args.get("back_to")
    upload_id = request.args.get("upload_id", type=int)
    if back_to == "review" and upload_id:
        back_url = f"/upload/{upload_id}"
    return render_template("history.html", uploads=uploads, back_url=back_url)


@app.route("/apply/<int:upload_id>/<int:job_index>")
def apply_job(upload_id, job_index):
    if "user" not in session:
        return redirect("/auth")
    row = get_upload_by_id(upload_id, session["user"])
    if not row:
        return render_template("error.html", message="Upload not found"), 404
    jobs = _safe_json_load(row[6], [])
    if job_index < 0 or job_index >= len(jobs):
        return render_template("error.html", message="Job not found"), 404

    job = jobs[job_index] or {}
    raw_link = job.get("apply_url") or job.get("url") or "https://www.linkedin.com/jobs/"
    return redirect(_linkedin_login_redirect(raw_link))


@app.route("/upload/<int:upload_id>")
def view_upload(upload_id):
    if "user" not in session:
        return redirect("/auth")
    row = get_upload_by_id(upload_id, session["user"])
    if not row:
        return render_template("error.html", message="Upload not found"), 404
    _, filename, summary, resume_text, created_at, insights_json, jobs_json = row
    insights = enrich_insights(resume_text, _safe_json_load(insights_json, {}))
    jobs = _filter_jobs_for_display(_safe_json_load(jobs_json, []))
    market_trends = compute_market_trends(jobs)
    scores = compute_scores(resume_text)
    return render_template(
        "view_upload.html",
        upload_id=upload_id,
        filename=filename,
        summary=summary,
        summary_html=format_summary_html(clean_summary_text(summary)),
        insights=insights,
        jobs=jobs,
        market_trends=market_trends,
        resume_text=resume_text,
        created_at=created_at,
        resume_score=scores["resume_score"],
        ats_score=scores["ats_score"],
        ats_checks=scores["ats_checks"],
        tone_score=scores["tone_score"],
        content_score=scores["content_score"],
        structure_score=scores["structure_score"],
        skills_score=scores["skills_score"],
    )


@app.route("/download/<int:upload_id>")
def download_summary(upload_id):
    if "user" not in session:
        return redirect("/auth")
    row = get_upload_by_id(upload_id, session["user"])
    if not row:
        return render_template("error.html", message="Upload not found"), 404
    _, filename, summary, resume_text, _, insights_json, jobs_json = row
    insights = enrich_insights(resume_text, _safe_json_load(insights_json, {}))
    jobs = _filter_jobs_for_display(_safe_json_load(jobs_json, []))
    base = os.path.splitext(filename)[0] or "resume"

    scores = compute_scores(resume_text)

    def _pdf_safe(text):
        t = str(text or "")
        t = (
            t.replace("\u2019", "'")
            .replace("\u2018", "'")
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\u2022", "-")
            .replace("\u00a0", " ")
            .replace("\u200b", "")
        )
        return t.encode("latin-1", "replace").decode("latin-1")

    # Render PDF with the same result-page layout (browser print), fallback to FPDF if unavailable.
    try:
        from selenium import webdriver  # type: ignore
        from selenium.webdriver.chrome.options import Options  # type: ignore

        style_css = ""
        try:
            style_path = os.path.join(app.root_path, "static", "style.css")
            with open(style_path, "r", encoding="utf-8") as f:
                style_css = f.read()
        except OSError:
            style_css = ""

        html = render_template(
            "result_pdf_exact.html",
            style_css=style_css,
            filename=filename,
            generated_on=datetime.now().strftime("%d/%m/%Y %I:%M %p").lower(),
            summary_html=format_summary_html(clean_summary_text(summary)),
            insights=insights,
            jobs=jobs,
            resume_score=scores["resume_score"],
            ats_score=scores["ats_score"],
            ats_checks=scores["ats_checks"],
            tone_score=scores["tone_score"],
            content_score=scores["content_score"],
            structure_score=scores["structure_score"],
            skills_score=scores["skills_score"],
        )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".html", mode="w", encoding="utf-8") as tmp:
            tmp.write(html)
            tmp_path = tmp.name

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--allow-file-access-from-files")

        driver = webdriver.Chrome(options=options)
        try:
            driver.get(f"file://{tmp_path}")
            pdf_dict = driver.execute_cdp_cmd(
                "Page.printToPDF",
                {
                    "printBackground": True,
                    "paperWidth": 8.27,
                    "paperHeight": 11.69,
                    "marginTop": 0.2,
                    "marginBottom": 0.2,
                    "marginLeft": 0.2,
                    "marginRight": 0.2,
                },
            )
            pdf_bytes = base64.b64decode(pdf_dict["data"])
            output = io.BytesIO(pdf_bytes)
            output.seek(0)
            return send_file(
                output,
                as_attachment=True,
                download_name=f"{base}_summary.pdf",
                mimetype="application/pdf",
            )
        finally:
            driver.quit()
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    except Exception:
        pass

    # Build a fast but structured PDF directly with FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()

    line_width = pdf.w - pdf.l_margin - pdf.r_margin

    def _break_long_words(text, maxlen=34):
        parts = []
        for word in str(text).split(" "):
            if len(word) > maxlen:
                chunks = [word[i:i + maxlen] for i in range(0, len(word), maxlen)]
                parts.append(" ".join(chunks))
            else:
                parts.append(word)
        return " ".join(parts)

    def _section_header(title):
        pdf.set_fill_color(232, 241, 255)
        pdf.set_draw_color(198, 213, 235)
        y = pdf.get_y()
        pdf.rect(pdf.l_margin, y, line_width, 8, style="DF")
        pdf.set_xy(pdf.l_margin + 3, y + 1.5)
        pdf.set_font("Helvetica", style="B", size=11)
        pdf.cell(0, 5, _pdf_safe(title))
        pdf.ln(8)

    def _bullets(items, max_items=6):
        pdf.set_font("Helvetica", size=10)
        data = items[:max_items] if items else []
        if not data:
            pdf.multi_cell(line_width, 5, "- No data")
            return
        for item in data:
            txt = _pdf_safe(_break_long_words(str(item)))
            pdf.multi_cell(line_width, 5, f"- {txt}")

    # Header
    pdf.set_font("Helvetica", style="B", size=16)
    pdf.cell(0, 8, "Resume Review", ln=True)
    pdf.set_font("Helvetica", size=10)
    pdf.set_text_color(90, 102, 120)
    pdf.cell(0, 6, _pdf_safe(f"{filename} | Generated: {datetime.now().strftime('%d/%m/%Y %I:%M %p').lower()}"), ln=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # Score table
    _section_header("Score Overview")
    rows = [
        ("Resume Score", f"{scores['resume_score']}/100"),
        ("ATS Score", f"{scores['ats_score']}/100"),
        ("Tone & Style", f"{scores['tone_score']}/100"),
        ("Content", f"{scores['content_score']}/100"),
        ("Structure", f"{scores['structure_score']}/100"),
        ("Skills", f"{scores['skills_score']}/100"),
    ]
    col1 = line_width * 0.68
    col2 = line_width - col1
    pdf.set_font("Helvetica", size=10)
    pdf.set_fill_color(248, 251, 255)
    pdf.set_draw_color(210, 220, 235)
    for label, value in rows:
        pdf.cell(col1, 7, _pdf_safe(label), border=1, fill=True)
        pdf.cell(col2, 7, _pdf_safe(value), border=1, ln=True, fill=True)
    pdf.ln(3)

    # ATS checks
    _section_header("ATS Checks")
    _bullets([f"{'OK' if c['ok'] else 'Needs work'} - {c['label']}" for c in scores.get("ats_checks", [])], max_items=8)
    pdf.ln(3)

    # Summary
    _section_header("AI Summary")
    for line in _pdf_safe(clean_summary_text(summary)).splitlines():
        if line.strip():
            pdf.multi_cell(line_width, 5, _pdf_safe(_break_long_words(line)))
        else:
            pdf.ln(1)
    pdf.ln(2)

    # Insights
    if insights:
        _section_header("AI Insights")
        insight_map = [
            ("Strengths", insights.get("strengths", [])),
            ("Weaknesses", insights.get("weaknesses", [])),
            ("Gaps", insights.get("gaps", [])),
            ("Recommendations", insights.get("recommendations", [])),
            ("Target Roles", insights.get("target_roles", [])),
        ]
        for label, data in insight_map:
            pdf.set_font("Helvetica", style="B", size=10)
            pdf.cell(0, 6, _pdf_safe(label), ln=True)
            _bullets(data, max_items=5)
            pdf.ln(1)

    # Jobs
    if jobs:
        if pdf.get_y() > (pdf.page_break_trigger - 65):
            pdf.add_page()
        _section_header("Top Job Recommendations")
        pdf.set_font("Helvetica", style="B", size=9)
        w_role, w_company, w_location, w_match = line_width * 0.34, line_width * 0.29, line_width * 0.22, line_width * 0.15
        pdf.set_fill_color(238, 245, 255)
        pdf.cell(w_role, 7, "Role", border=1, fill=True)
        pdf.cell(w_company, 7, "Company", border=1, fill=True)
        pdf.cell(w_location, 7, "Location", border=1, fill=True)
        pdf.cell(w_match, 7, "Match", border=1, ln=True, fill=True)

        pdf.set_font("Helvetica", size=9)
        for job in jobs[:8]:
            title = _pdf_safe(_break_long_words(job.get("title", "-"), maxlen=18))
            company = _pdf_safe(_break_long_words(job.get("company", "-"), maxlen=16))
            location = _pdf_safe(_break_long_words(job.get("location", "-"), maxlen=14))
            match = _pdf_safe(f"{job.get('match_score', 0)}%")
            pdf.cell(w_role, 7, title, border=1)
            pdf.cell(w_company, 7, company, border=1)
            pdf.cell(w_location, 7, location, border=1)
            pdf.cell(w_match, 7, match, border=1, ln=True)

    pdf_bytes = pdf.output(dest="S")
    if isinstance(pdf_bytes, str):
        pdf_bytes = pdf_bytes.encode("latin-1", "replace")
    output = io.BytesIO(pdf_bytes)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{base}_summary.pdf",
        mimetype="application/pdf",
    )


@app.route("/delete/<int:upload_id>", methods=["POST"])
def delete_upload_route(upload_id):
    if "user" not in session:
        return redirect("/auth")
    deleted = delete_upload(upload_id, session["user"])
    if not deleted:
        return render_template("error.html", message="Upload not found"), 404
    return redirect("/history")


if __name__ == "__main__":
    app.run(debug=False, threaded=True, use_reloader=False)
