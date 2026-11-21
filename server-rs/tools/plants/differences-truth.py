"""Wrong rules for what a device with limited access is told, and for the gate of the desk of the seats."""
import runner

D='clarp-kernel/src/devices.rs'
G='clarp-kernel/src/desk.rs'
RUNS={
    'scope':['test','--test','device_scope_vectors'],
    'gate':['test','--test','hotseat_gate_vectors'],
    'limited':['test','--test','host','a_limited_device_is_refused_as_the_python_host_refuses_it'],
    'desk':['test','--test','host','the_owner_turns_the_desk_of_the_seats_off'],
    'screens':['test','--test','host','a_screen_of_something_this_host_has_not_is_answered'],
}
PLANTS=[]
# The GET allowlist independently denies the old full-only and disk/terminal paths.
# The former denylist plants are obsolete because opening those guards would grant nothing.
LIMITED_POSTS=['/send','/transcribe','/upload','/select','/focus','/clips/ack','/clog','/location',
               '/calendar/response','/remote-action','/voice-events']
for index, entry in enumerate(LIMITED_POSTS):
    after = f'    "{LIMITED_POSTS[index + 1]}",' if index + 1 < len(LIMITED_POSTS) else '];'
    PLANTS.append((D,'scope',f'refuse {entry} to a limited device',
                   f'    "{entry}",\n{after}',f'    "/never-sent{entry}",\n{after}'))
for run,label,old,new in [
    ('scope','other words','"full device access required"','"this device may not do that"'),
    ('scope','a PATCH never looked at','method != "POST" || !LIMITED_POSTS.contains(&path)','method != "PATCH" && (method != "POST" || !LIMITED_POSTS.contains(&path))'),
    ('scope','reads refused','if method == "GET" {','if method == "OPTIONS" {'),
    ('scope','every change of an action allowed','return !(method == "POST" && invoked(path).is_some());','return false;'),
    ('scope','an invoke refused','return !(method == "POST" && invoked(path).is_some());','return true;'),
    ('scope','an invoke by any method','return !(method == "POST" && invoked(path).is_some());','return invoked(path).is_none();'),
    ('scope','an empty id invoked','(!id.is_empty() && !id.contains(\'/\'))','(!id.contains(\'/\'))'),
    ('scope','an id with a slash invoked','(!id.is_empty() && !id.contains(\'/\'))','(!id.is_empty())'),
    ('scope','an id read without its %XX','u8::from_str_radix(two, 16)','u8::from_str_radix(two, 10)'),
    ('scope','a send by any method','method != "POST" || !LIMITED_POSTS.contains(&path)','!LIMITED_POSTS.contains(&path)'),
]:
    PLANTS.append((D,run,label,old,new))
for label,old,new in [
    ('a desk turned off is asked','if !on {','if false {'),
    ('a desk that is not there is asked','if !there {','if false {'),
    ('unavailable before turned off','if !on {\n        return Gate::Off;\n    }\n    if !there {\n        return Gate::Unavailable;\n    }','if !there {\n        return Gate::Unavailable;\n    }\n    if !on {\n        return Gate::Off;\n    }'),
    ('a read is no object','let Some(body) = body else {\n        return Gate::Asked(Value::Object(Map::new()));','let Some(body) = body else {\n        return Gate::NoObject;'),
    ('an empty body is no object','if body.is_empty() {','if false {'),
    ('any JSON is asked','Some(asked @ Value::Object(_)) => Gate::Asked(asked),','Some(asked) => Gate::Asked(asked),'),
    ('other words for no desk','"hotseat desk unavailable"','"seats desk unavailable"'),
    ('other words for no object','"bad json"','"invalid json"'),
    ('anything but true turns it off','desk == Some(&Value::Bool(false))','desk.is_some_and(|desk| desk != &Value::Bool(true))'),
    ('nothing turns it off','desk == Some(&Value::Bool(false))','false'),
]:
    PLANTS.append((G,'gate',label,old,new))
for file,run,label,old,new in [
    ('clarp-host/src/edge.rs','limited','a head asked as a change','"GET"\n    } else {\n        method.as_str()','"HEAD"\n    } else {\n        method.as_str()'),
    ('clarp-host/src/edge.rs','limited','the edge says other words','clarp_kernel::devices::LIMITED_REFUSAL\n','"this device may not do that"\n'),
    ('clarp-host/src/edge.rs','desk','the apps told of a desk turned off','app.seats.told() && !crate::seats::turned_off(app)','app.seats.told()'),
    ('clarp-host/src/screens.rs','desk','the desk read when turned off','if crate::seats::turned_off(&app) {','if false {'),
    ('clarp-host/src/screens.rs','desk','a change asked when turned off','desk::gate(!crate::seats::turned_off(app), true, Some(body))','desk::gate(true, true, Some(body))'),
    ('clarp-host/src/screens.rs','desk','turned off is a bad request','Gate::Off => Err(ApiError::plain(StatusCode::NOT_FOUND, "not found"))','Gate::Off => Err(bad("not found"))'),
    ('clarp-host/src/screens.rs','screens','another action not built','.route("/hotseat/switch", post(hotseat_switch))','.route("/hotseat/switch", post(hotseat_switch))\n        .route("/hotseat/{action}", post(|| async { not_built("Changing the account seats is") }))'),
    ('clarp-host/src/seats.rs','desk','a desk written true is off','toml::Value::Boolean(on) => Value::Bool(*on),','toml::Value::Boolean(_) => Value::Bool(false),'),
    ('clarp-host/src/seats.rs','desk','a desk written as text is off','            _ => Value::Null,\n        });\n    clarp_kernel::desk::turned_off','            _ => Value::Bool(false),\n        });\n    clarp_kernel::desk::turned_off'),
    ('clarp-host/src/seats.rs','desk','another table read','.and_then(|file| file.get("hotseat"))','.and_then(|file| file.get("seats"))'),
]:
    PLANTS.append((file,run,label,old,new))
if __name__=='__main__':
    raise SystemExit(runner.main(__file__,RUNS,PLANTS))
