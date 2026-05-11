from typing import List, Dict
from urllib.parse import quote_plus
from urllib.parse import unquote, urlparse, parse_qs
import re
import time
import html

import requests

from config import (
    ENABLE_LINKEDIN_SCRAPING,
    LINKEDIN_EMAIL,
    LINKEDIN_PASSWORD,
    LINKEDIN_LOCATION,
    LINKEDIN_MAX_JOBS,
    LINKEDIN_INTERACTIVE_LOGIN,
    LINKEDIN_BROWSER,
)


def _clean_job_keywords(keywords: List[str]) -> List[str]:
    noise = {
        "bevanoor", "resume", "college", "student", "engineering", "project",
        "disease", "detection", "elective", "ongoing", "basic", "using",
    }
    out = []
    for k in (keywords or []):
        t = (k or "").strip().lower()
        if not t or t in noise:
            continue
        if len(t) < 3:
            continue
        out.append(t)
    return out[:6]


def _role_queries_from_keywords(cleaned_keywords: List[str]) -> List[str]:
    kw = set(cleaned_keywords or [])
    queries = []

    # Domain-aware role seeds
    if kw & {"civil", "construction", "structural", "autocad", "estimation", "site"}:
        queries.extend(
            [
                "civil engineer",
                "site engineer",
                "structural engineer",
                "construction engineer",
                "quantity surveyor",
            ]
        )

    if kw & {"mechanical", "solidworks", "catia", "manufacturing"}:
        queries.extend(
            [
                "mechanical engineer",
                "design engineer",
                "production engineer",
            ]
        )

    if kw & {"electrical", "power", "substation", "plc"}:
        queries.extend(
            [
                "electrical engineer",
                "power systems engineer",
                "maintenance engineer",
            ]
        )

    if kw & {"python", "java", "javascript", "sql", "react", "backend", "ml", "ai"}:
        queries.extend(
            [
                "python developer",
                "software engineer",
                "data analyst",
                "machine learning engineer",
            ]
        )

    # Always keep broad fallback set.
    queries.extend(
        [
            "graduate engineer trainee",
            "junior engineer",
            "engineer intern",
        ]
    )

    # Deduplicate preserving order
    out = []
    seen = set()
    for q in queries:
        k = q.lower().strip()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(q)
    return out


def _mock_jobs(keywords: List[str], location: str) -> List[Dict]:
    kw = ", ".join(keywords[:4]) if keywords else "software, data, python"
    return [
        {
            "title": "Python Developer Intern",
            "company": "TechNova Labs",
            "location": location,
            "url": "https://www.linkedin.com/jobs/",
            "apply_url": "https://www.linkedin.com/jobs/",
            "description": f"Intern role focused on {kw}, backend APIs, and testing.",
            "source": "mock",
        },
        {
            "title": "AI/ML Associate Engineer",
            "company": "NeuronStack",
            "location": location,
            "url": "https://www.linkedin.com/jobs/",
            "apply_url": "https://www.linkedin.com/jobs/",
            "description": f"Work on model integration, embeddings, Pinecone, and LLM workflows ({kw}).",
            "source": "mock",
        },
    ]


def _safe_text(element):
    try:
        return element.text.strip()
    except Exception:
        return ""


def _extract_job_id_from_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"currentJobId=(\d+)", url)
    if m:
        return m.group(1)
    return ""


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _strip_html(m.group(1))


def _extract_jobs_from_search_html(html_text: str, location: str, max_jobs: int) -> List[Dict]:
    jobs: List[Dict] = []
    seen = set()
    if not html_text:
        return jobs

    # Strategy 1: extract columns independently (robust against nested card markup).
    titles = [
        _strip_html(t) for t in re.findall(
            r'(?is)class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</',
            html_text
        )
    ]
    companies = [
        _strip_html(c) for c in re.findall(
            r'(?is)class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</',
            html_text
        )
    ]
    locations = [
        _strip_html(l) for l in re.findall(
            r'(?is)class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</',
            html_text
        )
    ]
    urls = [
        html.unescape(u) for u in re.findall(
            r'(?is)href="(https?://[^"]*linkedin\.com/jobs/view/[^"]+)"',
            html_text
        )
    ]

    if titles and companies and urls:
        n = min(len(titles), len(companies), len(urls), max_jobs)
        for i in range(n):
            title = titles[i].strip()
            company = companies[i].strip()
            loc = (locations[i].strip() if i < len(locations) else location).strip()
            url = urls[i].strip()
            if not title or not company:
                continue
            key = (title.lower(), company.lower(), loc.lower())
            if key in seen:
                continue
            seen.add(key)
            jobs.append(
                {
                    "title": title,
                    "company": company,
                    "location": loc or location,
                    "url": url,
                    "apply_url": url,
                    "description": "",
                    "source": "linkedin",
                }
            )
        if jobs:
            return jobs[:max_jobs]

    cards = re.findall(
        r'(?is)<div[^>]*class="[^"]*(?:base-card|job-search-card)[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html_text,
    )
    if not cards:
        cards = re.findall(r"(?is)<li[^>]*class=\"[^\"]*base-card[^\"]*\"[^>]*>(.*?)</li>", html_text)

    for card in cards:
        if len(jobs) >= max_jobs:
            break
        title = _extract_first(r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)<', card)
        if not title:
            title = _extract_first(r'class="[^"]*job-search-card__title[^"]*"[^>]*>(.*?)<', card)
        company = _extract_first(r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)<', card)
        if not company:
            company = _extract_first(r'class="[^"]*job-search-card__subtitle[^"]*"[^>]*>(.*?)<', card)
        loc = _extract_first(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)<', card)
        href_match = re.search(r'href="([^"]*linkedin\.com/jobs/view/[^"]+)"', card, flags=re.IGNORECASE)
        url = html.unescape(href_match.group(1)) if href_match else ""
        if not title or not company:
            continue
        key = (title.lower().strip(), company.lower().strip(), (loc or location).lower().strip())
        if key in seen:
            continue
        seen.add(key)
        jobs.append(
            {
                "title": title,
                "company": company,
                "location": loc or location,
                "url": url or "https://www.linkedin.com/jobs/",
                "apply_url": url or "https://www.linkedin.com/jobs/",
                "description": "",
                "source": "linkedin",
            }
        )
    return jobs[:max_jobs]


def _discover_linkedin_job_links(query: str, max_links: int = 12, timeout_s: int = 8) -> List[str]:
    """Fallback discovery when LinkedIn search page blocks bot traffic."""
    urls: List[str] = []
    seen = set()
    try:
        resp = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": f"site:linkedin.com/jobs/view {query}"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            return []
        html_text = resp.text
        raw_links = re.findall(r'href="([^"]+)"', html_text, flags=re.IGNORECASE)
        for raw in raw_links:
            link = html.unescape(raw)
            if "duckduckgo.com/l/?" in link:
                parsed = urlparse(link)
                qs = parse_qs(parsed.query)
                if "uddg" in qs and qs["uddg"]:
                    link = unquote(qs["uddg"][0])
            if "linkedin.com/jobs/view/" not in link:
                continue
            link = link.split("?")[0]
            if link in seen:
                continue
            seen.add(link)
            urls.append(link)
            if len(urls) >= max_links:
                break
    except Exception:
        return []
    return urls


def _job_from_linkedin_job_url(url: str, fallback_location: str, timeout_s: int = 8) -> Dict:
    job_id = _extract_job_id_from_url(url)
    title = ""
    company = ""
    location = fallback_location
    description = ""
    if not job_id:
        return {}

    try:
        detail = requests.get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
            timeout=timeout_s,
        )
        if detail.status_code == 200 and detail.text:
            txt = detail.text
            title = _extract_first(r'class="[^"]*topcard__title[^"]*"[^>]*>(.*?)<', txt)
            if not title:
                title = _extract_first(r'class="[^"]*job-details-jobs-unified-top-card__job-title[^"]*"[^>]*>(.*?)<', txt)

            company = _extract_first(r'class="[^"]*topcard__org-name-link[^"]*"[^>]*>(.*?)<', txt)
            if not company:
                company = _extract_first(r'class="[^"]*topcard__flavor--black-link[^"]*"[^>]*>(.*?)<', txt)
            if not company:
                company = _extract_first(r'class="[^"]*job-details-jobs-unified-top-card__company-name[^"]*"[^>]*>(.*?)<', txt)

            # Structured data fallback
            if not company:
                m_org = re.search(r'"hiringOrganization"\s*:\s*\{[^{}]*"name"\s*:\s*"([^"]+)"', txt, flags=re.IGNORECASE)
                if m_org:
                    company = _strip_html(m_org.group(1))

            # og:title fallback: "<Role> - <Company>"
            if not company:
                m_og = re.search(r'property="og:title"\s+content="([^"]+)"', txt, flags=re.IGNORECASE)
                if m_og:
                    og = html.unescape(m_og.group(1))
                    parts = [p.strip() for p in re.split(r"\s+-\s+", og) if p.strip()]
                    if len(parts) >= 2:
                        if not title:
                            title = parts[0]
                        company = parts[1]

            loc = _extract_first(r'class="[^"]*topcard__flavor--bullet[^"]*"[^>]*>(.*?)<', txt)
            if loc:
                location = loc
            description = _extract_first(r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', txt)
            if not description:
                description = _strip_html(txt)
    except Exception:
        return {}

    if not title:
        return {}
    if not company:
        company = "LinkedIn"
    return {
        "title": title[:160],
        "company": company[:160],
        "location": location or fallback_location,
        "url": url,
        "apply_url": url,
        "description": description[:4000],
        "source": "linkedin",
    }


def _search_page_job_urls(query: str, location: str, max_links: int = 15, timeout_s: int = 8) -> List[str]:
    urls: List[str] = []
    seen = set()
    try:
        resp = requests.get(
            "https://www.linkedin.com/jobs/search/",
            params={"keywords": query, "location": location},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            return []
        matches = re.findall(r"https?://www\.linkedin\.com/jobs/view/\d+/?", resp.text, flags=re.IGNORECASE)
        for u in matches:
            uu = u.split("?")[0]
            if uu in seen:
                continue
            seen.add(uu)
            urls.append(uu)
            if len(urls) >= max_links:
                break
    except Exception:
        return []
    return urls


def _scrape_with_linkedin_guest_api(
    keywords: List[str],
    location: str,
    max_jobs: int,
    timeout_s: int = 10,
) -> List[Dict]:
    search_term = " ".join(keywords[:6]) if keywords else "python developer"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }
    results: List[Dict] = []
    seen = set()
    session = requests.Session()
    session.headers.update(headers)

    for start in (0, 25, 50):
        if len(results) >= max_jobs:
            break
        params = {"keywords": search_term, "location": location, "start": start}
        resp = session.get(
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
            params=params,
            timeout=timeout_s,
        )
        if resp.status_code != 200 or not resp.text.strip():
            continue

        cards = re.findall(r"(?is)<li[^>]*>(.*?)</li>", resp.text)
        if not cards:
            cards = re.findall(r"(?is)<div[^>]*base-card[^>]*>(.*?)</div>\s*</div>", resp.text)

        for card in cards:
            if len(results) >= max_jobs:
                break
            title = _extract_first(r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)<', card)
            company = _extract_first(r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)<', card)
            loc = _extract_first(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)<', card)
            href_match = re.search(r'href="([^"]*linkedin\.com/jobs/view/[^"]+)"', card, flags=re.IGNORECASE)
            url = html.unescape(href_match.group(1)) if href_match else ""
            job_id = _extract_job_id_from_url(url)
            if not title or not company:
                continue
            key = (title.lower().strip(), company.lower().strip(), (loc or location).lower().strip())
            if key in seen:
                continue
            seen.add(key)

            description = ""
            if job_id:
                detail = session.get(
                    f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
                    timeout=timeout_s,
                )
                if detail.status_code == 200 and detail.text:
                    description = _extract_first(
                        r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
                        detail.text,
                    )
                    if not description:
                        description = _strip_html(detail.text)

            results.append(
                {
                    "title": title,
                    "company": company,
                    "location": loc or location,
                    "url": url or "https://www.linkedin.com/jobs/",
                    "apply_url": url or "https://www.linkedin.com/jobs/",
                    "description": description[:4000],
                    "source": "linkedin",
                }
            )

    return results[:max_jobs]


def _login_if_needed(driver, wait, login_email: str | None = None, login_password: str | None = None):
    from selenium.webdriver.common.by import By  # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore

    email_to_use = (login_email or LINKEDIN_EMAIL or "").strip()
    password_to_use = (login_password or LINKEDIN_PASSWORD or "").strip()
    if not email_to_use or not password_to_use:
        return
    driver.get("https://www.linkedin.com/login")
    user = wait.until(EC.presence_of_element_located((By.ID, "username")))
    pwd = wait.until(EC.presence_of_element_located((By.ID, "password")))
    user.clear()
    user.send_keys(email_to_use)
    pwd.clear()
    pwd.send_keys(password_to_use)
    driver.find_element(By.XPATH, "//button[@type='submit']").click()
    wait.until(lambda d: "feed" in d.current_url or "checkpoint" in d.current_url or "linkedin.com" in d.current_url)


def _wait_manual_login(driver, wait):
    from selenium.webdriver.common.by import By  # type: ignore

    # User logs in manually in opened browser (no password collected by this app).
    driver.get("https://www.linkedin.com/login")
    wait.until(
        lambda d: (
            "feed" in d.current_url
            or "linkedin.com/jobs" in d.current_url
            or len(d.find_elements(By.CSS_SELECTOR, ".global-nav")) > 0
        )
    )


def _scrape_with_selenium(
    keywords: List[str],
    location: str,
    max_jobs: int,
    max_runtime_s: int = 10,
    login_email: str | None = None,
    login_password: str | None = None,
) -> List[Dict]:
    from selenium import webdriver  # type: ignore
    from selenium.webdriver.common.by import By  # type: ignore
    from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore
    from selenium.common.exceptions import TimeoutException  # type: ignore
    from selenium.webdriver.chrome.options import Options  # type: ignore
    from selenium.webdriver.common.action_chains import ActionChains  # type: ignore
    from selenium.webdriver.common.keys import Keys  # type: ignore

    search_term = " ".join(keywords[:6]) if keywords else "python developer"
    encoded_kw = quote_plus(search_term)
    encoded_loc = quote_plus(location)
    jobs_url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_kw}&location={encoded_loc}"

    options = Options()
    email_to_use = (login_email or LINKEDIN_EMAIL or "").strip()
    password_to_use = (login_password or LINKEDIN_PASSWORD or "").strip()
    interactive_login = LINKEDIN_INTERACTIVE_LOGIN
    # Keep scraping invisible for user (no open/close windows during analysis).
    # Use visible browser only for explicit interactive-login mode.
    if not interactive_login:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1280")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")

    browser = (LINKEDIN_BROWSER or "chrome").lower()
    driver = None
    if browser == "safari" and interactive_login:
        try:
            driver = webdriver.Safari()
        except Exception:
            # Fallback to Chrome if Safari driver is unavailable.
            driver = webdriver.Chrome(options=options)
    else:
        driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 7)
    results: List[Dict] = []
    start = time.time()

    try:
        driver.set_page_load_timeout(max_runtime_s)
        # Default: no manual login popup. Use env credentials when available.
        if interactive_login:
            wait_long = WebDriverWait(driver, 240)
            _wait_manual_login(driver, wait_long)
        elif email_to_use and password_to_use:
            _login_if_needed(driver, wait, login_email=email_to_use, login_password=password_to_use)

        driver.get(jobs_url)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        # Wait for at least one known jobs list selector (signed-in OR guest page).
        try:
            wait.until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, "ul.scaffold-layout__list-container li")) > 0
                or len(d.find_elements(By.CSS_SELECTOR, "ul.jobs-search__results-list li")) > 0
                or len(d.find_elements(By.CSS_SELECTOR, "ul.jobs-search__results-list li.base-card")) > 0
                or len(d.find_elements(By.CSS_SELECTOR, "li.base-card")) > 0
            )
        except TimeoutException:
            return []

        # Fast path: parse the public jobs cards directly from page HTML.
        html_jobs = _extract_jobs_from_search_html(driver.page_source, location, max_jobs)
        if html_jobs:
            return html_jobs

        # Scroll to load more cards
        body = driver.find_element(By.TAG_NAME, "body")
        for _ in range(2):
            ActionChains(driver).move_to_element(body).send_keys(Keys.END).perform()

        html_jobs = _extract_jobs_from_search_html(driver.page_source, location, max_jobs)
        if html_jobs:
            return html_jobs

        # Prefer signed-in selector, fallback to public selector
        cards = driver.find_elements(By.CSS_SELECTOR, "ul.scaffold-layout__list-container li")
        if not cards:
            cards = driver.find_elements(By.CSS_SELECTOR, "ul.jobs-search__results-list li")
        if not cards:
            cards = driver.find_elements(By.CSS_SELECTOR, "li.base-card")

        # Very broad fallback: infer jobs from all visible LinkedIn job-view links.
        if not cards:
            link_nodes = driver.find_elements(By.XPATH, "//a[contains(@href,'/jobs/view/')]")
            for ln in link_nodes[: max_jobs * 2]:
                try:
                    href = (ln.get_attribute("href") or "").strip()
                    txt = _safe_text(ln)
                    if not href or "/jobs/view/" not in href:
                        continue
                    title = txt or "LinkedIn Job"
                    company = "LinkedIn"
                    parent_text = _safe_text(ln.find_element(By.XPATH, "./ancestor::*[self::li or self::div][1]"))
                    lines = [x.strip() for x in parent_text.split("\n") if x.strip()]
                    if lines:
                        title = lines[0]
                    if len(lines) > 1:
                        company = lines[1]
                    results.append(
                        {
                            "title": title[:120],
                            "company": company[:120],
                            "location": location,
                            "url": href,
                            "apply_url": href,
                            "description": "",
                            "source": "linkedin",
                        }
                    )
                except Exception:
                    continue
            if results:
                uniq = {}
                for item in results:
                    key = (item.get("title", "").lower(), item.get("company", "").lower())
                    if key not in uniq:
                        uniq[key] = item
                return list(uniq.values())[:max_jobs]

        for card in cards[:max_jobs]:
            if time.time() - start > max_runtime_s:
                break
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                card.click()
            except Exception:
                pass

            title = ""
            company = ""
            loc = ""
            url = ""
            apply_url = ""
            description = ""
            job_id = ""

            # Card-level fallback values
            try:
                t = card.find_elements(By.CSS_SELECTOR, "h3")
                if t:
                    title = _safe_text(t[0])
                c = card.find_elements(By.CSS_SELECTOR, "h4")
                if c:
                    company = _safe_text(c[0])
                l = card.find_elements(By.CSS_SELECTOR, ".job-search-card__location")
                if l:
                    loc = _safe_text(l[0])
                a = card.find_elements(By.CSS_SELECTOR, "a")
                if a:
                    url = a[0].get_attribute("href") or ""
                    job_id = _extract_job_id_from_url(url)
            except Exception:
                pass

            # Guest-page fallback selectors
            if not title:
                t2 = card.find_elements(By.CSS_SELECTOR, ".base-search-card__title")
                if t2:
                    title = _safe_text(t2[0])
            if not company:
                c2 = card.find_elements(By.CSS_SELECTOR, ".base-search-card__subtitle")
                if c2:
                    company = _safe_text(c2[0])
            if not loc:
                l2 = card.find_elements(By.CSS_SELECTOR, ".job-search-card__location")
                if l2:
                    loc = _safe_text(l2[0])
            if not url:
                a2 = card.find_elements(By.CSS_SELECTOR, "a.base-card__full-link")
                if a2:
                    url = a2[0].get_attribute("href") or ""
                    job_id = _extract_job_id_from_url(url) or job_id

            # Right-panel details
            detail_text_selectors = [
                ".jobs-description-content__text",
                ".jobs-box__html-content",
                ".show-more-less-html__markup",
            ]
            for sel in detail_text_selectors:
                try:
                    el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    txt = _safe_text(el)
                    if txt and len(txt) > len(description):
                        description = txt
                except Exception:
                    continue

            # Signed-in detail title/company/location
            try:
                dt = driver.find_elements(By.CSS_SELECTOR, ".job-details-jobs-unified-top-card__job-title")
                if dt and _safe_text(dt[0]):
                    title = _safe_text(dt[0])
                dc = driver.find_elements(By.CSS_SELECTOR, ".job-details-jobs-unified-top-card__company-name")
                if dc and _safe_text(dc[0]):
                    company = _safe_text(dc[0])
                dl = driver.find_elements(By.CSS_SELECTOR, ".job-details-jobs-unified-top-card__bullet")
                if dl and _safe_text(dl[0]):
                    loc = _safe_text(dl[0])
                du = driver.find_elements(By.CSS_SELECTOR, ".job-details-jobs-unified-top-card__job-title a")
                if du:
                    url = du[0].get_attribute("href") or url
                    job_id = _extract_job_id_from_url(url) or job_id
            except Exception:
                pass

            # Try to capture direct apply button URL from detail pane.
            apply_selectors = [
                "a.jobs-apply-button",
                "a[data-live-test-job-apply-button]",
                "a[aria-label*='Apply']",
            ]
            for sel in apply_selectors:
                try:
                    ael = driver.find_elements(By.CSS_SELECTOR, sel)
                    if ael:
                        maybe = ael[0].get_attribute("href") or ""
                        if maybe and "linkedin.com" in maybe:
                            # This is still valid, but if direct external link exists we prefer that.
                            apply_url = maybe
                        elif maybe:
                            apply_url = maybe
                            break
                except Exception:
                    continue

            # Normalize to a stable view URL when job_id is available.
            if job_id:
                url = f"https://www.linkedin.com/jobs/view/{job_id}/"
            if not apply_url and url:
                apply_url = url

            if title and company:
                results.append(
                    {
                        "title": title,
                        "company": company,
                        "location": loc or location,
                        "url": url or jobs_url,
                        "apply_url": apply_url or (url or jobs_url),
                        "description": description[:4000],
                        "source": "linkedin",
                    }
                )

        # Deduplicate by (title, company)
        uniq = {}
        for item in results:
            key = (item.get("title", "").lower(), item.get("company", "").lower())
            if key not in uniq:
                uniq[key] = item
        return list(uniq.values())[:max_jobs]
    finally:
        driver.quit()


def fetch_linkedin_jobs(
    keywords: List[str],
    location: str | None = None,
    max_jobs: int | None = None,
    request_timeout_s: int = 30,
    email: str | None = None,
    password: str | None = None,
) -> List[Dict]:
    location = (location or LINKEDIN_LOCATION or "India").strip()
    max_jobs = max_jobs or LINKEDIN_MAX_JOBS

    if not ENABLE_LINKEDIN_SCRAPING:
        return []

    cleaned_keywords = _clean_job_keywords(keywords)
    role_queries = _role_queries_from_keywords(cleaned_keywords)

    # Keep "known good" generic queries first (restores previous stable behavior),
    # then add resume/domain-specific terms.
    keyword_sets = [
        ["python", "developer"],
        ["software", "engineer"],
        ["data", "analyst"],
    ]
    if cleaned_keywords:
        keyword_sets.append(cleaned_keywords)
    for q in role_queries[:4]:
        parts = q.split()
        if parts not in keyword_sets:
            keyword_sets.append(parts)

    # Avoid duplicate location attempts like "india" and "India".
    location_sets = []
    for loc in [location, "India"]:
        l = (loc or "").strip()
        if not l:
            continue
        if l.lower() not in {x.lower() for x in location_sets}:
            location_sets.append(l)

    all_jobs: List[Dict] = []
    seen = set()
    started_at = time.time()
    deadline = started_at + max(8, request_timeout_s)

    for kw_set in keyword_sets:
        if len(all_jobs) >= max_jobs:
            break
        if time.time() >= deadline:
            break
        for loc in location_sets:
            if len(all_jobs) >= max_jobs:
                break
            if time.time() >= deadline:
                break
            jobs: List[Dict] = []
            remaining = max(1, int(deadline - time.time()))
            if remaining <= 1:
                break
            # First attempt: LinkedIn guest jobs endpoint (fast, no browser startup).
            try:
                jobs = _scrape_with_linkedin_guest_api(
                    kw_set,
                    loc,
                    max_jobs,
                    timeout_s=max(3, min(8, remaining)),
                )
                if jobs:
                    print(f"[linkedin] guest-api jobs={len(jobs)} kw={kw_set} loc={loc}")
                else:
                    print(f"[linkedin] guest-api no jobs kw={kw_set} loc={loc}")
            except Exception:
                jobs = []

            # Fallback: Selenium scrape.
            if not jobs and time.time() < deadline:
                try:
                    remaining = max(1, int(deadline - time.time()))
                    per_try_runtime = max(10, min(16, remaining))
                    if LINKEDIN_INTERACTIVE_LOGIN:
                        per_try_runtime = max(18, min(remaining, 30))
                    jobs = _scrape_with_selenium(
                        kw_set,
                        loc,
                        max_jobs,
                        max_runtime_s=per_try_runtime,
                        login_email=email,
                        login_password=password,
                    )
                    if jobs:
                        print(f"[linkedin] selenium jobs={len(jobs)} kw={kw_set} loc={loc}")
                        # Return immediately once we have real jobs to keep response fast.
                        for job in jobs:
                            key = (
                                (job.get("title") or "").strip().lower(),
                                (job.get("company") or "").strip().lower(),
                                (job.get("location") or "").strip().lower(),
                            )
                            if not key[0] or not key[1]:
                                continue
                            if key in seen:
                                continue
                            seen.add(key)
                            all_jobs.append(job)
                            if len(all_jobs) >= max_jobs:
                                break
                        if all_jobs:
                            return all_jobs[:max_jobs]
                    else:
                        print(f"[linkedin] selenium no jobs kw={kw_set} loc={loc}")
                except Exception:
                    print(f"[linkedin] selenium error kw={kw_set} loc={loc}")
                    continue

            for job in jobs:
                key = (
                    (job.get("title") or "").strip().lower(),
                    (job.get("company") or "").strip().lower(),
                    (job.get("location") or "").strip().lower(),
                )
                if not key[0] or not key[1]:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                all_jobs.append(job)
                if len(all_jobs) >= max_jobs:
                    break

    if all_jobs:
        return all_jobs[:max_jobs]

    # Final fallback: discover real LinkedIn job URLs via search index and hydrate details.
    discovered: List[Dict] = []
    for role in role_queries:
        links = _search_page_job_urls(role, location, max_links=max_jobs, timeout_s=6)
        if links:
            print(f"[linkedin] search-page links={len(links)} role={role} loc={location}")
        for link in links:
            job = _job_from_linkedin_job_url(link, location, timeout_s=6)
            if not job:
                continue
            key = (
                (job.get("title") or "").strip().lower(),
                (job.get("company") or "").strip().lower(),
                (job.get("location") or "").strip().lower(),
            )
            if not key[0] or not key[1]:
                continue
            if key in seen:
                continue
            seen.add(key)
            discovered.append(job)
            if len(discovered) >= max_jobs:
                return discovered[:max_jobs]

        links = _discover_linkedin_job_links(f"{role} {location}", max_links=max_jobs, timeout_s=6)
        if links:
            print(f"[linkedin] indexed links={len(links)} role={role} loc={location}")
        for link in links:
            job = _job_from_linkedin_job_url(link, location, timeout_s=6)
            if not job:
                continue
            key = (
                (job.get("title") or "").strip().lower(),
                (job.get("company") or "").strip().lower(),
                (job.get("location") or "").strip().lower(),
            )
            if not key[0] or not key[1]:
                continue
            if key in seen:
                continue
            seen.add(key)
            discovered.append(job)
            if len(discovered) >= max_jobs:
                return discovered[:max_jobs]
    return discovered[:max_jobs]
