//! What this device's network looks like, from its interface list: whether
//! it has any network at all, of which kind, and whether Tailscale (or
//! another VPN) is up. Read when a Host stops answering and while one is
//! down, to tell "this device is offline" and "Tailscale is off" apart from
//! a Host that is away, and to probe at once when the network changes.

use std::net::IpAddr;

use clarp_core::host_down::{DeviceNetwork, is_tailnet_ip};

/// One interface address, as the operating system lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Interface {
    pub name: String,
    pub address: IpAddr,
    pub up: bool,
    pub loopback: bool,
    /// The interface is a wireless one (Linux tells; elsewhere false).
    pub wireless: bool,
}

/// Interfaces that carry no network of their own: containers, bridges,
/// virtual machines.
fn virtual_name(name: &str) -> bool {
    ["docker", "br-", "veth", "virbr", "vmnet", "vboxnet", "lxc", "lxd", "cni", "flannel", "podman", "awdl", "llw", "anpi", "ap", "bridge", "gif", "stf"]
        .iter()
        .any(|prefix| name.starts_with(prefix))
}

fn vpn_name(name: &str) -> bool {
    ["tailscale", "utun", "tun", "wg", "ipsec", "zt"].iter().any(|prefix| name.starts_with(prefix))
}

fn usable(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(v4) => !v4.is_unspecified() && !v4.is_link_local(),
        IpAddr::V6(v6) => !v6.is_unspecified() && (v6.segments()[0] & 0xffc0) != 0xfe80,
    }
}

/// The network the interfaces show.
pub fn network_of(interfaces: &[Interface]) -> DeviceNetwork {
    let up: Vec<&Interface> = interfaces.iter().filter(|i| i.up && !i.loopback && usable(i.address)).collect();
    let tailscale = up.iter().any(|i| i.name.starts_with("tailscale") || is_tailnet_ip(i.address));
    let vpn = tailscale || up.iter().any(|i| vpn_name(&i.name));
    let physical: Vec<&&Interface> = up.iter().filter(|i| !vpn_name(&i.name) && !virtual_name(&i.name) && !is_tailnet_ip(i.address)).collect();
    let online = !physical.is_empty();
    let kind = if physical.iter().any(|i| i.wireless) {
        Some("wifi")
    } else if physical.iter().any(|i| i.name.starts_with("ppp")) {
        Some("cellular")
    } else if cfg!(target_os = "linux") && online {
        Some("ethernet")
    } else {
        None
    };
    DeviceNetwork { online: Some(online), kind, tailscale: Some(vpn) }
}

/// What changed in the network is told by this: the up interfaces and
/// their addresses, sorted.
pub fn fingerprint(interfaces: &[Interface]) -> String {
    let mut seen: Vec<String> = interfaces.iter().filter(|i| i.up && !i.loopback).map(|i| format!("{}={}", i.name, i.address)).collect();
    seen.sort();
    seen.join(",")
}

/// This device's interfaces now; None where they cannot be listed.
#[cfg(unix)]
pub fn interfaces() -> Option<Vec<Interface>> {
    let mut list: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs fills `list` with a linked list freed below.
    if unsafe { libc::getifaddrs(&mut list) } != 0 {
        return None;
    }
    let mut found = Vec::new();
    let mut cursor = list;
    while !cursor.is_null() {
        // SAFETY: a node of the list getifaddrs returned, alive until freed.
        let entry = unsafe { &*cursor };
        cursor = entry.ifa_next;
        if entry.ifa_addr.is_null() || entry.ifa_name.is_null() {
            continue;
        }
        // SAFETY: ifa_name is a NUL-terminated C string.
        let name = unsafe { std::ffi::CStr::from_ptr(entry.ifa_name) }.to_string_lossy().into_owned();
        // SAFETY: ifa_addr points at a sockaddr of the family it names.
        let address = unsafe {
            match i32::from((*entry.ifa_addr).sa_family) {
                libc::AF_INET => {
                    let v4 = &*(entry.ifa_addr as *const libc::sockaddr_in);
                    IpAddr::from(u32::from_be(v4.sin_addr.s_addr).to_be_bytes())
                }
                libc::AF_INET6 => {
                    let v6 = &*(entry.ifa_addr as *const libc::sockaddr_in6);
                    IpAddr::from(v6.sin6_addr.s6_addr)
                }
                _ => continue,
            }
        };
        let flags = entry.ifa_flags;
        let up = flags & (libc::IFF_UP as u32) != 0 && flags & (libc::IFF_RUNNING as u32) != 0;
        let loopback = flags & (libc::IFF_LOOPBACK as u32) != 0;
        let wireless = cfg!(target_os = "linux") && std::path::Path::new("/sys/class/net").join(&name).join("wireless").exists();
        found.push(Interface { name, address, up, loopback, wireless });
    }
    // SAFETY: the list getifaddrs returned, freed once.
    unsafe { libc::freeifaddrs(list) };
    Some(found)
}

#[cfg(not(unix))]
pub fn interfaces() -> Option<Vec<Interface>> {
    None
}

/// The device's network now (unknown where it cannot be read).
pub fn network() -> DeviceNetwork {
    interfaces().map_or_else(DeviceNetwork::default, |list| network_of(&list))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn iface(name: &str, address: &str, up: bool) -> Interface {
        Interface { name: name.into(), address: address.parse().unwrap(), up, loopback: name == "lo", wireless: name.starts_with("wl") }
    }

    #[test]
    fn wifi_and_tailscale_are_seen() {
        let list = [iface("lo", "127.0.0.1", true), iface("wlan0", "192.168.1.5", true), iface("tailscale0", "100.90.1.2", true)];
        let network = network_of(&list);
        assert_eq!(network.online, Some(true));
        assert_eq!(network.kind, Some("wifi"));
        assert_eq!(network.tailscale, Some(true));
    }

    #[test]
    fn only_loopback_bridges_and_a_vpn_is_offline() {
        let list = [
            iface("lo", "127.0.0.1", true),
            iface("docker0", "172.17.0.1", true),
            iface("tailscale0", "100.90.1.2", true),
            iface("wlan0", "192.168.1.5", false),
            iface("enp3s0", "fe80::1", true),
        ];
        let network = network_of(&list);
        assert_eq!(network.online, Some(false));
        assert_eq!(network.tailscale, Some(true));
    }

    #[test]
    fn no_tailscale_without_its_interface_or_range() {
        let network = network_of(&[iface("enp3s0", "10.0.0.4", true)]);
        assert_eq!((network.online, network.tailscale), (Some(true), Some(false)));
        // macOS: Tailscale is a utun with a 100.64/10 address.
        assert_eq!(network_of(&[iface("en0", "10.0.0.4", true), iface("utun4", "100.100.1.1", true)]).tailscale, Some(true));
    }

    #[test]
    fn the_fingerprint_follows_up_interfaces() {
        let a = [iface("wlan0", "192.168.1.5", true), iface("tailscale0", "100.90.1.2", true)];
        let b = [iface("wlan0", "192.168.1.5", true), iface("tailscale0", "100.90.1.2", false)];
        assert_ne!(fingerprint(&a), fingerprint(&b));
        assert_eq!(fingerprint(&a), fingerprint(&[a[1].clone(), a[0].clone()]));
    }

    #[test]
    fn this_device_can_be_read() {
        assert!(interfaces().is_some_and(|list| list.iter().any(|i| i.loopback)));
    }
}
