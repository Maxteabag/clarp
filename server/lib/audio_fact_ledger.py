"""Candidate deterministic audio fact ledger. Caller identity must come from authenticated Host context.
Separate SQLite store, no provider calls; integration into live ingestion remains unshipped.
"""
import json, sqlite3
from pathlib import Path

def canon(value): return json.dumps(value,sort_keys=True,separators=(',',':'))

class AudioFactLedger:
 def __init__(self, path):
  self.path = Path(path)
  with self.conn() as c:
   c.executescript("CREATE TABLE IF NOT EXISTS authority(k TEXT PRIMARY KEY,owner TEXT,generation INT,revision INT,cancelled INT,reader TEXT);CREATE TABLE IF NOT EXISTS effects(k TEXT PRIMARY KEY,payload TEXT);CREATE TABLE IF NOT EXISTS producer(k TEXT PRIMARY KEY,event TEXT UNIQUE);CREATE TABLE IF NOT EXISTS envelopes(id TEXT PRIMARY KEY,event TEXT);")
 def conn(self):return sqlite3.connect(self.path,timeout=10)
 def scope(self,e):return canon([e[k] for k in ['host','conversation','message','clip']])
 def effect(self,e):return canon([e[k] for k in ['host','conversation','message','revision','clip','stage','occurrence']])
 def seed(self,e, *, owner, generation, revision, reader):
  with self.conn() as c:c.execute('INSERT OR IGNORE INTO authority VALUES(?,?,?,?,?,?)',(self.scope(e),owner,generation,revision,0,reader))
 def authority(self,e,**fields):
  assert set(fields)<= {'owner','generation','revision','cancelled','reader'}
  with self.conn() as c:
   for k,v in fields.items():c.execute(f'UPDATE authority SET {k}=? WHERE k=?',(v,self.scope(e)))
 def produce(self,e):
  # Source-owned durable outbox identity; reconstruction after restart returns same ID.
  key=self.effect(e)
  with self.conn() as c:
   c.execute('INSERT OR IGNORE INTO producer VALUES(?,?)',(key,'producer:'+key));return c.execute('SELECT event FROM producer WHERE k=?',(key,)).fetchone()[0]
 def count(self):
  with self.conn() as c:return c.execute('SELECT count(*) FROM effects').fetchone()[0]
 def apply(self,e,envelope,caller='worker',fault=None):
  with self.conn() as c:
   c.execute('BEGIN IMMEDIATE');state=c.execute('SELECT owner,generation,revision,cancelled,reader FROM authority WHERE k=?',(self.scope(e),)).fetchone()
   if not state or caller!=state[4]:return 'denied'
   key=self.effect(e);producer=c.execute('SELECT event FROM producer WHERE k=?',(key,)).fetchone()
   if not producer or e.get('producerEvent')!=producer[0]:return 'unknown_producer_event'
   oldenv=c.execute('SELECT event FROM envelopes WHERE id=?',(envelope,)).fetchone()
   if oldenv and oldenv[0]!=producer[0]:return 'envelope_conflict'
   if e['stage'] in ['authored','synthesized'] and e['occurrence']!='once':return 'invalid_occurrence'
   if e['stage']=='delivered' and not e['occurrence'].startswith('playback:'):return 'invalid_occurrence'
   payload=canon({'fact':e['fact']});old=c.execute('SELECT payload FROM effects WHERE k=?',(key,)).fetchone()
   if old:result='replay' if old[0]==payload else 'conflict'
   elif (e['owner'],e['generation'],e['revision'])!=state[:3]:result='stale_authority'
   elif state[3]:result='cancelled'
   elif e['fact'] is None:result='needs_judgment'
   else:c.execute('INSERT INTO effects VALUES(?,?)',(key,payload));result='accepted'
   if result in ['accepted','replay']:c.execute('INSERT OR IGNORE INTO envelopes VALUES(?,?)',(envelope,producer[0]))
   if fault=='before':c.rollback();return 'rollback_before_commit'
   c.commit();return 'lost_ack_after_commit' if fault=='after' else result
