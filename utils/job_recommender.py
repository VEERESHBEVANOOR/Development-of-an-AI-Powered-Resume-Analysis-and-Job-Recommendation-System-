import math
import re
from typing import List, Dict

from utils.embedding import get_embedding


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    nb = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _keyword_overlap(resume_text: str, job_text: str, keywords: List[str]) -> float:
    if not keywords:
        return 0.0
    resume_lower = resume_text.lower()
    job_lower = job_text.lower()
    useful = [k.strip().lower() for k in keywords if k and k.strip()]
    if not useful:
        return 0.0
    matched = 0
    for kw in useful:
        if kw in resume_lower and kw in job_lower:
            matched += 1
    return matched / len(useful)


def _job_blob(job: Dict) -> str:
    parts = [
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("description", ""),
    ]
    return " ".join(p for p in parts if p).strip()


def _extract_trending_terms(jobs: List[Dict], top_n: int = 8) -> List[str]:
    freq = {}
    for job in jobs:
        text = _job_blob(job).lower()
        tokens = re.findall(r"[a-z][a-z0-9+#.]{2,24}", text)
        for t in tokens:
            if t in {"and", "the", "with", "for", "job", "role", "team", "years", "experience"}:
                continue
            freq[t] = freq.get(t, 0) + 1
    ordered = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in ordered[:top_n]]


def rank_jobs_for_resume(
    resume_text: str,
    jobs: List[Dict],
    keywords: List[str],
    strengths: List[str] | None = None,
    top_k: int = 8,
) -> List[Dict]:
    if not resume_text.strip() or not jobs:
        return []

    resume_embedding = get_embedding(resume_text[:7000])
    trends = _extract_trending_terms(jobs, top_n=10)
    strengths = [s.lower() for s in (strengths or [])]
    ranked = []

    for job in jobs:
        blob = _job_blob(job)
        if not blob:
            continue
        blob_l = blob.lower()
        job_embedding = get_embedding(blob[:3500])
        semantic = _cosine(resume_embedding, job_embedding)
        overlap = _keyword_overlap(resume_text, blob, keywords)
        trend_overlap = sum(1 for t in trends[:8] if t in blob_l and t in resume_text.lower()) / max(1, min(8, len(trends)))
        strength_bonus = 0.0
        if strengths:
            strength_bonus = sum(1 for s in strengths if s and s in blob_l) / len(strengths)
        score = (0.65 * semantic) + (0.20 * overlap) + (0.10 * trend_overlap) + (0.05 * strength_bonus)
        ranked.append(
            {
                **job,
                "match_score": round(max(0.0, min(1.0, score)) * 100, 1),
                "trend_score": round(trend_overlap * 100, 1),
            }
        )

    ranked.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    return ranked[:top_k]


def extract_keywords_from_resume(resume_text: str, max_keywords: int = 12) -> List[str]:
    candidates = re.findall(r"[A-Za-z][A-Za-z+.#]{1,20}", resume_text)
    stop = {
        "and", "the", "with", "for", "from", "that", "this", "your", "work",
        "resume", "project", "projects", "education", "experience", "name",
        "using", "basic", "college", "engineering", "student", "management",
        "system", "dr", "vp", "ongoing", "implemented", "developed",
    }
    freq = {}
    for token in candidates:
        t = token.lower()
        if t in stop or len(t) < 3:
            continue
        freq[t] = freq.get(t, 0) + 1
    ordered = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [token for token, _ in ordered[:max_keywords]]


def compute_market_trends(jobs: List[Dict]) -> List[str]:
    return _extract_trending_terms(jobs, top_n=10)
