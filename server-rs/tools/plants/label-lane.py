#!/usr/bin/env python3
"""What a check opens, reports, keeps, and shows after restart."""
import runner
H="clarp-host/src/label_checker.rs"
K="clarp-kernel/src/janitors/label_audit.rs"
RUNS={"host":["test","--test","host","--","working_labels_are_reported_in_the_checkers_own_chat"],
      "vectors":["test","--test","label_lane"]}
plants=[]
def plant(file,run,old,new):
    plants.append((file,run,"change "+old+" to "+new,old,new))
for old,new in [('found > 0 && !filed','found > 0 && filed'),('Checked {checked} working labels;','Checked {checked} labels;'),(' (already reported)',' (reported)')]:
    plant(K,"vectors",old,new)
for old,new in [
    ('if entries.is_empty() {','if entries.len()<2{'),
    ('row(LAST, &now)','row(LAST, &0)'), # anchor disambiguated below
    ('if !site_enabled(app) {\n        return Ok(now + 15_000);\n    }','if site_enabled(app){return Ok(now+15_000);}'),
    ('row(MARK, &mark)','row(MARK, &String::new())'),
    ('"a{}={}"','"a{}:{}"'),('response["usage"][field]','response["tokens"][field]'),
    ('found.is_none());','found.is_some());'),('row["sequence"] = json!(next);','row["sequence"] = json!(next+1);'),
    ('row["sequence"] = json!(next);','row["sequence"] = json!(next+2);'),
    ('(next % CAPACITY)','((next+1)%CAPACITY)'),
    ('lane.record(bounds, (adaptive::IDLE, "Nothing shown as working"), now)','lane.record(bounds, (adaptive::WORKED, "Nothing shown as working"), now)')]:
    if old=='row(LAST, &now)':old='keep_together(vec![row(LAST, &now)';new='keep_together(vec![row(LAST, &"missing")'
    if 'sequence' in old and '+2' in new:continue
    plant(H,"host",old,new)
plant("clarp-host/src/janitors.rs","host","    crate::label_checker::look(app);","    let _ = app;")
for a,b in [
    ("CAPACITY: u64 = 100","CAPACITY: u64 = 99"),("CAPACITY: u64 = 100","CAPACITY: u64 = 101"),
    ("next.wrapping_add(1)","next.wrapping_add(2)"),
    ("billing refusal","ordinary refusal"),('row["error"] = json!("invalid judgment response")','row["error"] = json!("malformed")'),
    ('if let Some(d) = &doc','if let Some(d) = &doc'),
]:
    if a==b or "invalid judgment response" in a:continue
    plant(H,"host",a,b)
if __name__=="__main__":raise SystemExit(runner.main(__file__,RUNS,plants))
