# AI Resume Analyzer System (Week 1-8 Complete)

This project implements the full lifecycle described in the project PDF:
- Resume upload and user management
- LLM-powered resume summary + insights
- Embedding + Pinecone storage
- LinkedIn job scraping (Selenium) with recommendation ranking
- Testing and documentation

## Tech Stack
- Frontend: HTML, CSS (Jinja templates)
- Backend: Flask (Python)
- LLM: Ollama (`llama3.1:8b`) or OpenAI (configurable)
- Vector DB: Pinecone
- Embeddings: `sentence-transformers/all-MiniLM-L6-v2`
- DB: SQLite (`database/users.db`)
- Scraping: Selenium (LinkedIn jobs)


## Project Structure

```
ai_resume_system/
  app.py
  config.py
  requirements.txt
  database/users.db
  static/style.css
  templates/
  uploads/resumes/
  utils/
    auth.py
    resume_parser.py
    embedding.py
    llm_analyzer.py
    pinecone_db.py
    linkedin_scraper.py
    job_recommender.py
    scoring.py
    uploads.py
  tests/
```

## Setup

1. Create and activate venv:
   - `python3 -m venv venv`
   - `source venv/bin/activate`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Create `.env` file:

```
SECRET_KEY=replace-with-random-secret

# LLM
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OPENAI_API_KEY=

# Pinecone
PINECONE_API_KEY=your_pinecone_key

# LinkedIn scraping (optional)
ENABLE_LINKEDIN_SCRAPING=false
LINKEDIN_EMAIL=
LINKEDIN_PASSWORD=
LINKEDIN_LOCATION=India
LINKEDIN_MAX_JOBS=20
```

4. If using Ollama:
   - `ollama serve`
   - `ollama pull llama3.1:8b`
5. Run app:
   - `python app.py`

## Notes
- If LinkedIn scraping is OFF, the system still works using fallback jobs.
- If OpenAI quota is exhausted, switch to Ollama by setting `LLM_PROVIDER=ollama`.
- Downloaded PDF includes score, ATS, summary, insights, and top job recommendations.
