use sha2::{Digest, Sha256};

/// Match the Python writer's UTF-8 SHA-256 hexdigest in saved target placements.
pub(crate) fn target_digest(text: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let digest = Sha256::digest(text.as_bytes());
    let mut hex = String::with_capacity(digest.len() * 2);
    for &byte in digest.as_slice() {
        hex.push(char::from(HEX[usize::from(byte >> 4)]));
        hex.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    hex
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_digest_matches_python_hexdigest() {
        // This UTF-8 vector also checks that a leading zero is preserved.
        assert_eq!(
            target_digest("译文0"),
            "0edd1638c750077227ad95db07a463ecd08d1f72aa0e9a36c686447a585a8547"
        );
    }
}
