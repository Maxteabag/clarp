"""Build the self-contained stage-three proposal from its editable source."""
from pathlib import Path
root=Path(__file__).resolve().parent
head='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#253b4a"><title>Stage three — Follow the work</title><style>'
(root/'index.html').write_text(head+(root/'style.css').read_text()+'</style></head><body>'+(root/'body.html').read_text()+'</body></html>')
