//! PCM WAVE duration as read from the RIFF chunks by Python's wave module.
pub fn wav_seconds(bytes: &[u8]) -> Option<f64> {
    fn word(bytes: &[u8]) -> Option<u16> {
        Some(u16::from_le_bytes(bytes.get(..2)?.try_into().ok()?))
    }
    fn size(bytes: &[u8]) -> Option<u32> {
        Some(u32::from_le_bytes(bytes.get(..4)?.try_into().ok()?))
    }
    if bytes.get(..4)? != b"RIFF" || bytes.get(8..12)? != b"WAVE" {
        return None;
    }
    let limit = (8u64 + u64::from(size(&bytes[4..])?)).min(bytes.len() as u64) as usize;
    let mut chunks = bytes.get(12..limit)?;
    let mut format = None;
    while let Some(header) = chunks.get(..8) {
        let length = size(&header[4..])? as usize;
        match &header[..4] {
            b"fmt " => {
                let data = chunks.get(8..8usize.checked_add(length)?)?;
                match word(data)? {
                    1 => {}
                    0xfffe
                        if data.get(24..40)?
                            == [1, 0, 0, 0, 0, 0, 16, 0, 128, 0, 0, 170, 0, 56, 155, 113] => {}
                    _ => return None,
                }
                let channels = u32::from(word(data.get(2..)?)?);
                let rate = size(data.get(4..)?)?;
                let width = u32::from(word(data.get(14..)?)?).div_ceil(8);
                let frame = channels * width;
                if frame == 0 || rate == 0 {
                    return None;
                }
                format = Some((frame, rate));
            }
            b"data" => {
                let (frame, rate) = format?;
                let frames = length as u32 / frame;
                return (frames > 0).then(|| f64::from(frames) / f64::from(rate));
            }
            _ => {}
        }
        chunks = chunks.get(8usize.checked_add(length)?.checked_add(length % 2)?..)?;
    }
    None
}
pub fn long_model(model: &str, threshold: i64, seconds: Option<f64>) -> &str {
    if seconds.is_some_and(|s| s.is_finite() && s > 0. && s >= threshold as f64) {
        model.trim()
    } else {
        ""
    }
}
