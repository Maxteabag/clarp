"""Wrong routes, display choices and model selection for an agent's portraits."""
import runner

RUNS = {
    'settings': ['test','--test','portrait_generation'],
    'host': ['test','--test','host','agent_portraits::'],
}
K = 'clarp-kernel/src/portraits.rs'
P = 'clarp-host/src/personas.rs'
H = 'clarp-host/src/edge.rs'
F = 'clarp-host/src/faces.rs'
PLANTS = [
    (K,'settings','keep surrounding spaces','s.trim().to_ascii_lowercase()', 's.to_ascii_lowercase()'),
    (K,'settings','case-sensitive stored value','s.trim().to_ascii_lowercase().as_str()', 's.trim()'),
    (K,'settings','ignore numeric true','"true" | "1"', '"true" | "2"'),
    (K,'settings','ignore yes','"yes" | "on"', '"off" | "on"'),
    (K,'settings','ignore on','"yes" | "on"', '"yes" | "no"'),
    (K,'settings','ignore true','"true" | "1"', '"false" | "1"'),
    (P,'host','wrong validation receipt','"model_avatars required"','"model required"'),
    (P,'host','save into another preference','portraits::MODEL_SETTING.to_owned()', '"other".to_owned()'),
    (H,'host','read a different snapshot preference','directory.setting(clarp_kernel::portraits::MODEL_SETTING)','directory.setting("other")'),
    (H,'host','use another backend default setting','format!("{}_model", view.backend)','format!("{}_effort", view.backend)'),
    (F,'host','omit configured model family','model_files(persona, backend, model, default_model)','model_files(persona, backend, model, "")'),
    (F,'host','janitor loses the generic portrait','"janitor-default.jpg".to_string()', '"absent.jpg".to_string()'),
    (F,'host','always show fallback janitor art','vec![file.to_string(), "janitor-default.jpg".to_string()]','vec!["janitor-default.jpg".to_owned()]'),
]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
