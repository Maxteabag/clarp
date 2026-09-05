"""Vocabulary harvested from a workspace must be words a person says.

An agent rooted at a home directory walks package trees, and the ranking
rewards a name that recurs across many files. A library ships as `.a`, `.so`,
`.so.0` and `.so.0.0.0`, so that convention alone pushed `libaribb24` above
`Tripletex` in the compiled prompt — 40% of one budget was library names.
"""
from __future__ import annotations

from lib.workspace_vocab import _worth_saying, identifiers_from_paths


def test_library_stems_are_rejected():
    for name in ("libmp3lame", "liblzo2", "libkadm5srv", "libaribb24",
                 "libicui18n", "libngtcp2", "libSDL2"):
        assert not _worth_saying(name), name


def test_version_stamped_fragments_are_rejected():
    for name in ("fftw3l", "pcre2", "aribb24", "kadm5srv", "dri3"):
        assert not _worth_saying(name), name


def test_real_domain_words_still_pass():
    for name in ("Tripletex", "Omarchy", "Hyprland", "Maxteabag", "Termius",
                 "Supabase", "Postgres", "Clarp"):
        assert _worth_saying(name), name


def test_a_package_tree_contributes_nothing():
    paths = [f"/home/linuxbrew/.linuxbrew/lib/libaribb24.so.{i}" for i in range(40)]
    paths += [f"/home/linuxbrew/.linuxbrew/lib/libmp3lame.a"] * 10
    assert identifiers_from_paths(paths) == ()


def test_a_real_project_still_yields_its_nouns():
    paths = [
        "clarp/server/lib/TripletexClient.py",
        "clarp/server/lib/TripletexInvoice.py",
        "clarp/server/lib/OmarchyTheme.py",
    ]
    got = {t.lower() for t in identifiers_from_paths(paths)}
    assert "tripletex" in got
    assert "omarchy" in got
