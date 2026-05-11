import re

from config import (
    WEIGHT_TONE_STYLE,
    WEIGHT_CONTENT,
    WEIGHT_STRUCTURE,
    WEIGHT_SKILLS,
)


SECTION_KEYWORDS = {
    "contact": ["@", "phone", "linkedin", "github"],
    "summary": ["summary", "objective", "profile"],
    "education": ["education", "academic", "university", "college"],
    "experience": ["experience", "employment", "work history", "internship"],
    "skills": ["skills", "technical skills", "technologies"],
    "projects": ["projects", "project"],
}

COMMON_SKILLS = [
    "python", "java", "c++", "sql", "javascript", "html", "css",
    "flask", "django", "react", "node", "aws", "docker",
    "git", "linux", "machine learning", "data science",
]


def _has_any(text_lower, keywords):
    return any(k in text_lower for k in keywords)


def _count_skills(text_lower):
    return sum(1 for k in COMMON_SKILLS if k in text_lower)


ACTION_VERBS = {
    "built", "developed", "implemented", "designed", "led", "created", "optimized",
    "improved", "deployed", "automated", "analyzed", "integrated", "managed",
}


def compute_scores(resume_text, jobs=None, insights=None):
    text = resume_text or ""
    text_lower = text.lower()
    words = re.findall(r"[a-zA-Z0-9]+", text_lower)
    word_count = len(words)

    # Structure: section coverage + readable length
    section_hits = sum(
        1 for _, kws in SECTION_KEYWORDS.items() if _has_any(text_lower, kws)
    )
    section_score = min(100, (section_hits / len(SECTION_KEYWORDS)) * 100)
    if 280 <= word_count <= 1000:
        length_score = 100
    elif 180 <= word_count <= 1400:
        length_score = 80
    else:
        length_score = 60
    structure_score = (section_score * 0.65) + (length_score * 0.35)

    # Skills: based on resume text only (deterministic)
    skill_hits = _count_skills(text_lower)
    skills_score = min(100, 35 + skill_hits * 6)

    # Content: measurable outcomes and action verbs
    has_numbers = bool(re.search(r"\d", text))
    action_verb_hits = sum(1 for v in ACTION_VERBS if v in text_lower)
    bullet_count = len(re.findall(r"^\s*[-*•]", text, flags=re.MULTILINE))
    content_score = 45
    content_score += 20 if has_numbers else 0
    content_score += min(20, action_verb_hits * 3)
    content_score += 15 if bullet_count >= 4 else 5
    content_score = min(100, content_score)

    # Tone/Style: readability and signal quality
    punctuation = re.findall(r"[^\w\s]", text)
    punct_ratio = (len(punctuation) / max(len(text), 1))
    avg_sentence_len = 0
    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    if sentences:
        avg_sentence_len = word_count / len(sentences)

    if word_count < 150:
        tone_score = 55
    elif punct_ratio > 0.28:
        tone_score = 62
    elif 10 <= avg_sentence_len <= 24:
        tone_score = 90
    else:
        tone_score = 78

    # ATS checks (dynamic, analysis-based)
    has_contact = ("@" in text) or bool(re.search(r"\b\d{10}\b", text))
    has_sections = section_hits >= 4
    keyword_rich = skill_hits >= 5
    readable_length = 180 <= word_count <= 1400
    quantified = has_numbers

    ats_checks = [
        {"label": "Contact information detected", "ok": has_contact},
        {"label": "Core resume sections detected", "ok": has_sections},
        {"label": "Relevant technical keywords present", "ok": keyword_rich},
        {"label": "Readable length for ATS parsing", "ok": readable_length},
        {"label": "Quantified achievements detected", "ok": quantified},
    ]

    resume_score = (
        tone_score * WEIGHT_TONE_STYLE
        + content_score * WEIGHT_CONTENT
        + structure_score * WEIGHT_STRUCTURE
        + skills_score * WEIGHT_SKILLS
    )

    # ATS score focuses more on structure + skills
    ats_score = (
        structure_score * 0.45
        + skills_score * 0.35
        + content_score * 0.20
    )

    return {
        "resume_score": round(resume_score),
        "ats_score": round(ats_score),
        "tone_score": round(tone_score),
        "content_score": round(content_score),
        "structure_score": round(structure_score),
        "skills_score": round(skills_score),
        "ats_checks": ats_checks,
    }
