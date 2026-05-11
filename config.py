import os

from dotenv import load_dotenv

load_dotenv()

# ==============================
# Flask / App Configuration
# ==============================

# Secret key for Flask sessions
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-env")


# ==============================
# File Upload Configuration
# ==============================

# Folder where uploaded resumes are stored
UPLOAD_FOLDER = "uploads/resumes"

# Allowed resume formats
ALLOWED_EXTENSIONS = {"pdf"}


# ==============================
# Database Configuration
# ==============================

# SQLite database path
SQLITE_DB = "database/users.db"


# ==============================
# Vector Database (Pinecone)
# ==============================

# Enable Pinecone storage for Week 1–2
USE_PINECONE = True

# Only required if USE_PINECONE = True
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
INDEX_NAME = "resume-index"
PINECONE_CLOUD = "aws"
PINECONE_REGION = "us-east-1"


# ==============================
# LLM / OpenAI Configuration
# ==============================

# Enable LLM-based resume analysis
USE_LLM = True

# Read OpenAI API key from environment variable
# (DO NOT hardcode the key here)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# LLM provider to use: "ollama" or "openai"
LLM_PROVIDER = "ollama"

# OpenAI settings (only if LLM_PROVIDER = "openai")
LLM_MODEL = "gpt-3.5-turbo"

# Ollama settings (only if LLM_PROVIDER = "ollama")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "90"))
OLLAMA_RETRIES = int(os.getenv("OLLAMA_RETRIES", "1"))
MAX_RESUME_CHARS_FOR_LLM = int(os.getenv("MAX_RESUME_CHARS_FOR_LLM", "4200"))
ALLOW_LLM_FALLBACK = os.getenv("ALLOW_LLM_FALLBACK", "false").lower() == "true"

# Gemini web-style answer settings (optional)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "12"))


# ==============================
# LinkedIn Scraping / Jobs
# ==============================

ENABLE_LINKEDIN_SCRAPING = os.getenv("ENABLE_LINKEDIN_SCRAPING", "true").lower() == "true"
LINKEDIN_EMAIL = os.getenv("LINKEDIN_EMAIL")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD")
LINKEDIN_LOCATION = os.getenv("LINKEDIN_LOCATION", "India")
LINKEDIN_MAX_JOBS = int(os.getenv("LINKEDIN_MAX_JOBS", "20"))
REQUIRE_REAL_LINKEDIN = os.getenv("REQUIRE_REAL_LINKEDIN", "true").lower() == "true"
USE_MOCK_JOBS = os.getenv("USE_MOCK_JOBS", "false").lower() == "true"
LINKEDIN_INTERACTIVE_LOGIN = os.getenv("LINKEDIN_INTERACTIVE_LOGIN", "false").lower() == "true"
LINKEDIN_BROWSER = os.getenv("LINKEDIN_BROWSER", "chrome").lower()


# ==============================
# Resume Scoring (UI Defaults)
# ==============================

# Default scores shown in UI
DEFAULT_RESUME_SCORE = 88
DEFAULT_ATS_SCORE = 88

# Weights for future scoring logic
WEIGHT_TONE_STYLE = 0.25
WEIGHT_CONTENT = 0.25
WEIGHT_STRUCTURE = 0.25
WEIGHT_SKILLS = 0.25
