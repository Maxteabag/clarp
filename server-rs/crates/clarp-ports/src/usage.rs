//! One read of a provider's quota. A transport releases its request-scoped credentials and
//! returns only the observation, or none if the provider could not answer.
pub trait UsageReader {
    type Reading;
    fn read(self) -> impl std::future::Future<Output = Option<Self::Reading>> + Send;
}
