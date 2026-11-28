// Build as a clarp-host example in the predecessor checkout. Reads one
// attested fixture agent without taking its writer or starting a process.
use clarp_kernel::conversation::Message;
use clarp_kernel::journal::{Recorder, kind};
use clarp_ports::log::{Kinds, LogEngine};
use std::path::PathBuf;

fn main() {
    let path = PathBuf::from(std::env::args_os().nth(1).expect("fixture agent directory"));
    let log = clarp_store_sqlite::SqliteEngine::new()
        .read_committed(&path, clarp_codec::standard_set("none").unwrap())
        .unwrap();
    let recorder = Recorder::open(log).unwrap();
    let rows: Vec<_> = recorder.agent().waiting().map(|waiting| {
        let row = recorder.log().read_after(waiting.message_seq - 1, Kinds::only(kind::WAITING), 1).unwrap().pop().unwrap();
        assert_eq!(row.seq, waiting.message_seq);
        let message: Message = serde_json::from_slice(&row.body).unwrap();
        serde_json::json!({"id":message.client_id(), "turn":waiting.id.0, "reserved":recorder.agent().joins().contains_key(&waiting.id)})
    }).collect();
    println!("{}", serde_json::json!(rows));
}
