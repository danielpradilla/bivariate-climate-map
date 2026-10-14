import contextlib
from datetime import date
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import download_assets


class DownloadPeriodTests(unittest.TestCase):
    def test_invalid_period_rejected_before_network_access(self):
        for start, end in ((2025, 2024), (1949, 2025), (2025, 2027)):
            with self.subTest(start=start, end=end), patch.object(download_assets, "date") as clock:
                clock.today.return_value = date(2026, 10, 14)
                with patch.object(sys, "argv", ["download_assets.py", "--start-year", str(start), "--end-year", str(end)]), patch.object(download_assets, "fetch_document") as fetch, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        download_assets.main()
                    self.assertEqual(error.exception.code, 2)
                    fetch.assert_not_called()

    def test_unpublished_year_does_not_fall_back_or_create_output(self):
        registry = {"regions": [{"id": "world", "metadataFile": "regions/world/metadata.json"}]}
        manifest = {"years": [2024, 2025]}
        for flags, missing in (([], "[2026]"), (["--start-year", "2025", "--end-year", "2026"], "[2026]"), (["--start-year", "2023", "--end-year", "2024"], "[2023]")):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temporary, patch.object(download_assets, "date") as clock:
                clock.today.return_value = date(2026, 10, 14)
                output = Path(temporary) / "data"
                with patch.object(sys, "argv", ["download_assets.py", "--output-dir", str(output), *flags]), patch.object(download_assets, "fetch_document", side_effect=[(b"", registry), (b"", manifest)]):
                    with self.assertRaises(ValueError) as error:
                        download_assets.main()
                    self.assertIn(missing, str(error.exception))
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
