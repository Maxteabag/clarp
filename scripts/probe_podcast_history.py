#!/usr/bin/env python3
"""Create an isolated podcast-history wire fixture and measure storage commits.

No live Host, provider, media gallery or user feedback is touched.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=200)
    args=parser.parse_args()
    if not 1<=args.samples<=1000: parser.error('--samples must be 1..1000')
    args.output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='podcast-history-probe-') as temporary:
        root=Path(temporary)
        os.environ.update(CLAUDE_PWA_DB=str(root/'state.sqlite'),CLARP_CONFIG_DIR=str(root/'config'),
                          CLAUDE_PWA_CONFIG=str(root/'config.toml'),CLARP_SHARE_DIR=str(root/'share'))
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
        from lib import agents,artifacts,db,media_store,podcast_history as history,podcast_live
        db.reset_for_tests(root/'state.sqlite')
        agents.create_agent(persona='Fixture',voice_id='fixture',cwd=str(root),session='fixture')
        audio=media_store.publish(session='fixture',blob=b'ID3\x04\x00\x00fixture',source_name='fixture.mp3',
            content_type='audio/mpeg',media_dir=root/'media')
        episode={'revision':media_store.get(audio['asset_id'])['sha256'],
            'transcript':[{'start':0,'end':100,'text':'We can compare a smaller experiment before choosing a larger one.'}],
            'chapters':[{'start':0,'end':100,'title':'Choosing the next experiment','source':'The pilot is a proposal; no resources have been started.'}],
            'corrections':'A simulation is not a real-device result.'}
        artifact=artifacts.create(session='fixture',type='audio',title='Planning a multiplayer experiment',
            artifact_id='podcast-fixture',payload={'url':audio['url'],'mime_type':'audio/mpeg','file_name':'fixture.mp3',
            'duration_ms':100000,'podcast':episode})
        source=artifacts.create(session='fixture',type='document',title='Multiplayer research plan',
            artifact_id='source-plan-fixture',payload={'content':'Begin with a bounded pilot and keep the original measurements.'})
        context=podcast_live.context_for(episode,42,100,source=source)
        ident=history.create(artifact=artifact,position=42,context=context,source=source,model='gpt-live-1',voice='marin')
        for kind,text in [('input','Could we make this first experiment smaller?'),
            ('output','Yes. Keep the first comparison focused on network delivery and real browser clients. The larger runtime comparison can follow the measurements.'),
            ('input','That is the scope I would like the plan to consider.')]:
            history.record(ident,{'type':'session.'+kind+'_transcript.delta','delta':text})
        history.finish(ident)
        fixture=history.get(ident)
        (args.output/'conversation.json').write_text(json.dumps(fixture,indent=2)+'\n')
        second=history.create(artifact=artifact,position=42,context=context,source=source,model='gpt-live-1',voice='marin')
        samples=[]
        for _ in range(args.samples):
            started=time.perf_counter()
            history.record(second,{'type':'session.input_transcript.delta','delta':'One recognized phrase. '})
            samples.append((time.perf_counter()-started)*1000)
        history.finish(second)
        result={'scope':'Isolated local SQLite WAL writes, no competing writer or provider; not production voice latency.',
            'samples':len(samples),'p50_ms':statistics.median(samples),
            'p95_ms':sorted(samples)[int(len(samples)*.95)],'max_ms':max(samples),'raw_ms':samples,
            'schema':db.conn().execute('PRAGMA user_version').fetchone()[0]}
        (args.output/'storage-timing.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k!='raw_ms'}))

if __name__=='__main__':main()
