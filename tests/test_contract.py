import json
import unittest
from pathlib import Path

from src.culture_finance_progress import validate_event

DATA_DIR = Path(__file__).parents[1] / "data"

class ContractTest(unittest.TestCase):
    def test_sample_matches_domain_contract(self):
        record = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(record), [])

    def test_sample_chain_matches_domain_contract(self):
        records = json.loads((DATA_DIR / "sample_chain.json").read_text(encoding="utf-8"))
        self.assertTrue(records)
        for record in records:
            self.assertEqual(validate_event(record), [], record.get("event_id"))

if __name__ == "__main__":
    unittest.main()
