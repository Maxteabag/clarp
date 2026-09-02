"""Run the VAD stress test: mix each speech clip with synthesized noise across
an SNR grid, plus noise-only false-trigger cases, and score each detector.

Usage:
  python run.py [--save-mixes]
Speech clips: corpus/speech/*.wav  (16 kHz mono or anything soundfile reads)
"""
from __future__ import annotations

import glob
import os
import sys
import statistics as st

import vadlib as V

MODEL = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
SPEECH_DIR = os.path.join(os.path.dirname(__file__), "corpus", "speech")
NOISES = ["white", "pink", "hvac", "babble", "hum"]
SNRS = [20, 10, 5, 0, -5]
TOTAL_S = 6.0
ONSET_S = 1.5
SEED = 7


def detectors():
    return {
        "silero": V.Silero(MODEL, threshold=0.5),
        "webrtc": V.WebRTC(mode=2),
        "energy": V.EnergyBaseline(),
    }


def main():
    save = "--save-mixes" in sys.argv
    clips = sorted(glob.glob(os.path.join(SPEECH_DIR, "*.wav")))
    if not clips:
        print(f"No speech clips in {SPEECH_DIR} — generate some first.")
        sys.exit(1)
    dets = detectors()
    print(f"{len(clips)} speech clips · noises={NOISES} · SNRs={SNRS} dB\n")

    # results[detector] = list of score dicts (speech cases)
    results = {k: [] for k in dets}
    false_triggers = {k: 0 for k in dets}      # on noise-only beds
    ft_total = 0

    for clip in clips:
        speech = V.load_mono16k(clip)
        for ni, noise_kind in enumerate(NOISES):
            noise = V.synth_noise(noise_kind, TOTAL_S, seed=SEED + ni)
            for snr in SNRS:
                mix, ron, roff = V.place_in_bed(
                    speech, noise, onset_s=ONSET_S, total_s=TOTAL_S, snr_db=snr)
                if save:
                    name = f"{os.path.basename(clip)[:-4]}_{noise_kind}_{snr}dB.wav"
                    V.write_wav(os.path.join(os.path.dirname(__file__), "corpus", "mixes", name), mix)
                for dn, det in dets.items():
                    sc = det.segments(mix)
                    results[dn].append(V.score(ron, roff, sc, TOTAL_S))

    # noise-only false-trigger test (no speech present)
    for ni, noise_kind in enumerate(NOISES):
        noise = V.synth_noise(noise_kind, TOTAL_S, seed=SEED + 100 + ni)
        ft_total += 1
        for dn, det in dets.items():
            if det.segments(noise):
                false_triggers[dn] += 1

    # ---- report ----
    def avg(xs):
        xs = [x for x in xs if x == x]      # drop NaN
        return st.mean(xs) if xs else float("nan")

    print(f"{'detector':9} {'F1':>6} {'prec':>6} {'recall':>7} {'FA%':>6} "
          f"{'detect%':>8} {'onset_ms':>9} {'offset_ms':>10} {'segs':>5}  false-trig")
    print("-" * 86)
    for dn in dets:
        r = results[dn]
        f1 = avg([x["f1"] for x in r])
        prec = avg([x["precision"] for x in r])
        rec = avg([x["recall"] for x in r])
        fa = avg([x["false_alarm"] for x in r]) * 100
        det_pct = 100 * sum(x["detected"] for x in r) / len(r)
        onset = avg([x["onset_lat"] for x in r]) * 1000
        offset = avg([x["offset_lat"] for x in r]) * 1000
        segs = avg([x["n_segments"] for x in r])
        print(f"{dn:9} {f1:6.3f} {prec:6.3f} {rec:7.3f} {fa:6.1f} "
              f"{det_pct:7.0f}% {onset:9.0f} {offset:10.0f} {segs:5.1f}  "
              f"{false_triggers[dn]}/{ft_total}")

    print("\nPer-SNR F1 (silero / webrtc / energy):")
    per = {dn: {snr: [] for snr in SNRS} for dn in dets}
    idx_map = []
    for clip in clips:
        for noise_kind in NOISES:
            for snr in SNRS:
                idx_map.append(snr)
    for dn in dets:
        for i, sc in enumerate(results[dn]):
            per[dn][idx_map[i]].append(sc["f1"])
    for snr in SNRS:
        parts = " / ".join(f"{avg(per[dn][snr]):.2f}" for dn in dets)
        print(f"  {snr:>3} dB:  {parts}")


if __name__ == "__main__":
    main()
