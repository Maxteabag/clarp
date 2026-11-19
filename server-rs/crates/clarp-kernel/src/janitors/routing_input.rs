//! Transport chosen for a one-shot prompt from the final arguments of its command.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Input {
    Arguments,
    Stdin,
    File,
}

/// For argument-only tools the terminating NUL counts toward Linux's 128 KiB limit.
pub fn check(backend: &str, args: &[String]) -> Result<(), String> {
    if args.iter().any(|arg| arg.len() >= 128 * 1024) {
        Err(format!(
            "prompt too large for {backend}: the CLI takes it only as an argument"
        ))
    } else {
        Ok(())
    }
}

/// Removes an inline prompt and returns the input still to be supplied. For a file
/// the caller appends the path it created; the private path is never part of policy.
pub fn prepare(backend: &str, args: &mut Vec<String>, prompt: &str) -> Result<Input, String> {
    if args.last().is_some_and(|last| last == prompt) {
        match backend {
            "claude" | "codex" => {
                args.pop();
                if backend == "codex" {
                    args.push("-".into());
                }
                return Ok(Input::Stdin);
            }
            "grok" | "muse" => {
                let flag = if backend == "grok" { "-p" } else { "--" };
                if args.len() >= 2 && args[args.len() - 2] == flag {
                    args.truncate(args.len() - 2);
                    args.push("--prompt-file".into());
                    return Ok(Input::File);
                }
            }
            _ => {}
        }
    }
    check(backend, args)?;
    Ok(Input::Arguments)
}

/// Encode system text as Python's UTF-8 surrogateescape encoder does. Accept code
/// points here because Rust strings cannot represent an unpaired surrogate.
pub fn prompt_bytes(
    backend: &str,
    points: impl IntoIterator<Item = u32>,
) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    for n in points {
        if (0xdc80..=0xdcff).contains(&n) {
            bytes.push((n - 0xdc00) as u8);
        } else if let Some(c) = char::from_u32(n) {
            bytes.extend_from_slice(c.encode_utf8(&mut [0; 4]).as_bytes());
        } else {
            return Err(format!("prompt cannot be encoded for {backend}"));
        }
    }
    Ok(bytes)
}

/// Six significant digits, as used by the reference's timeout messages.
pub fn timeout(backend: &str, seconds: f64) -> String {
    let scientific = format!("{seconds:.5e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let power = exponent.parse::<i32>().unwrap_or(0);
    let trim = |s: String| {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            s
        }
    };
    let duration = if !(-4..6).contains(&power) {
        format!("{}e{power:+03}", trim(mantissa.into()))
    } else {
        trim(format!(
            "{seconds:.precision$}",
            precision = (5 - power) as usize
        ))
    };
    format!("{backend} timed out after {duration} s")
}

pub fn public_error(text: &str) -> String {
    static OLD: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        #[allow(clippy::expect_used)]
        regex::Regex::new(
            r"(?s)\ACommand '\[(?:'([^']*)')?.*\]' timed out after ([0-9.]+) seconds\z",
        )
        .expect("stored timeout pattern")
    });
    let Some(caps) = OLD.captures(text) else {
        return text.into();
    };
    let name = caps
        .get(1)
        .and_then(|s| s.as_str().rsplit('/').next())
        .filter(|s| !s.is_empty())
        .unwrap_or("the routing CLI");
    match caps[2].parse() {
        Ok(seconds) => timeout(name, seconds),
        Err(_) => format!("{name} timed out"),
    }
}
