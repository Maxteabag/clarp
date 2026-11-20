#!/usr/bin/env python3
"""Check the public umbrella feature name and its original contract number."""
import runner

RUNS = {"wire": ["test", "--test", "host", "the_host_lists_transcription_with_its_reference_contract"]}
SITE = "clarp-host/src/edge.rs"
PLANTS = [
    (SITE, "wire", "the umbrella name is missing", '("transcription", 1),', '("transcription-missing", 1),'),
    (SITE, "wire", "the umbrella has the wrong contract", '("transcription", 1),', '("transcription", 2),'),
]

if __name__ == "__main__":
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
