#!/usr/bin/env python3
"""Compare the offline level analyser with the reference's synthetic PCM vectors."""
import io
import json
from pathlib import Path
import runpy
import unittest
import wave

ROOT=Path(__file__).resolve().parents[1]
ANALYSER=runpy.run_path(str(ROOT/"tools/voice-metrics.py"))
VECTORS=ROOT/"crates/clarp-kernel/tests/fixtures/voice-timeline.json"


class Levels(unittest.TestCase):
    def test_reference_integrity_thresholds(self):
        cases=json.loads(VECTORS.read_text())["flags"]
        self.assertTrue(cases)
        for c in cases:
            for key,transcript in [("empty",""),("present","fixture")]:
                self.assertEqual(ANALYSER["corruption_reasons"](c["metric"], transcript=transcript), c[key])

    def test_reference_capture_metrics_and_reasons(self):
        rows=json.loads(VECTORS.read_text())["metrics"]
        self.assertGreater(len(rows),0)
        for row in rows:
            wav=io.BytesIO()
            with wave.open(wav,"wb") as w:
                w.setnchannels(row["channels"])
                w.setsampwidth(row["width"])
                w.setframerate(row["rate"])
                w.writeframes(b"".join(n.to_bytes(row["width"],"little",signed=row["width"]!=1) for n in row["samples"]))
            metrics=ANALYSER["analyze"](wav.getvalue(),"audio/wav")
            if ANALYSER["np"] is None:
                self.assertEqual(metrics,{"bytes":len(wav.getvalue()),"content_type":"audio/wav","unavailable":"numpy"})
                self.assertEqual(ANALYSER["corruption_reasons"](metrics,transcript=""),[])
            else:
                self.assertEqual(metrics,row["measured"])
                for key,transcript in [("empty",""),("present","fixture")]:
                    self.assertEqual(ANALYSER["corruption_reasons"](metrics,transcript=transcript),row[key])


if __name__=="__main__":
    unittest.main()
