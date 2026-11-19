"""Wrong rules for the two directory routes and the shared launch ledger."""
import runner
K='clarp-kernel/src/launch_directories.rs'
H='clarp-host/src/launch_directories.rs'
F='clarp-host/src/fleet.rs'
D='clarp-kernel/src/devices.rs'
RUNS={'vectors':['test','--test','launch_directories'],
      'scope':['test','--test','device_scope_vectors'],
      'host':['test','--test','host','new_agent_directory_suggestions_and_frequent_paths_survive_restart']}
PLANTS=[]
def plant(file,run,why,old,new):
    PLANTS.append((file,run,why,old,new))
for why,a,b in [
    ('keep surrounding blanks','let path = python::strip(path);','let path = path;'),
    ('count empty launches','if path.is_empty()','if false'),
    ('start count at zero','old.map_or(1,','old.map_or(0,'),
    ('freeze usage count','u.use_count + 1','u.use_count'),
    ('replace first-use time','|u| u.first_used_at','|_| now'),
    ('use a wrong last-use clock','last_used_at: now,','last_used_at: now+1,'),
    ('zero means one','.filter(|v| *v != 0)','.filter(|_| true)'),
    ('wrong default favorites limit','.unwrap_or(5)','.unwrap_or(4)'),
    ('reject the smallest favorites limit','.clamp(1, 20)','.clamp(2, 20)'),
    ('return no favorites for a negative limit','.clamp(1, 20)','.clamp(0, 20)'),
    ('omit the twentieth favorite','.clamp(1, 20)','.clamp(1, 19)'),
    ('allow twenty-one favorites','.clamp(1, 20)','.clamp(1, 21)'),
    ('rank least frequent first','Reverse(u.use_count)','u.use_count'),
    ('rank older favorites first','Reverse(u.use_count), Reverse(u.last_used_at)','Reverse(u.use_count), u.last_used_at'),
    ('reverse favorite tie-break','Reverse(u.use_count), Reverse(u.last_used_at), &u.path','Reverse(u.use_count), Reverse(u.last_used_at), Reverse(&u.path)'),
    ('ignore favorite limit','rows.truncate(n);','rows.truncate(n+1);'),
    ('change favorite response field','"paths":rows','"favorites":rows'),
    ('drop shared cwd','!janitor.contains(&u.path) || regular.contains(&u.path)','!janitor.contains(&u.path)'),
    ('include janitor-only cwd','!janitor.contains(&u.path) || regular.contains(&u.path)','true'),
    ('rank oldest recent first','(Reverse(u.last_used_at), &u.path)','(u.last_used_at, &u.path)'),
    ('reverse recent ties','(Reverse(u.last_used_at), &u.path)','(Reverse(u.last_used_at), Reverse(&u.path))'),
    ('only 29 recent paths','rows.into_iter().take(30)','rows.into_iter().take(29)'),
    ('take 31 recent paths','rows.into_iter().take(30)','rows.into_iter().take(31)'),
    ('leave query blanks','python::first(python::strip(raw), 512)','python::first(raw, 512)'),
    ('truncate one character early','python::strip(raw), 512','python::strip(raw), 511'),
    ('truncate one character late','python::strip(raw), 512','python::strip(raw), 513'),
    ('skip folding the source','let folded = python::casefold(path);','let folded = path.to_string();'),
    ('keep spaces in fuzzy query',".filter(|c| *c != ' ')",".filter(|_| true)"),
    ('forget earlier matched characters','remaining.any(|p| p == c)','folded.contains(c)'),
    ('miss last fallback input','if fallback { 1000 }','if fallback { 999 }'),
    ('read another fallback line','if fallback { 1000 }','if fallback { 1001 }'),
    ('drop last ranked result','.take(100)','.take(99)'),
    ('filter a successful tool answer','!fallback || fuzzy(p, query)','fuzzy(p, query)'),
    ('return nonmatching fallback rows','!fallback || fuzzy(p, query)','true'),
    ('do not sort directory entries','children.sort();','children.reverse();'),
    ('use case-sensitive prefix','python::casefold(p).starts_with(&prefix)','p.starts_with(&prefix)'),
    ('drop thirtieth explicit child','.take(30)\n        .collect()','.take(29)\n        .collect()'),
    ('offer a thirty-first explicit child','.take(30)\n        .collect()','.take(31)\n        .collect()'),
    ('forget literal tilde concatenation','format!("{home}{rest}")','format!("{home}/{rest}")'),
    ('always put tool before explicit for paths',"q.starts_with(['~', '.']) || q.contains('/')","false"),
    ('put explicit first for plain words',"q.starts_with(['~', '.']) || q.contains('/')","true"),
    ('offer all recent paths for typed text','filter(|p| fuzzy(p, q))','filter(|_| true)'),
    ('duplicate path spelling','seen.contains(&path) || !is_dir(&path)','!is_dir(&path)'),
    ('offer missing directories','seen.contains(&path) || !is_dir(&path)','seen.contains(&path)'),
    ('omit home for empty query','candidates.push(home.into());','candidates.extend(Vec::<String>::new());'),
    ('shorten labels for sibling homes','path.starts_with(&format!("{home}/"))','path.starts_with(home)'),
    ('leave the home label expanded','path == home || path.starts_with','path.starts_with'),
    ('only fifteen matches','matches.len() == 16','matches.len() == 15'),
    ('seventeen matches','matches.len() == 16','matches.len() == 17'),
    ('misname the matches field','"matches":matches','"rows":matches'),
    ('misname result label','"label":label','"name":label')]:
    plant(K,'vectors',why,a,b)
for why,a,b in [
    ('let a limited device enumerate directories','|| path == "/launch-directories"','|| path == "/not-a-route"'),
    ('close descendants too','path == "/launch-directories"','path.starts_with("/launch-directories")'),
    ('close limited favorite reads','path == "/launch-directories"','matches!(path, "/launch-directories" | "/favorite-paths")')]:
    plant(D,'scope',why,a,b)
for why,a,b in [
    ('keep zoxide alive past the timeout','.kill_on_drop(true)','.kill_on_drop(false)'),
    ('wait too long for zoxide','Duration::from_millis(500)','Duration::from_secs(3)'),
    ('leak Host credentials','for name in crate::OWN_SECRETS {','for name in crate::OWN_SECRETS.iter().take(0) {'),
    ('inherit caller session identity','cmd.env_remove(name);\n        }','let _ = name;\n        }'),
    ('lose the option terminator','["query", "--list", "--"]','["query", "--list", "-"]'),
    ('lose fallback argv','zoxide(&["query", "--list"])','zoxide(&["query", "--"])'),
    ('ignore nonzero fallback stdout','|(_, stdout)| rules::ranked(&stdout, query, true)','|(ok, stdout)| if ok {rules::ranked(&stdout,query,true)}else{vec![]}'),
    ('skip explicit matches','let explicit = explicit(&q, &home);','let explicit = Vec::new();'),
    ('treat a blank first parameter as supplied','k == key && !v.is_empty()','k == key'),
    ('allow POST favorites','get(favorites).post(crate::edge::no_route)','get(favorites).post(favorites)')]:
    plant(H,'host',why,a,b)
for why,old,new in [
    ('skip relaunch accounting','self.put_launch(&record)?;\n        self.replaced','self.put(&record)?;\n        self.replaced'),
    ('skip new agent accounting','self.put_launch(&record)?;\n        self.members.push','self.put(&record)?;\n        self.members.push'),
    ('count built-in installation instead of an explicit Janitor','if by_request {\n            self.put_launch','if !by_request {\n            self.put_launch'),
    ('forget usage on restart','launch_usage.insert(key, usage);','let _ = (key,usage);'),
    ('forget memory after writing','Arc::make_mut(&mut self.launch_usage).insert(usage.path.clone(), usage);','let _ = usage;'),
    ('save no durable frequency row','(shelf(LAUNCH_USAGE), usage.path.clone(), Some(bytes))','(shelf(LAUNCH_USAGE), usage.path.clone(), None)'),
    ('key the durable count by agent id','(shelf(LAUNCH_USAGE), usage.path.clone(), Some(bytes))','(shelf(LAUNCH_USAGE), record.agent_id.clone(), Some(bytes))'),
]:
    plant(F,'host',why,old,new)
for why,old,new in [
    ('double slash root collapsed','value.starts_with("//") && !value.starts_with("///")','false'),
    ('three slashes retained as double','value.starts_with("//") && !value.starts_with("///")','value.starts_with("//")'),
    ('lose normalized empty path','".".into()','"".into()'),
    ('erase parent traversals','*part != "."','*part != "." && *part != ".."'),
    ('leave query case untouched','python::casefold(query)','query.to_owned()'),
    ('lose non-ASCII decimal digits','char::from_digit(d,10)','char::from_digit(d,2)'),
    ('ignore the Python integer input limit','count()>4300','count()>9999'),
]:
    plant(K,'vectors',why,old,new)
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
