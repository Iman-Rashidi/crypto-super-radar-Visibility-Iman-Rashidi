from pathlib import Path


def test_manual_scan_html_polls_job_and_surfaces_exact_network_errors():
    source = (Path(__file__).parents[1] / "crypto_super_radar" / "web_app.py").read_text(encoding="utf-8")
    assert "fetch('/jobs/'+job" in source
    assert "setTimeout(poll,2500)" in source
    assert "خطای دقیق پیگیری " in source
    assert "['completed','partial'].includes(String(j.status||'').toLowerCase())" in source
