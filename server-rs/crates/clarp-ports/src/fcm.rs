//! HTTP operations used by Firebase Cloud Messaging.
pub struct Response {
    pub status: u16,
    pub retry_after: String,
    pub body: Vec<u8>,
}

pub trait FcmHttp: Send + Sync {
    fn exchange(&self, issuer: &str, assertion: &str) -> Option<Response>;
    fn deliver(&self, project: &str, bearer: &str, message: &[u8]) -> Option<Response>;
}
