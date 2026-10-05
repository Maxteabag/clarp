from scripts.viz_reload import revisions


def test_frontend_revisions_track_css_code_additions_and_deletions(tmp_path):
    lib=tmp_path/'static/lib';lib.mkdir(parents=True)
    html=tmp_path/'static/viz.html';html.write_text('hello')
    initial=revisions(tmp_path)
    css=lib/'viz-test.css';css.write_text('body{color:red}')
    styles=revisions(tmp_path)
    assert styles['code']==initial['code'] and styles['styles']!=initial['styles']
    html.write_text('changed')
    assert revisions(tmp_path)['code']!=styles['code']
    css.unlink()
    assert revisions(tmp_path)['styles']==initial['styles']
    (tmp_path/'private.txt').write_text('irrelevant')
    unchanged=revisions(tmp_path)
    (tmp_path/'private.txt').write_text('still irrelevant')
    assert revisions(tmp_path)==unchanged
