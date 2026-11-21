//! One version answer for each installed provider executable and modification time.

use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime};

use tokio::sync::OnceCell;

struct Probe {
    modified: SystemTime,
    answer: Arc<OnceCell<Option<Arc<str>>>>,
}

#[derive(Default)]
pub(crate) struct Cache {
    by_program: Mutex<HashMap<PathBuf, Probe>>,
}

fn executable(program: &Path) -> Option<(PathBuf, SystemTime)> {
    let locate = |path: &Path| {
        let path = std::path::absolute(path).ok()?;
        let metadata = path.metadata().ok()?;
        if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
            return None;
        }
        Some((path, metadata.modified().ok()?))
    };
    if program.as_os_str().as_encoded_bytes().contains(&b'/') {
        return locate(program);
    }
    std::env::split_paths(&std::env::var_os("PATH")?)
        .find_map(|directory| locate(&directory.join(program)))
}

impl Cache {
    pub(crate) async fn answer(&self, program: PathBuf) -> Option<Arc<str>> {
        let (path, modified) = tokio::task::spawn_blocking(move || executable(&program))
            .await
            .ok()??;
        let answer = {
            let mut programs = self
                .by_program
                .lock()
                .unwrap_or_else(PoisonError::into_inner);
            let probe = programs.entry(path.clone()).or_insert_with(|| Probe {
                modified,
                answer: Arc::default(),
            });
            if probe.modified != modified {
                *probe = Probe {
                    modified,
                    answer: Arc::default(),
                };
            }
            probe.answer.clone()
        };
        answer
            .get_or_init(|| async {
                crate::models::asked(&path, &["--version".into()], Duration::from_secs(2))
                    .await
                    .filter(|(code, _)| *code == 0)
                    .map(|(_, output)| Arc::from(output))
            })
            .await
            .clone()
    }
}
