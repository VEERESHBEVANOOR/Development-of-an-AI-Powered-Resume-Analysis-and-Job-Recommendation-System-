import unittest
from unittest.mock import patch

from utils.job_recommender import extract_keywords_from_resume, rank_jobs_for_resume


class TestJobRecommender(unittest.TestCase):
    def test_keyword_extract(self):
        resume = "Python Flask SQL machine learning analytics dashboard"
        keywords = extract_keywords_from_resume(resume, max_keywords=5)
        self.assertTrue(len(keywords) > 0)

    @patch("utils.job_recommender.get_embedding")
    def test_rank_jobs(self, mock_embedding):
        def fake_embed(text):
            text_l = text.lower()
            if "python" in text_l or "flask" in text_l or "sql" in text_l:
                return [1.0, 0.0, 0.0]
            return [0.0, 1.0, 0.0]

        mock_embedding.side_effect = fake_embed
        resume = "Python Flask SQL machine learning analytics"
        jobs = [
            {
                "title": "Python Developer",
                "company": "A",
                "location": "Remote",
                "description": "Flask SQL API development",
            },
            {
                "title": "Graphic Designer",
                "company": "B",
                "location": "Remote",
                "description": "Photoshop Illustrator design",
            },
        ]
        ranked = rank_jobs_for_resume(resume, jobs, ["python", "flask", "sql"], top_k=2)
        self.assertEqual(len(ranked), 2)
        self.assertGreaterEqual(ranked[0]["match_score"], ranked[1]["match_score"])


if __name__ == "__main__":
    unittest.main()
