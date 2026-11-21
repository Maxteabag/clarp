"""Wrong rules for terminal portrait gallery hints (PR 510)."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'portrait_media_vectors'],
    'hints': ['test', '-p', 'clarp-host', '--lib', 'media_created::tests::'],
    'host': ['test', '--test', 'host', '--', 'agent_portraits::the_owner_asks_for_two_portrait_edits'],
}
K = 'clarp-kernel/src/gallery.rs'
G = 'clarp-host/src/gallery.rs'
A = 'clarp-host/src/media_created.rs'
P = 'clarp-host/src/portrait_generation.rs'
F = 'clarp-host/src/fleet/portrait_jobs.rs'

PLANTS = [
    (K, 'vectors', 'a portrait born at the start is missed',
     '.range((Included((since, String::new())), Unbounded))',
     '.range((Included((since.saturating_add(1), String::new())), Unbounded))'),
    (K, 'vectors', 'a portrait born at the end is missed',
     '.take_while(|(at, _)| *at <= until)', '.take_while(|(at, _)| *at < until)'),
    (K, 'vectors', 'a later portrait is included',
     '.take_while(|(at, _)| *at <= until)', '.take_while(|(at, _)| *at <= until.saturating_add(1))'),
    (K, 'vectors', 'a producer appearing within a name is sufficient',
     '!image.asset.created_by.starts_with(prefix)', '!image.asset.created_by.contains(prefix)'),
    (K, 'vectors', 'discarded at the start is too early',
     'Some(deletion) if deletion.at >= since', 'Some(deletion) if deletion.at > since'),
    (K, 'vectors', 'a discard before the run is included',
     'Some(deletion) if deletion.at >= since', 'Some(deletion) if deletion.at >= since - 1'),
    (K, 'vectors', 'a discard after the terminal timestamp is missed',
     'Some(deletion) if deletion.at >= since', 'Some(deletion) if deletion.at >= since && deletion.at <= until'),
    (K, 'vectors', 'the selector reads text publications',
     'let is_gallery = !image.asset.named_only && is_image(&image.asset.mime_type);',
     'let is_gallery = !image.asset.named_only;'),
    (K, 'vectors', 'a deleted portrait is treated as kept',
     'discarded.push(image)', 'kept.push(image)'),
    (K, 'vectors', 'a running portrait only sees the tombstones',
     'None => kept.push(image),', 'None => {},'),
    (G, 'hints', 'a portrait selection leaks another chat',
     '&window.session,', '"elm",'),
    (G, 'hints', 'deleted hints use the gallery creation timestamp',
     'Hint::of(image, "deleted", deleted.at)', 'Hint::of(image, "deleted", image.asset.created_at)'),
    (G, 'hints', 'a discarded portrait is announced as created',
     'Hint::of(image, "deleted", deleted.at)', 'Hint::of(image, "created", deleted.at)'),
    (G, 'hints', 'the writer does not remember which hints it sent',
     'if self.db.get(HINTS, &key)?.is_none() {', 'if true {'),
    (G, 'hints', 'the recorded selection is emitted in reverse',
     'hints.sort_unstable_by(|a, b| (a.at, &a.asset_id).cmp(&(b.at, &b.asset_id)));',
     'hints.reverse();'),
    (A, 'hints', 'an announcement failure is silent',
     'self.failed(why.words());', 'let _ = why;'),
    (P, 'host', 'the successful gallery delivery does not announce',
     'Ok(Some(window)) => announcer.tell(window).await,',
     'Ok(Some(_)) => {},'),
    (F, 'host', 'the durable delivery keeps no window',
     'staged.media_window = Some(window);', 'staged.media_window = None;'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
