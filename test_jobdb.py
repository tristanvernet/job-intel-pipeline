from contextlib import closing

import db


def test_database_paths_keep_rows_separate(tmp_path):
    first = db.JobDB(tmp_path / "first.db")
    second = db.JobDB(tmp_path / "second.db")
    first.init()
    second.init()
    job = dict(id="same", company="First", title="Engineer",
               url="https://example.com/job", track="full_time")
    assert first.insert_job(job) is True
    assert second.job_ids() == set()
    assert second.insert_job({**job, "company": "Second"}) is True
    assert first.job_ids() == second.job_ids() == {"same"}
    for store, company in ((first, "First"), (second, "Second")):
        with closing(store.connect()) as conn:
            assert conn.execute("SELECT company FROM jobs").fetchone()[0] == company
