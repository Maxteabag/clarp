"""Render all-level paired evidence without choosing the prettiest repetition."""
import argparse
import json
from pathlib import Path
import statistics

def report(path):
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    samples=[r for r in rows if 'answers' in r]
    lines=['# Current vs examples + short labels: all detail levels','',
        'Same Spark low model and eight synthetic activities; three repetitions per condition. '
        'The candidate uses audience-specific examples, not the old Plain-English-only prompt at every level. '
        'These are experimental outputs; nothing has been deployed.','',
        'Developer was verified through the shipping request path: disabled, zero model calls. '
        'Both versions retain the original raw activity.','',
        '## Timing','', '| Level | Current median | Examples median | Valid batches |', '|---|---:|---:|---:|']
    for level,name in enumerate(['Developer','Technical','Balanced','Plain English','Grandma']):
        if level==0:
            lines.append('| Developer | No inference | No inference | Bypass passed |'); continue
        groups=[[r for r in samples if r['detail_level']==level and r['prompt']==variant] for variant in ['baseline','examples']]
        values=[f'{statistics.median(r["total_ms"] for r in group)/1000:.2f} s' for group in groups]
        lines.append(f'| {name} | {values[0]} | {values[1]} | {sum(r["valid"] for group in groups for r in group)}/6 |')
    lines += ['', 'Small samples measure batch completion, not per-item latency or a reliable P95.', '', '## Side-by-side actual outputs', '',
        'Repetition zero is shown consistently below; all repetitions follow in the appendix. No hand-polishing.']
    keys=list(samples[0]['answers'])
    for index,case in enumerate(keys):
        raw=rows[0]['raw'][index]['activity']
        lines += ['', f'### {case.replace("_"," ")}', '', '| Level | Current | Examples + short labels |', '|---|---|---|']
        original=raw.get('command',raw.get('summary','')).replace('|','\\|')
        lines.append(f'| Developer | `{original}` | Same raw activity |')
        for level,name in enumerate(['Developer','Technical','Balanced','Plain English','Grandma']):
            if not level: continue
            pair=[next(r for r in samples if r['detail_level']==level and r['prompt']==variant and r['repetition']==0)['answers'][case] for variant in ['baseline','examples']]
            lines.append('| '+name+' | '+' | '.join(str(t).replace('|','\\|') for t in pair)+' |')
    lines += ['', '## Appendix: every repetition','']
    for row in samples:
        lines += [f'### {row["audience"]} / {row["prompt"]} / repetition {row["repetition"]}', '']
        lines += [f'- **{case}:** {text}' for case,text in row['answers'].items()]
        lines += ['']
    return '\n'.join(lines)+'\n'

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('input',type=Path); p.add_argument('output',type=Path)
    args=p.parse_args()
    with args.output.open('x') as output: output.write(report(args.input))
