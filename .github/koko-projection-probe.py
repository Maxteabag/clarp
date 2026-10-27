#!/usr/bin/python3
"""CI debug-check instrumentation only. Never run for the shipping release build."""
import pathlib,hashlib,json
root=pathlib.Path(__file__).resolve().parents[1];src=root/'desktop-rs/slint-app/src/live_view.rs';ui=root/'desktop-rs/slint-app/ui/transcript.slint'
before={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (src,ui)}
s=src.read_text();needle='    clarp_core::history_fold::present(rows, view, &options)'
assert s.count(needle)==1
s=s.replace(needle,'''    // Test-only probe measures this actual present call; it never calls
    // present a second time and logs no message text.
    let probe_start = std::time::Instant::now();
    let folded = clarp_core::history_fold::present(rows, view, &options);
    let ns = probe_start.elapsed().as_nanos();
    let handoffs: Vec<_> = folded.folds.iter().filter(|f| f.key.starts_with("live:handoff:")).map(|f| (
        f.key.clone(), f.next.as_deref().and_then(|id| rows.iter().position(|r| r.id == id)), f.pieces.len()
    )).collect();
    thread_local! { static PROBE: std::cell::Cell<(u64, u128, u128, usize, u32)> = const { std::cell::Cell::new((0, 0, 0, 0, 0)) }; }
    PROBE.with(|probe| {
        let (n, sum, max, old_count, lines) = probe.get();
        let emit = lines < 128 && (n < 8 || n % 64 == 0 || old_count != handoffs.len());
        if emit {
            eprintln!("KOKO_PROJECTION session={session} calls={} ns={ns} max_ns={} sum_ns={} live={} rows={} handoffs={handoffs:?}", n + 1, max.max(ns), sum + ns, view.is_some(), rows.len());
        }
        probe.set((n + 1, sum + ns, max.max(ns), handoffs.len(), lines + u32::from(emit)));
    });
    folded''');src.write_text(s)
s=ui.read_text();needle='export component Transcript inherits FocusScope {\n';assert s.count(needle)==1;s=s.replace(needle,needle+'''    // Accessibility-only CI observation: no layout or scroll assignment.
    accessible-value: "content=" + (list.content-height / 1px) + " viewport=" + (list.visible-height / 1px) + " offset=" + (list.content-y / 1px) + " following=" + root.follow;
''',1);ui.write_text(s)
print(json.dumps({'probe_only':True,'shipped_release_unchanged':True,'before':before,'after':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (src,ui)}},sort_keys=True))
