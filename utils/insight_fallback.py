import re


def enrich_insights(resume_text, insights):
    text = resume_text or ""
    text_l = text.lower()
    insights = insights if isinstance(insights, dict) else {}

    out = {
        "strengths": list(insights.get("strengths", []) or []),
        "weaknesses": list(insights.get("weaknesses", []) or []),
        "gaps": list(insights.get("gaps", []) or []),
        "recommendations": list(insights.get("recommendations", []) or []),
        "target_roles": list(insights.get("target_roles", []) or []),
    }

    has_numbers = bool(re.search(r"\d", text))
    has_projects = "project" in text_l
    has_experience = "experience" in text_l or "intern" in text_l
    has_summary = "summary" in text_l or "objective" in text_l or "profile" in text_l
    has_skills = "skills" in text_l
    has_contact = ("@" in text) and ("linkedin" in text_l or "github" in text_l)

    if not out["strengths"]:
        if has_skills:
            out["strengths"].append("Technical skills section present with relevant technologies.")
        if has_projects:
            out["strengths"].append("Project work demonstrates practical implementation exposure.")
        if has_experience:
            out["strengths"].append("Experience section helps recruiters assess role readiness.")
        if not out["strengths"]:
            out["strengths"].append("Resume provides a base profile suitable for further improvement.")

    if not out["weaknesses"]:
        if not has_summary:
            out["weaknesses"].append("Missing professional summary/objective at the top.")
        if not has_numbers:
            out["weaknesses"].append("Achievements are not quantified with measurable impact.")
        if not has_contact:
            out["weaknesses"].append("Contact links (LinkedIn/GitHub) are incomplete or missing.")
        if not has_projects:
            out["weaknesses"].append("Project section needs stronger detail and outcomes.")
        if not out["weaknesses"]:
            out["weaknesses"].append("Bullet points can be sharper with action-result format.")

    if not out["gaps"]:
        if not has_experience:
            out["gaps"].append("Limited industry/internship evidence for target roles.")
        if not has_skills:
            out["gaps"].append("Dedicated skills section not clearly visible.")
        if not has_numbers:
            out["gaps"].append("Impact metrics (%, time saved, scale) are missing.")
        if not out["gaps"]:
            out["gaps"].append("Resume can be better aligned to specific job descriptions.")

    if not out["recommendations"]:
        out["recommendations"].append("Add a 2-3 line profile summary tailored to target role.")
        out["recommendations"].append("Rewrite bullets with action verb + quantified result.")
        out["recommendations"].append("Prioritize top 8-12 skills relevant to target jobs.")
        out["recommendations"].append("Add LinkedIn and GitHub links in the header.")

    if not out["target_roles"]:
        if "machine learning" in text_l or "data science" in text_l:
            out["target_roles"].append("ML Engineer")
            out["target_roles"].append("Data Analyst")
        elif "flask" in text_l or "django" in text_l or "api" in text_l:
            out["target_roles"].append("Backend Developer")
            out["target_roles"].append("Python Developer")
        elif "react" in text_l or "javascript" in text_l:
            out["target_roles"].append("Full Stack Developer")
        else:
            out["target_roles"].append("Software Engineer")

    for k in out:
        out[k] = [str(x).strip() for x in out[k] if str(x).strip()][:6]
    return out
