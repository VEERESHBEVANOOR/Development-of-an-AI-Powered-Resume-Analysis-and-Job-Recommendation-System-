import unittest

from utils.insight_fallback import enrich_insights


class TestInsightFallback(unittest.TestCase):
    def test_ensures_weaknesses_and_recommendations(self):
        resume = "Name\nSkills: Python, Flask\nProjects: Built app."
        out = enrich_insights(resume, {})
        self.assertTrue(len(out["weaknesses"]) > 0)
        self.assertTrue(len(out["recommendations"]) > 0)


if __name__ == "__main__":
    unittest.main()
