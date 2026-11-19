//! A label that outlived the work behind it.
//!
//! An agent says in a few words what it is busy with beyond its turns (work it left
//! running), and the apps draw it as working while it says so. A label nobody took back
//! would draw it so for ever. So an idle agent's own label older than two hours is taken
//! back, and what it rested in settles to idle; the apps are told to read the rows again
//! (`agent-roster`, `stale-work`). The rule is the Python Host's (`custom_status_due`,
//! [`clarp_kernel::helpers::status_due`], held by its own answers); here it is looked at
//! now and then, as the helpers are. A label set again since it was looked at stands: the
//! agent takes back only the one that was set at the time it was looked at.

use std::time::Duration;

use clarp_kernel::helpers;

use crate::edge::App;

/// Looks now and then for labels that outlived their time.
pub fn run(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        let every = Duration::from_millis(app.config.helpers_checked_every_ms.max(50));
        loop {
            tokio::time::sleep(every).await;
            retire_due(&app).await;
        }
    });
}

/// Takes back every label that is due, and tells the apps of each.
pub async fn retire_due(app: &App) {
    let now = app.clock.now_ms();
    let directory = app.fleet.directory();
    for member in &directory.members {
        if member.record.is_janitor {
            continue;
        }
        let view = member.agent.view();
        let (label, set_at) = (&view.status.label, view.status.label_at_ms);
        let doing = crate::fleet::doing(&directory.members, member);
        let due = helpers::status_due(label, set_at, false, &doing, now, app.config.status_ttl_ms);
        let Some(set_at) = set_at.filter(|_| due) else {
            continue;
        };
        match member.agent.retire_label(set_at).await {
            Ok(true) => {
                eprintln!(
                    "clarp-host: staleWorkStatusCleared {} ({} min old)",
                    member.record.session,
                    (now - set_at) / 60_000
                );
                app.hub.publish(
                    crate::wire::roster("stale-work", &member.record.session),
                    now,
                );
            }
            // Set again since it was looked at: it stands.
            Ok(false) => {}
            // Nothing is lost but this look: the next one tries again. It is said.
            Err(why) => eprintln!(
                "clarp-host: the stale label of {} could not be taken back: {}",
                member.record.session, why.0
            ),
        }
    }
}
