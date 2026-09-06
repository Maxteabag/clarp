"""Fresh current/refined live matrix, fixed model, four cases, all audiences."""
import argparse
import json
from pathlib import Path
import statistics
from levels import candidate, GRANDMA_REFINEMENT, NAMES
from run import PROMPTS, requests, shipping, trial

CASES = [
    {'case':'Find overdue invoices','activity':{'name':'Bash','command':'python overdue_invoices.py','scripts':[{'file':'overdue_invoices.py','source_excerpt':"from datetime import date\nimport csv\nwith open('invoices.csv') as f:\n for row in csv.DictReader(f):\n  if row['paid']=='false' and date.fromisoformat(row['due_date']) < date.today():\n   print(row['invoice_number'], row['customer'], row['amount'])",'excerpt_only':True}]}},
    {'case':'Inspect invoice checker','activity':{'name':'Bash','command':"sed -n '1,80p' overdue_invoices.py",'scripts':[{'file':'overdue_invoices.py','source_excerpt':"from datetime import date\nimport csv\nwith open('invoices.csv') as f:\n for row in csv.DictReader(f):\n  if row['paid']=='false' and date.fromisoformat(row['due_date']) < date.today():\n   print(row['invoice_number'], row['customer'], row['amount'])",'excerpt_only':True}]}},
    {'case':'Change waiting limit','activity':{'name':'Edit','file_path':'network/client.ts','summary':'Change request timeout from 45000 ms to 15000 ms.'}},
    {'case':'Unknown task','activity':{'name':'Bash','command':'bash task_zeta.sh'}},
]
REFINEMENTS = {
1: 'Keep useful executable names, paths, flags and numeric conditions. Explain the effect, not just the invocation. Prefer one concise sentence.',
2: 'Lead with the useful action and target. Keep only essential technical context; omit runtime names and literal field names when ordinary wording is precise.',
3: 'Use everyday words. Translate timeout into waiting for a reply, and executing code into its evidenced task. Avoid filenames, languages, API names and internal fields. Preserve numbers and restrictions.',
4: GRANDMA_REFINEMENT,
}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true'); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--render',type=Path,help='Read existing evidence and render report, no model calls')
    args=p.parse_args()
    if args.render:
        with args.output.open('x') as output: output.write(render(args.render))
        return
    if not args.live:
        print('24 model calls: 2 variants x 4 translated levels x 3 repetitions; Developer bypass only.')
        return
    with args.output.open('x') as output:
        with shipping.ToolExplanations(translate=lambda *_: (_ for _ in ()).throw(AssertionError('No inference for Developer'))) as service:
            assert all(i['status']=='disabled' for i in service.request(0,requests(CASES))['items'])
        output.write(json.dumps({'detail_level':0,'model_calls':0,'bypass_verified':True,'cases':CASES})+'\n')
        for repetition in range(3):
            for level in [1,2,3,4]:
                for variant in (['baseline','refined'] if (level+repetition)%2 else ['refined','baseline']):
                    PROMPTS['refined']=candidate(level)+REFINEMENTS[level]+'\n'
                    row=trial(variant,CASES,repetition,detail_level=level)
                    row.update(audience=NAMES[level],instructions=PROMPTS[variant]+shipping.POLICIES[level],experiment='fresh-refined-all')
                    output.write(json.dumps(row,ensure_ascii=False)+'\n'); output.flush()
                    print(json.dumps({k:row[k] for k in ['audience','prompt','repetition','valid','total_ms']}),flush=True)

def render(path):
    rows=[json.loads(s) for s in path.read_text().splitlines()]
    samples=rows[1:]
    lines=['# Fresh live experiment: CURRENT vs REFINED at every level','',
        'Spark low; four new synthetic activities; three repetitions per condition. '
        '24 fresh calls and 96 labels. No production deployment. Developer bypass verified without model calls. '
        'Current is the unchanged shipping prompt plus audience policy; refined adds level-specific examples and wording rules.','',
        'Tables show repetition zero consistently, with all repetitions in the appendix.','',
        '## Batch completion timing','', '| Level | Current median | Refined median |','|---|---:|---:|',
        '| Developer | No model | No model |']
    for level in range(1,5):
        values=[statistics.median(r['total_ms'] for r in samples if r['detail_level']==level and r['prompt']==v)/1000 for v in ['baseline','refined']]
        lines.append(f'| {NAMES[level]} | {values[0]:.2f} s | {values[1]:.2f} s |')
    for case in CASES:
        lines+=['', '## '+case['case'],'','| Level | CURRENT | REFINED |','|---|---|---|']
        raw=case['activity'].get('command',case['activity'].get('summary',''))
        lines.append(f'| Developer | `{raw}` | Same raw activity |')
        for level in range(1,5):
            pair=[next(r for r in samples if r['detail_level']==level and r['prompt']==v and r['repetition']==0).get('answers',{}).get(case['case'],'[trial failed]') for v in ['baseline','refined']]
            lines.append('| '+NAMES[level]+' | '+' | '.join(str(s).replace('|','\\|') for s in pair)+' |')
    lines+=['','## All repetitions','']
    for r in samples:
        lines += [f'### {r["audience"]}, {r["prompt"]}, repetition {r["repetition"]}', '']
        lines += [f'- **{k}:** {v}' for k,v in r.get('answers',{}).items()]
        lines += ['']
    return '\n'.join(lines)+'\n'

if __name__=='__main__': main()
