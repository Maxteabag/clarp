//! The built-in Google speaker shares an old adapter id. A choice of the
//! built-in is valid only for the exact packages the operator inspected.
use std::ffi::OsStr;
use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read};
use std::os::fd::AsRawFd;
use std::os::unix::{ffi::OsStrExt, fs::OpenOptionsExt};
use std::path::{Component, Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};
use toml::Table;

const MAX_MANIFEST: u64 = 1024 * 1024;

fn open(path: &Path, directory: bool) -> std::io::Result<File> {
    let flags = nix::libc::O_NOFOLLOW
        | nix::libc::O_NONBLOCK
        | if directory { nix::libc::O_DIRECTORY } else { 0 };
    OpenOptions::new().read(true).custom_flags(flags).open(path)
}

// All components below an open directory are addressed through that fd,
// including when a package changes while the Host inventories it.
fn at(dir: &File) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd()))
}

fn marker(package: &File, exe: &Value) -> Vec<Vec<u8>> {
    let other = || vec![b"other".to_vec()];
    let absent = || vec![b"noexec".to_vec()];
    if exe.is_null() {
        return absent();
    }
    let Some(name) = exe.as_str().map(str::trim).filter(|n| !n.is_empty()) else {
        return other();
    };
    let path = Path::new(name);
    let mut parts = Vec::new();
    for part in path.components() {
        match part {
            Component::Normal(p) => parts.push(p),
            Component::CurDir => {}
            _ => return other(),
        }
    }
    if parts.is_empty() {
        return other();
    }
    let Ok(mut parent) = package.try_clone() else {
        return other();
    };
    let mut walk = PathBuf::new();
    for (i, part) in parts.iter().enumerate() {
        walk.push(part);
        let child = at(&parent).join(part);
        match open(&child, i + 1 != parts.len()) {
            Ok(file) => parent = file,
            Err(_) => {
                return match child.symlink_metadata() {
                    Err(e) if e.kind() == ErrorKind::NotFound => absent(),
                    Ok(m) if m.is_symlink() => match child.read_link() {
                        Ok(target) => vec![
                            b"symlink".to_vec(),
                            walk.as_os_str().as_bytes().to_vec(),
                            target.as_os_str().as_bytes().to_vec(),
                        ],
                        Err(_) => other(),
                    },
                    _ => other(),
                };
            }
        }
    }
    let Ok(meta) = parent.metadata() else {
        return other();
    };
    if !meta.is_file() {
        return other();
    }
    let mut sha = Sha256::new();
    match std::io::copy(&mut parent, &mut sha) {
        Ok(len) if len == meta.len() => vec![
            b"exec".to_vec(),
            len.to_be_bytes().to_vec(),
            sha.finalize().to_vec(),
        ],
        _ => other(),
    }
}

fn candidate(package: &File, relative: &Path, name: &OsStr) -> Result<Option<String>, ()> {
    let file = open(&at(package).join(name), false).map_err(|_| ())?;
    if !file.metadata().map_err(|_| ())?.is_file() {
        return Err(());
    }
    let mut raw = Vec::new();
    file.take(MAX_MANIFEST + 1)
        .read_to_end(&mut raw)
        .map_err(|_| ())?;
    if raw.len() as u64 > MAX_MANIFEST {
        return Err(());
    }
    let value: Value = serde_json::from_slice(&raw).map_err(|_| ())?;
    if !value.is_object() {
        return Err(());
    }
    if !value["id"]
        .as_str()
        .is_some_and(|s| s.trim().eq_ignore_ascii_case("google"))
    {
        return Ok(None);
    }
    let mut hash = Sha256::new();
    let markers = marker(package, &value["executable"]);
    for part in [
        b"clarp-google-collision-v1".as_slice(),
        relative.as_os_str().as_bytes(),
        &raw,
    ]
    .into_iter()
    .chain(markers.iter().map(Vec::as_slice))
    {
        hash.update((part.len() as u64).to_be_bytes());
        hash.update(part);
    }
    Ok(Some(format!("{:x}", hash.finalize())))
}

fn inventory(root: &Path) -> Result<Vec<String>, ()> {
    let dir = match open(root, true) {
        Ok(d) => d,
        Err(e) if e.kind() == ErrorKind::NotFound => return Ok(vec![]),
        Err(_) => return Err(()),
    };
    let mut ids = Vec::new();
    for entry in std::fs::read_dir(at(&dir)).map_err(|_| ())? {
        let entry = entry.map_err(|_| ())?;
        let name = entry.file_name();
        let kind = entry.file_type().map_err(|_| ())?;
        if kind.is_symlink() {
            return Err(());
        }
        if kind.is_dir() {
            let package = open(&entry.path(), true).map_err(|_| ())?;
            let manifest = OsStr::new("manifest.json");
            match at(&package).join(manifest).symlink_metadata() {
                Err(e) if e.kind() == ErrorKind::NotFound => continue,
                Err(_) => return Err(()),
                Ok(_) => {}
            }
            ids.extend(candidate(
                &package,
                &Path::new(&name).join(manifest),
                manifest,
            )?);
        } else if name.as_bytes().ends_with(b".json") {
            ids.extend(candidate(&dir, Path::new(&name), &name)?);
        }
    }
    ids.sort();
    Ok(ids)
}

pub fn refusal(root: &Path, owners: &Table) -> Option<&'static str> {
    let Ok(ids) = inventory(root) else {
        return Some("google_id_unverifiable");
    };
    if ids.is_empty() {
        return None;
    }
    let resolution = owners.get("tts").and_then(|v| v.get("google_resolution"));
    if let Some(r) = resolution
        && r.get("choice").and_then(toml::Value::as_str) == Some("builtin")
        && let Some(mut packages) =
            r.get("packages")
                .and_then(toml::Value::as_array)
                .and_then(|a| {
                    a.iter()
                        .map(toml::Value::as_str)
                        .collect::<Option<Vec<_>>>()
                })
    {
        packages.sort();
        if packages == ids {
            return None;
        }
    }
    Some("google_id_collision")
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    #[test]
    fn google_collisions_and_acknowledgements_match_the_reference() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/speech-engines-vectors.json"
        ))
        .unwrap();
        let root =
            std::env::temp_dir().join(format!("clarp-voice-collision-{}", std::process::id()));
        for case in fixture["guards"].as_array().unwrap() {
            let dir = root.join("sample");
            std::fs::create_dir_all(&dir).unwrap();
            let raw = case["raw"].as_str().unwrap();
            std::fs::write(dir.join("manifest.json"), raw).unwrap();
            if let Some(program) = case["executable"].as_str() {
                let data: Value = serde_json::from_str(raw).unwrap();
                let target = dir.join(data["executable"].as_str().unwrap());
                std::fs::create_dir_all(target.parent().unwrap()).unwrap();
                std::fs::write(target, program).unwrap();
            }
            let config = serde_json::from_value::<Table>(
                serde_json::json!({"tts":{"google_resolution":case["resolution"]}}),
            )
            .unwrap();
            assert_eq!(refusal(&root, &config), case["code"].as_str(), "{case}");
            std::fs::remove_dir_all(&root).unwrap();
        }
        assert_eq!(refusal(&root, &Table::new()), None);
    }
}
