import unittest

from utils.scoring import compute_scores


class TestScoring(unittest.TestCase):
    def test_scores_in_range(self):
        text = """
        John Doe
        Education: BE Computer Science
        Experience: Built Flask APIs and React UI.
        Skills: Python, SQL, Docker, Git
        Projects: ATS resume analyzer with Pinecone and LLM.
        """
        scores = compute_scores(text)
        for key in [
            "resume_score",
            "ats_score",
            "tone_score",
            "content_score",
            "structure_score",
            "skills_score",
        ]:
            self.assertGreaterEqual(scores[key], 0)
            self.assertLessEqual(scores[key], 100)


if __name__ == "__main__":
    unittest.main()
