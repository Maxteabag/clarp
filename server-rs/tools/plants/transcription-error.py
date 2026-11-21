#!/usr/bin/env python3
"""Wrong client words and disclosures at transcription failure boundaries."""
import runner

K = 'clarp-kernel/src/heard.rs'
H = 'clarp-host/src/heard.rs'
C = 'clarp-host/src/heard/call.rs'
D = 'clarp-host/src/diagnostics.rs'
S = 'clarp-host/src/listening.rs'
RUNS = {
    'words': ['test', '-p', 'clarp-kernel', '--test', 'transcription_error_vectors'],
    'provider': ['test', '-p', 'clarp-host', '--test', 'host', 'a_call_clip_is_heard_for_its_captured_participant'],
    'busy': ['test', '-p', 'clarp-host', '--test', 'host', 'a_loading_or_busy_transcription_has_fixed_client_and_call_words'],
    'stream': ['test', '-p', 'clarp-host', '--test', 'host', 'a_phone_streams_what_it_hears'],
    'health': ['test', '-p', 'clarp-host', '--lib', 'diagnostic_producers_record_metadata_under_their_own_categories'],
}
PLANTS = []
for status, word in [
    (400, 'transcription model not installed'), (503, 'transcription model loading'),
    (429, 'whisper busy'), (500, 'transcription failed'),
]:
    anchor = f'({status}, "{word}")'
    PLANTS.append((K, 'words', f'{status} uses the wrong status', anchor, f'(418, "{word}")'))
    PLANTS.append((K, 'words', f'{status} uses the wrong words', anchor, f'({status}, "engine failed")'))

PLANTS += [
    (K, 'words', 'busy voice event gets a failure stage',
     'Self::Busy => Some(json!({"message":message})),',
     'Self::Busy => Some(json!({"message":message,"stage":"stt"})),'),
    (K, 'words', 'failure voice event loses its stage',
     'Self::Failed => Some(json!({"message":message,"stage":"stt"})),',
     'Self::Failed => Some(json!({"message":message})),'),
    (K, 'words', 'a model refusal adds a voice error event',
     'Self::UnknownModel | Self::Loading => None,',
     'Self::UnknownModel | Self::Loading => Some(json!({"message":message})), '),
    (H, 'provider', 'a provider error replies with private detail',
     'refuse(status, self.word())', 'refuse(status, "/fixture/private/provider-model.bin")'),
    (H, 'provider', 'a provider error suppresses its log detail',
     'eprintln!("clarp-host: {}: {detail}", kind.client_words().1);', 'let _ = detail;'),
    (H, 'provider', 'the upload drops the provider failure voice event',
     'StatusCode::INTERNAL_SERVER_ERROR => Failure::Failed.voice_detail(),',
     'StatusCode::INTERNAL_SERVER_ERROR => None,'),
    (H, 'busy', 'the upload drops the busy voice event',
     'StatusCode::TOO_MANY_REQUESTS => Failure::Busy.voice_detail(),',
     'StatusCode::TOO_MANY_REQUESTS => None,'),
    (H, 'busy', 'a waiting upload is treated as a generic failure in health',
     'Self::Failed(Failure::Busy) => "busy",', 'Self::Failed(Failure::Busy) => "failed",'),
    (C, 'busy', 'a busy attempt is reported as a generic failure',
     'let reason = error.word();', 'let reason = Failure::Failed.client_words().1;'),
    (C, 'provider', 'a call attempt reveals a private provider path',
     'let reason = error.word();', 'let reason = "/fixture/private/provider-model.bin";'),
    (D, 'provider', 'health ignores the fixed word from the failing subsystem',
     'e.error.map(|(_, word)| word)', 'e.error.map(|_|"failed")'),
    (D, 'health', 'synthesis failure is recorded with a different word',
     '(!ok).then_some("speech failed")', '(!ok).then_some("failed")'),
    (S, 'stream', 'the socket relays the private provider message',
     'line.tell(said(&json!({"type":"error","message":word}))).await;',
     'line.tell(said(&message)).await;'),
    (S, 'stream', 'the provider message is lost from the private log',
     'eprintln!("clarp-host: listening stream failed: {message}");', ''),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
