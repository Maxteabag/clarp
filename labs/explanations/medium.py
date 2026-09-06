"""Three contemporaneous columns, all levels: current low/refined low/refined medium."""
import argparse
import json
from pathlib import Path
import statistics
from refined_all import CASES, REFINEMENTS
from levels import candidate, NAMES
from run import PROMPTS, shipping, requests, trial

COLUMNS=[('baseline','low'),('refined','low'),('refined','medium')]
LABELS=['Current · low','Refined · low','Refined · medium']

def render(path):
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    samples=rows[1:]
    lines=['# Spark: current low vs refined low vs refined medium','',
        '36 fresh calls; three repetitions per condition; four identical synthetic activities. '
        'Same model gpt-5.3-codex-spark. Refined low and medium use identical instructions. '
        'Developer bypass verified separately: no model calls. No production changes.', '',
        '| Level | Current low | Refined low | Refined medium |','|---|---:|---:|---:|']
    for level in range(1,5):
        groups=[[r for r in samples if r['detail_level']==level and (r['prompt'],r['effort'])==column] for column in COLUMNS]
        lines.append('| '+NAMES[level]+' | '+' | '.join(f'{statistics.median(r["total_ms"] for r in g)/1000:.2f} s' for g in groups)+' |')
    lines += ['', 'Times are median batch completion, not per-item latency. Small pilot, not a P95/SLA.', '',
        f'Format/160-character checks passed: {sum(r["valid"] for r in samples)}/{len(samples)} batches. All attempts retained.', '',
        'Primary tables consistently show repetition zero, not the best-looking answer.']
    for case in CASES:
        lines+=['','## '+case['case'],'','| Level | Current low | Refined low | Refined medium |','|---|---|---|---|']
        raw=case['activity'].get('command',case['activity'].get('summary',''))
        lines.append(f'| Developer | `{raw}` | Same raw activity | Same raw activity |')
        for level in range(1,5):
            pair=[next(r for r in samples if r['detail_level']==level and (r['prompt'],r['effort'])==v and r['repetition']==0).get('answers',{}).get(case['case'],'[trial failed]') for v in COLUMNS]
            lines.append('| '+NAMES[level]+' | '+' | '.join(str(s).replace('|','\\|') for s in pair)+' |')
    lines+=['','## Every repetition','']
    for row in samples:
        lines += [f'### {row["audience"]} / {row["prompt"]} / {row["effort"]} / repetition {row["repetition"]}', '']
        lines += [f'- **{k}:** {v}' for k,v in row.get('answers',{}).items()]
        lines += ['']
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true'); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--render',type=Path)
    p.add_argument('--resume',action='store_true',help='Append only missing conditions; preserve completed and failed samples')
    args=p.parse_args()
    if args.render:
        with args.output.open('x') as output: output.write(render(args.render))
        return
    if not args.live:
        print('36 calls: 3 columns x 4 translated levels x 3 repetitions; Developer bypass only.'); return
    completed=set()
    if args.resume:
        existing=[json.loads(line) for line in args.output.read_text().splitlines()]
        assert existing[0]['bypass_verified'] and existing[0]['cases']==CASES
        completed={(r['detail_level'],r['prompt'],r['effort'],r['repetition']) for r in existing[1:]}
        assert len(completed)==len(existing)-1
    with args.output.open('a' if args.resume else 'x') as output:
        with shipping.ToolExplanations(translate=lambda *_: (_ for _ in ()).throw(AssertionError('Developer inference'))) as service:
            assert all(i['status']=='disabled' for i in service.request(0,requests(CASES))['items'])
        if not args.resume:
            output.write(json.dumps({'detail_level':0,'bypass_verified':True,'model_calls':0,'cases':CASES})+'\n')
        for repetition in range(3):
            for level in range(1,5):
                offset=(repetition+level)%3
                for prompt,effort in COLUMNS[offset:]+COLUMNS[:offset]:
                    if (level,prompt,effort,repetition) in completed:
                        continue
                    PROMPTS['refined']=candidate(level)+REFINEMENTS[level]+'\n'
                    row=trial(prompt,CASES,repetition,detail_level=level,effort=effort)
                    row.update(audience=NAMES[level],instructions=PROMPTS[prompt]+shipping.POLICIES[level],experiment='spark-three-columns')
                    output.write(json.dumps(row,ensure_ascii=False)+'\n'); output.flush()
                    print(json.dumps({k:row[k] for k in ['audience','prompt','effort','repetition','valid','total_ms']}),flush=True)

if __name__=='__main__': main()
