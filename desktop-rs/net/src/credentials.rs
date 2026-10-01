//! Device tokens in the freedesktop Secret Service (gnome-keyring, KWallet),
//! one item per Host, found by `application` and `server` attributes. Uses a
//! plain session like the C++ client, and treats any unlock or delete prompt
//! as a failure: the app never pops keyring dialogs on its own.

use std::collections::HashMap;

use zbus::zvariant::{OwnedObjectPath, Value};
use zbus::Connection;

const SERVICE: &str = "org.freedesktop.secrets";
const SERVICE_PATH: &str = "/org/freedesktop/secrets";
const SERVICE_INTERFACE: &str = "org.freedesktop.Secret.Service";
const COLLECTION_PATH: &str = "/org/freedesktop/secrets/aliases/default";
const COLLECTION_INTERFACE: &str = "org.freedesktop.Secret.Collection";
const ITEM_INTERFACE: &str = "org.freedesktop.Secret.Item";
pub const APPLICATION: &str = "com.maxteabag.Clarp";
const LABEL: &str = "Clarp native desktop token";

/// `(session, parameters, value, content type)`, the Secret Service `Secret`.
type Secret = (OwnedObjectPath, Vec<u8>, Vec<u8>, String);

fn attributes(server_url: &str) -> HashMap<&str, &str> {
    HashMap::from([("application", APPLICATION), ("server", server_url)])
}

fn is_prompt(path: &OwnedObjectPath) -> bool {
    !path.as_str().is_empty() && path.as_str() != "/"
}

async fn open_session(connection: &Connection) -> zbus::Result<OwnedObjectPath> {
    let reply = connection
        .call_method(Some(SERVICE), SERVICE_PATH, Some(SERVICE_INTERFACE), "OpenSession", &("plain", Value::from("")))
        .await?;
    let (_, session): (zbus::zvariant::OwnedValue, OwnedObjectPath) = reply.body().deserialize()?;
    Ok(session)
}

async fn search(connection: &Connection, server_url: &str) -> zbus::Result<(Vec<OwnedObjectPath>, Vec<OwnedObjectPath>)> {
    let reply = connection
        .call_method(Some(SERVICE), SERVICE_PATH, Some(SERVICE_INTERFACE), "SearchItems", &(attributes(server_url),))
        .await?;
    reply.body().deserialize()
}

/// The stored token for `server_url`, or an empty string when there is none,
/// the item is locked, or no Secret Service is running.
pub async fn lookup(server_url: &str) -> String {
    match try_lookup(server_url).await {
        Ok(token) => token,
        Err(error) => {
            eprintln!("credentials: lookup for {server_url} failed: {error}");
            String::new()
        }
    }
}

async fn try_lookup(server_url: &str) -> zbus::Result<String> {
    let connection = Connection::session().await?;
    let session = open_session(&connection).await?;
    let (unlocked, _) = search(&connection, server_url).await?;
    let Some(item) = unlocked.first() else { return Ok(String::new()) };
    let reply = connection
        .call_method(Some(SERVICE), item.as_str(), Some(ITEM_INTERFACE), "GetSecret", &(&session,))
        .await?;
    let (secret,): (Secret,) = reply.body().deserialize()?;
    Ok(String::from_utf8_lossy(&secret.2).trim().to_owned())
}

/// Stores (replacing) the token for `server_url`.
pub async fn store(server_url: &str, token: &str) -> Result<(), String> {
    let connection = Connection::session().await.map_err(|_| "Secret Service session could not be opened".to_owned())?;
    let session = open_session(&connection).await.map_err(|_| "Secret Service session could not be opened".to_owned())?;
    let mut properties: HashMap<&str, Value> = HashMap::new();
    properties.insert("org.freedesktop.Secret.Item.Label", Value::from(LABEL));
    properties.insert("org.freedesktop.Secret.Item.Attributes", Value::from(attributes(server_url)));
    let secret: Secret = (session, Vec::new(), token.as_bytes().to_vec(), "text/plain; charset=utf-8".into());
    let reply = connection
        .call_method(Some(SERVICE), COLLECTION_PATH, Some(COLLECTION_INTERFACE), "CreateItem", &(properties, secret, true))
        .await
        .map_err(|error| error.to_string())?;
    let (item, prompt): (OwnedObjectPath, OwnedObjectPath) = reply.body().deserialize().map_err(|error| error.to_string())?;
    if (item.as_str().is_empty() || item.as_str() == "/") && is_prompt(&prompt) {
        return Err("Secret Service requires an unlock prompt".into());
    }
    Ok(())
}

/// Deletes the token for `server_url`; succeeds when there was none.
pub async fn remove(server_url: &str) -> Result<(), String> {
    let connection = Connection::session().await.map_err(|error| error.to_string())?;
    let (mut items, locked) = search(&connection, server_url).await.map_err(|error| error.to_string())?;
    items.extend(locked);
    let Some(item) = items.first() else { return Ok(()) };
    let reply = connection
        .call_method(Some(SERVICE), item.as_str(), Some(ITEM_INTERFACE), "Delete", &())
        .await
        .map_err(|error| error.to_string())?;
    let prompt: OwnedObjectPath = reply.body().deserialize().map_err(|error| error.to_string())?;
    if is_prompt(&prompt) {
        return Err("Secret Service requires a delete prompt".into());
    }
    Ok(())
}
