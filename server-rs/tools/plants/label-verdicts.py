#!/usr/bin/env python3
"""One replacement per observable label-verdict or quiet-document rule."""
import runner
FILE="clarp-kernel/src/janitors/label_audit.rs"
changes=[
    ("index + 1","index + 2"), ("None => Some(0.0)","None => Some(1.0)"),
    ('probability(answer, "accurate")','probability(answer, "finished")'),
    ("probability(answer, choice)",'probability(answer, "accurate")'),
    ("accurate <= 0.30","accurate < 0.30"),("accurate <= 0.30","accurate <= 0.31"),
    ("confidence >= 0.50","confidence > 0.50"),("confidence >= 0.50","confidence >= 0.49"),
    ("_ => None,",'_ => Some("unknown"),'),("m < 90","m <= 90"),("m < 90","m < 89"),
    ("round_ties_even()","round()"),("{accurate:.3}","{accurate:.2}"),
    ("triples.sort();","triples.reverse();"),("python::dumps_ascii","python::compact_ascii"),
    ("count == 0 || fingerprint == previous","count == 0"),
    ("count == 1 {","count == 2 {"),("count - 1","count + 1"),("hash[..40]","hash[..39]"),
]
# Replacing each wire field or reference phrase must change the emitted artifact.
for phrase in ["the work it describes looks finished","it looks like it is waiting for your answer",
               "nothing seems to be happening","it seems to be doing something else","a while","{m} min",
               "no change for {age}","1 working label looks wrong","working labels look wrong"," And {} more.",
               'fields.remove("line")','label-audit-{}','label-audit:{run}','"type":"document"',
               '"status":"completed"','"attention_kind":"label_mismatch"','nothing was changed.']:
    if phrase.startswith('fields.'): replacement='fields.remove("match")'
    elif phrase.startswith('"'): replacement=phrase.replace('":"','":"other_')
    else: replacement=phrase+'?'
    changes.append((phrase,replacement))
RUNS={"vectors":["test","--test","label_verdicts"]}
if __name__=="__main__":
    raise SystemExit(runner.main(__file__,RUNS,[(FILE,"vectors",f"change {a} to {b}",a,b) for a,b in changes]))
